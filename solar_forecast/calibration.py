"""Bounded autonomous calibration from forecast-versus-actual daylight hours."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
ALGORITHM_VERSION = "bounded-temperature-residual-2"


def fit_calibration(
    history,
    site_id,
    timezone_name,
    minimum_samples=48,
    minimum_complete_days=3,
    minimum_valid_hours_per_day=4,
    rolling_window_days=180,
    factor_min=0.70,
    factor_max=1.30,
    minimum_mae_improvement_percent=2.0,
    minimum_actual_kwh=0.01,
    required_measurement_source_fingerprint=None,
):
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    trusted_daily_dates = {
        item["target_date"]
        for item in history.list_actuals(
            site_id,
            limit=max(int(rolling_window_days), int(minimum_complete_days)),
        )
        if (item.get("details") or {}).get("quality_valid") is True
        and (
            required_measurement_source_fingerprint is None
            or (item.get("details") or {}).get(
                "measurement_source_fingerprint"
            )
            == required_measurement_source_fingerprint
        )
        and item["target_date"] < today
    }
    usable_by_day = {}
    for target_date in history.hourly_actual_dates(
        site_id, timezone_name, limit=int(rolling_window_days)
    ):
        if target_date not in trusted_daily_dates or target_date >= today:
            continue
        day_rows = []
        for row in history.hourly_comparisons(
            site_id, target_date, timezone_name
        )["hours"]:
            predicted = row["forecast_kwh"]
            actual = row["actual_kwh"]
            temperature = (
                row["actual_temperature_c"]
                if row["actual_temperature_c"] is not None
                else row["forecast_temperature_c"]
            )
            if (
                row.get("actual_quality_valid") is not True
                or (
                    required_measurement_source_fingerprint is not None
                    and row.get("actual_measurement_source_fingerprint")
                    != required_measurement_source_fingerprint
                )
                or predicted is None
                or actual is None
                or temperature is None
                or predicted < 0.25
                or actual < float(minimum_actual_kwh)
                or row["solar_elevation_deg"] < 5
            ):
                continue
            day_rows.append(
                {
                    "target_time": row["target_time"],
                    "predicted": float(predicted),
                    "actual": max(0.0, float(actual)),
                    "x": float(temperature) - 25.0,
                    "weight": max(0.25, float(predicted)),
                }
            )
        if len(day_rows) >= int(minimum_valid_hours_per_day):
            usable_by_day[target_date] = day_rows

    training_days = sorted(usable_by_day)
    usable = [row for day in training_days for row in usable_by_day[day]]
    if (
        len(training_days) < int(minimum_complete_days)
        or len(usable) < int(minimum_samples)
    ):
        return None

    weight_sum = sum(item["weight"] for item in usable)
    x_mean = sum(item["weight"] * item["x"] for item in usable) / weight_sum
    y_mean = (
        sum(
            item["weight"] * item["actual"] / item["predicted"]
            for item in usable
        )
        / weight_sum
    )
    denominator = sum(
        item["weight"] * (item["x"] - x_mean) ** 2 for item in usable
    )
    slope = (
        0.0
        if denominator == 0
        else sum(
            item["weight"]
            * (item["x"] - x_mean)
            * (item["actual"] / item["predicted"] - y_mean)
            for item in usable
        )
        / denominator
    )
    slope = max(-0.02, min(0.02, slope))
    factor_min = float(factor_min)
    factor_max = float(factor_max)
    intercept = max(factor_min, min(factor_max, y_mean - slope * x_mean))

    before = sum(
        abs(item["actual"] - item["predicted"]) for item in usable
    ) / len(usable)
    after = sum(
        abs(
            item["actual"]
            - item["predicted"]
            * _factor(
                intercept,
                slope,
                item["x"] + 25.0,
                factor_min,
                factor_max,
            )
        )
        for item in usable
    ) / len(usable)
    accepted = after <= before * (
        1.0 - float(minimum_mae_improvement_percent) / 100.0
    )
    fitted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "site_id": site_id,
        "fitted_at": fitted_at,
        "algorithm_version": ALGORITHM_VERSION,
        "training_start": min(item["target_time"] for item in usable),
        "training_end": max(item["target_time"] for item in usable),
        "sample_count": len(usable),
        "training_day_count": len(training_days),
        "training_days": training_days,
        "measurement_source_fingerprint": (
            required_measurement_source_fingerprint
        ),
        "intercept_factor": round(intercept, 8),
        "temperature_slope_per_c": round(slope, 8),
        "mae_before_kwh": round(before, 8),
        "mae_after_kwh": round(after, 8),
        "accepted": accepted,
        "guardrails": {
            "minimum_samples": int(minimum_samples),
            "minimum_complete_days": int(minimum_complete_days),
            "minimum_valid_hours_per_day": int(minimum_valid_hours_per_day),
            "rolling_window_days": int(rolling_window_days),
            "requires_trusted_daily_observation": True,
            "requires_quality_valid_hourly_observation": True,
            "requires_current_measurement_source": (
                required_measurement_source_fingerprint is not None
            ),
            "minimum_forecast_kwh": 0.25,
            "minimum_actual_kwh": float(minimum_actual_kwh),
            "minimum_solar_elevation_deg": 5,
            "factor_min": factor_min,
            "factor_max": factor_max,
            "minimum_mae_improvement_percent": float(
                minimum_mae_improvement_percent
            ),
        },
    }


def apply_calibration(forecast, calibration):
    if not calibration or not calibration.get("accepted"):
        return forecast
    intercept = float(calibration["intercept_factor"])
    slope = float(calibration["temperature_slope_per_c"])
    guardrails = calibration.get("guardrails") or {}
    factor_min = float(guardrails.get("factor_min", 0.70))
    factor_max = float(guardrails.get("factor_max", 1.30))
    day_totals = {}
    for hour in forecast.get("hourly_forecast", []):
        temperature = float(hour["air_temperature_c"])
        factor = _factor(
            intercept, slope, temperature, factor_min, factor_max
        )
        hour["uncalibrated_expected_kwh"] = hour["expected_kwh"]
        hour["calibration_factor"] = round(factor, 5)
        hour["expected_kwh"] = round(hour["expected_kwh"] * factor, 3)
        for key, value in list(
            hour.get("expected_kwh_by_direction", {}).items()
        ):
            hour["expected_kwh_by_direction"][key] = round(value * factor, 3)
        day_totals.setdefault(
            hour["target_date"], {"total": 0.0, "directions": {}}
        )
        day_totals[hour["target_date"]]["total"] += hour["expected_kwh"]
        for key, value in hour.get("expected_kwh_by_direction", {}).items():
            directions = day_totals[hour["target_date"]]["directions"]
            directions[key] = directions.get(key, 0.0) + value
    for day in forecast.get("daily_forecast", []):
        totals = day_totals.get(day["target_date"])
        if not totals:
            continue
        day["uncalibrated_expected_kwh"] = day["expected_kwh"]
        day["expected_kwh"] = round(totals["total"], 1)
        day["expected_kwh_by_direction"] = {
            key: round(value, 1)
            for key, value in sorted(totals["directions"].items())
        }
    summary = forecast["forecast_summary"]
    summary["expected_kwh_total"] = round(
        sum(item["expected_kwh"] for item in forecast["daily_forecast"]), 1
    )
    summary["calibration"] = {
        key: calibration[key]
        for key in (
            "calibration_id",
            "algorithm_version",
            "fitted_at",
            "training_start",
            "training_end",
            "sample_count",
            "training_day_count",
            "training_days",
            "intercept_factor",
            "temperature_slope_per_c",
            "mae_before_kwh",
            "mae_after_kwh",
        )
        if key in calibration
    }
    return forecast


def _factor(
    intercept,
    slope,
    temperature_c,
    factor_min=0.70,
    factor_max=1.30,
):
    return max(
        float(factor_min),
        min(
            float(factor_max),
            float(intercept)
            + float(slope) * (float(temperature_c) - 25.0),
        ),
    )
