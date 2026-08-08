"""Deterministic, observation-only energy planning for LSF vNext.

The module has no Home Assistant client and cannot actuate anything.  It turns
an immutable forecast plus an explicitly timestamped site snapshot into a
reproducible proposal that a separate, authorised site actuator may consume.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
PLAN_VERSION = "0.6.0-observation-draft-1"


class RegulatorInputError(ValueError):
    """Raised when a plan cannot be reproduced safely from the supplied data."""


def build_observation_plan(site_config, forecast, snapshot, prices):
    """Return a deterministic 72-hour energy plan without actuator authority."""
    now = _instant(snapshot.get("observed_at"), "observed_at")
    issued_at = _instant(
        ((forecast.get("forecast_summary") or {}).get("forecast_issued_at")),
        "forecast.forecast_issued_at",
    )
    if issued_at > now + timedelta(minutes=5):
        raise RegulatorInputError("forecast issue time is in the future")

    usable_kwh = _positive(snapshot.get("usable_battery_kwh"), "usable_battery_kwh")
    soc = _range(snapshot.get("soc_percent"), "soc_percent", 0, 100)
    minimum_soc = _range(
        snapshot.get("minimum_soc_percent"), "minimum_soc_percent", 0, 100
    )
    if soc < minimum_soc:
        raise RegulatorInputError("observed SOC is below configured minimum SOC")
    target_soc = _range(
        snapshot.get("target_checkpoint_soc_percent", 95),
        "target_checkpoint_soc_percent",
        minimum_soc,
        100,
    )
    checkpoint_at = _instant(snapshot.get("checkpoint_at"), "checkpoint_at")
    base_load_kw = _nonnegative(snapshot.get("base_load_kw"), "base_load_kw")
    absolute_import_kw = _positive(
        snapshot.get("absolute_import_limit_kw"), "absolute_import_limit_kw"
    )
    grid_margin_kw = _nonnegative(snapshot.get("grid_margin_kw", 0.5), "grid_margin_kw")
    charge_efficiency = _range(
        snapshot.get("charge_efficiency", 0.95), "charge_efficiency", 0.01, 1
    )
    conversion_loss_percent = _range(
        snapshot.get("conversion_loss_percent", 3),
        "conversion_loss_percent",
        0,
        30,
    )
    options = _work_limit_options(snapshot.get("work_limit_options"))
    minimum_export_kw = _positive(
        snapshot.get("minimum_active_export_kw", 0.25),
        "minimum_active_export_kw",
    )

    price_map = _price_map(prices)
    hourly = []
    for raw in forecast.get("hourly_forecast") or []:
        start = _instant(raw.get("time") or raw.get("start"), "forecast hour start")
        duration = _positive(raw.get("interval_hours", 1), "interval_hours")
        if start < now - timedelta(hours=duration):
            continue
        if start >= now + timedelta(hours=72):
            continue
        solar = _nonnegative(raw.get("expected_kwh", 0), "expected_kwh")
        cloud = raw.get("cloud_area_fraction_percent")
        margin = _uncertainty_margin(cloud, issued_at, now)
        safe_solar = solar * (1 - margin / 100)
        load = _load_for_interval(snapshot, start, duration, base_load_kw)
        loss = safe_solar * conversion_loss_percent / 100
        hourly.append(
            {
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=duration)),
                "interval_hours": duration,
                "solar_kwh": round(solar, 4),
                "safe_solar_kwh": round(safe_solar, 4),
                "base_load_kwh": round(load, 4),
                "conversion_loss_kwh": round(loss, 4),
                "uncertainty_margin_percent": margin,
                "nord_pool_raw_nok_per_kwh": price_map.get(_hour_key(start)),
                "planned_import_kwh": 0.0,
                "planned_export_kwh": 0.0,
            }
        )
    if not hourly:
        raise RegulatorInputError("no usable forecast intervals in the next 72 hours")

    stored_initial = usable_kwh * soc / 100
    minimum_stored = usable_kwh * minimum_soc / 100
    stored = stored_initial
    lowest_stored = stored
    first_deficit_index = None
    stored_without_import = []
    for index, row in enumerate(hourly):
        stored = min(
            usable_kwh,
            stored + row["safe_solar_kwh"] - row["base_load_kwh"] - row["conversion_loss_kwh"],
        )
        stored_without_import.append(stored)
        lowest_stored = min(lowest_stored, stored)
        if stored < minimum_stored and first_deficit_index is None:
            first_deficit_index = index

    energy_shortfall = max(0.0, minimum_stored - lowest_stored)
    import_need = energy_shortfall / charge_efficiency
    latest_safe_start = None
    import_unmet = 0.0
    if import_need > 1e-9:
        deadline = first_deficit_index if first_deficit_index is not None else len(hourly) - 1
        first_deadline_need = max(
            0.0,
            (minimum_stored - stored_without_import[deadline]) / charge_efficiency,
        )
        cumulative = 0.0
        for index in range(deadline, -1, -1):
            row = hourly[index]
            charge_power = max(0.0, absolute_import_kw - grid_margin_kw - row["base_load_kwh"] / row["interval_hours"])
            cumulative += charge_power * row["interval_hours"]
            latest_safe_start = row["start"]
            if cumulative + 1e-9 >= first_deadline_need:
                break
        if cumulative + 1e-9 < first_deadline_need:
            latest_safe_start = None

        # Satisfy every prefix constraint, not merely the end-of-horizon
        # energy total. This draft deliberately schedules the minimum import
        # just in time. A later price optimiser may move it earlier, but only
        # if a replay proves that battery headroom prevents the energy being
        # lost to an intervening full-SOC clamp.
        capacities = []
        for row in hourly:
            charge_power = max(
                0.0,
                absolute_import_kw
                - grid_margin_kw
                - row["base_load_kwh"] / row["interval_hours"],
            )
            capacities.append(charge_power * row["interval_hours"])
        planned_stored = stored_initial
        for index, row in enumerate(hourly):
            projected = min(
                usable_kwh,
                planned_stored
                + row["safe_solar_kwh"]
                - row["base_load_kwh"]
                - row["conversion_loss_kwh"],
            )
            needed = max(0.0, (minimum_stored - projected) / charge_efficiency)
            amount = min(needed, capacities[index])
            row["planned_import_kwh"] = round(amount, 6)
            import_unmet = max(import_unmet, needed - amount)
            planned_stored = min(
                usable_kwh, projected + amount * charge_efficiency
            )

    total_safe_solar = sum(row["safe_solar_kwh"] for row in hourly)
    total_load = sum(row["base_load_kwh"] for row in hourly)
    total_loss = sum(row["conversion_loss_kwh"] for row in hourly)
    export_budget = max(
        0.0,
        stored_initial + total_safe_solar - total_load - total_loss - minimum_stored,
    )
    feasible = import_unmet <= 1e-9

    if import_need > 1e-9 and hourly[0]["planned_import_kwh"] > 0:
        raw_work_limit = hourly[0]["planned_import_kwh"] / hourly[0]["interval_hours"]
        reason = "planned_import_before_projected_reserve_breach"
    elif import_need > 1e-9:
        raw_work_limit = 0.0
        reason = "voluntary_export_blocked_to_preserve_multiday_reserve"
    elif export_budget > 1e-9:
        known = [row["nord_pool_raw_nok_per_kwh"] for row in hourly if row["nord_pool_raw_nok_per_kwh"] is not None]
        current = hourly[0]["nord_pool_raw_nok_per_kwh"]
        rank = _price_rank(current, known)
        raw_work_limit = -minimum_export_kw if rank is None or rank < 0.75 else -min(
            float(snapshot.get("maximum_export_kw", 4.7)),
            max(minimum_export_kw, export_budget / hourly[0]["interval_hours"]),
        )
        reason = "price_weighted_export_with_funded_multiday_reserve" if rank is not None and rank >= 0.75 else "minimum_anti_import_export_with_funded_reserve"
    else:
        raw_work_limit = 0.0
        reason = "no_export_budget_after_multiday_reserve"

    selected = _nearest_option(raw_work_limit, options)
    stored = stored_initial
    for row in hourly:
        stored = min(
            usable_kwh,
            stored + row["safe_solar_kwh"] + row["planned_import_kwh"] * charge_efficiency
            - row["base_load_kwh"] - row["conversion_loss_kwh"] - row["planned_export_kwh"],
        )
        row["projected_soc_percent"] = round(100 * stored / usable_kwh, 2)
        row["required_reserve_soc_percent"] = minimum_soc
        row_end = _instant(row["end"], "end")
        if row_end <= checkpoint_at and checkpoint_at > now:
            progress = min(
                1.0,
                max(0.0, (row_end - now).total_seconds() / (checkpoint_at - now).total_seconds()),
            )
            required_trajectory = soc + (target_soc - soc) * progress
            row["deviation_to_next_checkpoint_percentage_points"] = round(
                row["projected_soc_percent"] - required_trajectory, 2
            )
        else:
            row["deviation_to_next_checkpoint_percentage_points"] = None
        row["calculated_work_limit_kw"] = round(raw_work_limit, 3) if row is hourly[0] else None
        row["selected_work_limit_option"] = selected if row is hourly[0] else None
        row["reason"] = reason if row is hourly[0] else "forecast_interval"

    if min(row["projected_soc_percent"] for row in hourly) < minimum_soc - 0.01:
        feasible = False

    projected_at_checkpoint = _soc_at_or_before(hourly, checkpoint_at)
    lowest_row = min(hourly, key=lambda row: row["projected_soc_percent"])
    feasibility = "achievable"
    if not feasible:
        feasibility = "infeasible_without_import_or_flexible_load"
    elif projected_at_checkpoint is not None and projected_at_checkpoint < target_soc:
        feasibility = "at_risk"

    input_fingerprint = hashlib.sha256(
        json.dumps(
            {"site": site_config.get("site"), "forecast": forecast, "snapshot": snapshot, "prices": prices},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    decision_id = hashlib.sha256(
        f"{site_config['site']['id']}|{_iso(now)}|{input_fingerprint}".encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "lsf-regulator-plan/1",
        "plan_version": PLAN_VERSION,
        "decision_id": decision_id,
        "calculated_at": _iso(now),
        "mode": "observe_only",
        "season_mode": snapshot.get("season_mode", "unknown"),
        "actuation_authorized": False,
        "site_id": site_config["site"]["id"],
        "input_fingerprint": input_fingerprint,
        "forecast_issued_at": _iso(issued_at),
        "forecast_horizon_hours": round(sum(row["interval_hours"] for row in hourly), 2),
        "soc_now_percent": soc,
        "soc_checkpoint_at": _iso(checkpoint_at),
        "soc_checkpoint_target_percent": target_soc,
        "soc_checkpoint_feasibility": feasibility,
        "projected_soc_at_checkpoint_percent": projected_at_checkpoint,
        "lowest_projected_soc_percent": lowest_row["projected_soc_percent"],
        "lowest_projected_soc_at": lowest_row["end"],
        "next_useful_pv_at": _next_useful_pv(hourly),
        "base_load_until_useful_pv_kwh": _load_until_useful_pv(hourly),
        "required_reserve_kwh": round(minimum_stored, 3),
        "uncertainty_margin_kwh": round(
            sum(row["solar_kwh"] - row["safe_solar_kwh"] for row in hourly), 3
        ),
        "export_budget_kwh": round(export_budget, 3),
        "import_need_kwh": round(import_need, 3),
        "latest_safe_import_start": latest_safe_start,
        "absolute_import_limit_kw": absolute_import_kw,
        "observed_work_limit_kw": (
            _number(snapshot.get("observed_work_limit_kw"), "observed_work_limit_kw")
            if snapshot.get("observed_work_limit_kw") is not None
            else None
        ),
        "proposed_work_limit_kw": round(raw_work_limit, 3),
        "selected_work_limit_option": selected,
        "reason": reason,
        "data_quality": {
            "price_intervals_known": sum(row["nord_pool_raw_nok_per_kwh"] is not None for row in hourly),
            "forecast_is_stale": now - issued_at > timedelta(hours=2),
            "import_schedule_feasible": feasible,
            "input_snapshot_complete": True,
        },
        "hours": hourly,
    }


def _instant(value, field):
    if not value:
        raise RegulatorInputError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegulatorInputError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise RegulatorInputError(f"{field} must include timezone")
    return parsed.astimezone(UTC)


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value, field):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RegulatorInputError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise RegulatorInputError(f"{field} must be finite")
    return result


def _positive(value, field):
    result = _number(value, field)
    if result <= 0:
        raise RegulatorInputError(f"{field} must be positive")
    return result


def _nonnegative(value, field):
    result = _number(value, field)
    if result < 0:
        raise RegulatorInputError(f"{field} must be non-negative")
    return result


def _range(value, field, low, high):
    result = _number(value, field)
    if result < low or result > high:
        raise RegulatorInputError(f"{field} must be between {low} and {high}")
    return result


def _work_limit_options(values):
    if not isinstance(values, list) or not values:
        raise RegulatorInputError("work_limit_options must be a non-empty list")
    options = sorted({_number(value, "work_limit option") for value in values})
    return options


def _nearest_option(value, options):
    return min(options, key=lambda option: (abs(option - value), abs(option)))


def _hour_key(value):
    return value.replace(minute=0, second=0, microsecond=0)


def _price_map(prices):
    # Preserve source-native 15/60-minute intervals and aggregate them by
    # duration for the current hourly planner. Nord Pool prices may be
    # negative, so zero is not a lower bound.
    weighted = {}
    for row in prices or []:
        start = _instant(row.get("start"), "price start")
        end = _instant(
            row.get("end") or _iso(start + timedelta(hours=1)), "price end"
        )
        if end <= start:
            raise RegulatorInputError("price end must follow price start")
        price = _number(row.get("price_nok_per_kwh"), "price_nok_per_kwh")
        cursor = start
        while cursor < end:
            bucket = _hour_key(cursor)
            bucket_end = bucket + timedelta(hours=1)
            segment_end = min(end, bucket_end)
            duration = (segment_end - cursor).total_seconds() / 3600
            total, hours = weighted.get(bucket, (0.0, 0.0))
            weighted[bucket] = (total + price * duration, hours + duration)
            cursor = segment_end
    return {
        bucket: total / hours
        for bucket, (total, hours) in weighted.items()
        if hours > 0
    }


def _load_for_interval(snapshot, start, duration, fallback_kw):
    values = snapshot.get("base_load_kwh_by_start") or {}
    value = values.get(_iso(start))
    return _nonnegative(value, "base_load_kwh_by_start") if value is not None else fallback_kw * duration


def _uncertainty_margin(cloud, issued_at, now):
    if now - issued_at > timedelta(hours=2) or cloud is None:
        return 40
    cloud = _range(cloud, "cloud_area_fraction_percent", 0, 100)
    if cloud >= 80:
        return 30
    if cloud >= 30:
        return 20
    return 10


def _price_rank(current, values):
    if current is None or not values:
        return None
    return sum(value <= current for value in values) / len(values)


def _soc_at_or_before(hours, checkpoint):
    eligible = [row for row in hours if _instant(row["end"], "end") <= checkpoint]
    return eligible[-1]["projected_soc_percent"] if eligible else None


def _next_useful_pv(hours):
    for row in hours:
        if row["safe_solar_kwh"] >= row["base_load_kwh"]:
            return row["start"]
    return None


def _load_until_useful_pv(hours):
    total = 0.0
    for row in hours:
        if row["safe_solar_kwh"] >= row["base_load_kwh"]:
            break
        total += row["base_load_kwh"]
    return round(total, 3)
