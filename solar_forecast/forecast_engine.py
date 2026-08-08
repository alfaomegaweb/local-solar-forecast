"""Generic solar forecast engine.

The engine has no Home Assistant or site-specific dependencies.  It accepts a
validated site dictionary and MET Norway Locationforecast timeseries data and
returns plain JSON-compatible dictionaries.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


MODEL_VERSION = "local-solar-poa-0.2.0"
UTC = timezone.utc


class ConfigurationError(ValueError):
    """Raised when a site configuration cannot produce a safe forecast."""


def _finite(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    return result


def validate_site_config(config):
    """Validate and normalize the site-specific part of the configuration."""
    if not isinstance(config, dict):
        raise ConfigurationError("site configuration must be an object")

    site = config.get("site") or {}
    site_id = str(site.get("id") or "").strip()
    if not site_id:
        raise ConfigurationError("site.id is required")

    latitude = _finite(site.get("latitude"), "site.latitude")
    longitude = _finite(site.get("longitude"), "site.longitude")
    if not -90 <= latitude <= 90:
        raise ConfigurationError("site.latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ConfigurationError("site.longitude must be between -180 and 180")

    timezone_name = str(site.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ConfigurationError("site.timezone must be a valid IANA timezone") from exc

    pv = config.get("pv") or {}
    pv_mode = str(pv.get("mode") or "installed").strip().lower()
    if pv_mode not in {"installed", "none"}:
        raise ConfigurationError("pv.mode must be installed or none")

    arrays = config.get("arrays")
    if not isinstance(arrays, list):
        raise ConfigurationError("arrays must be a list")
    if pv_mode == "installed" and not arrays:
        raise ConfigurationError(
            "at least one arrays entry is required when pv.mode is installed"
        )
    if pv_mode == "none" and arrays:
        raise ConfigurationError("arrays must be empty when pv.mode is none")

    normalized_arrays = []
    seen_ids = set()
    calculated_panel_count = 0
    for index, item in enumerate(arrays):
        if not isinstance(item, dict):
            raise ConfigurationError(f"arrays[{index}] must be an object")
        array_id = str(item.get("id") or f"array_{index + 1:02d}").strip()
        if array_id in seen_ids:
            raise ConfigurationError(f"duplicate array id: {array_id}")
        seen_ids.add(array_id)

        capacity = item.get("capacity_kwp")
        module_count = item.get("module_count")
        module_power_wp = item.get("module_power_wp")
        if capacity is None:
            capacity = (
                _finite(module_count, f"arrays[{index}].module_count")
                * _finite(module_power_wp, f"arrays[{index}].module_power_wp")
                / 1000.0
            )
        elif module_count is not None and module_power_wp is not None:
            declared_capacity = _finite(
                capacity, f"arrays[{index}].capacity_kwp"
            )
            module_capacity = (
                _finite(module_count, f"arrays[{index}].module_count")
                * _finite(module_power_wp, f"arrays[{index}].module_power_wp")
                / 1000.0
            )
            if abs(declared_capacity - module_capacity) > 0.01:
                raise ConfigurationError(
                    f"arrays[{index}].capacity_kwp does not match "
                    "module_count × module_power_wp"
                )
        capacity = _finite(capacity, f"arrays[{index}].capacity_kwp")
        tilt = _finite(item.get("tilt_deg"), f"arrays[{index}].tilt_deg")
        azimuth = _finite(item.get("azimuth_deg"), f"arrays[{index}].azimuth_deg") % 360
        if capacity <= 0:
            raise ConfigurationError(f"arrays[{index}].capacity_kwp must be positive")
        if not 0 <= tilt <= 180:
            raise ConfigurationError(f"arrays[{index}].tilt_deg must be between 0 and 180")
        if module_count is not None:
            normalized_count = _finite(
                module_count, f"arrays[{index}].module_count"
            )
            if normalized_count <= 0 or not normalized_count.is_integer():
                raise ConfigurationError(
                    f"arrays[{index}].module_count must be a positive integer"
                )
            calculated_panel_count += int(normalized_count)

        orientation = str(item.get("orientation") or azimuth_to_direction(azimuth))
        normalized_arrays.append(
            {
                **item,
                "id": array_id,
                "capacity_kwp": capacity,
                "tilt_deg": tilt,
                "azimuth_deg": azimuth,
                "orientation": orientation,
            }
        )

    model = config.get("model") or {}
    performance_ratio = _finite(
        model.get("base_performance_ratio", 0.82),
        "model.base_performance_ratio",
    )
    if not 0 < performance_ratio <= 1.2:
        raise ConfigurationError("model.base_performance_ratio must be in (0, 1.2]")

    system = config.get("system") or {}
    calculated_capacity = sum(item["capacity_kwp"] for item in normalized_arrays)
    if system.get("installed_capacity_kwp") is not None:
        declared = _finite(
            system["installed_capacity_kwp"], "system.installed_capacity_kwp"
        )
        if abs(declared - calculated_capacity) > 0.01:
            raise ConfigurationError(
                "system.installed_capacity_kwp does not match arrays"
            )
    if system.get("panel_count") is not None:
        declared_panels = _finite(system["panel_count"], "system.panel_count")
        if not declared_panels.is_integer() or int(declared_panels) != calculated_panel_count:
            raise ConfigurationError("system.panel_count does not match arrays")

    measurements = config.get("measurements") or {}
    solar_energy = measurements.get("solar_energy") or {}
    statistic_entities = solar_energy.get("statistic_entities") or []
    if pv_mode == "none" and statistic_entities:
        raise ConfigurationError(
            "PV statistic_entities are not allowed when pv.mode is none"
        )
    if solar_energy:
        _validate_entity_ids(
            statistic_entities,
            "measurements.solar_energy.statistic_entities",
            required=True,
        )
        current_entities = solar_energy.get("current_day_entities") or []
        if current_entities:
            _validate_entity_ids(
                current_entities,
                "measurements.solar_energy.current_day_entities",
            )
        quality = solar_energy.get("data_quality") or {}
        if quality.get("require_all_entities", True) is not True:
            raise ConfigurationError(
                "measurements.solar_energy.data_quality.require_all_entities "
                "must be true for safe empirical learning"
            )
        minimum_daily = _finite(
            quality.get("minimum_daily_total_kwh", 0.05),
            "measurements.solar_energy.data_quality.minimum_daily_total_kwh",
        )
        if minimum_daily <= 0:
            raise ConfigurationError(
                "minimum_daily_total_kwh must be greater than zero"
            )
        maximum_specific_yield = _finite(
            quality.get("maximum_daily_specific_yield_kwh_per_kwp", 8.0),
            (
                "measurements.solar_energy.data_quality."
                "maximum_daily_specific_yield_kwh_per_kwp"
            ),
        )
        if not 0 < maximum_specific_yield <= 12:
            raise ConfigurationError(
                "maximum_daily_specific_yield_kwh_per_kwp must be in (0, 12]"
            )

    calibration = config.get("calibration") or {}
    if pv_mode == "none" and calibration.get("enabled") is True:
        raise ConfigurationError("PV calibration cannot be enabled when pv.mode is none")
    if calibration.get("enabled") is True:
        if not statistic_entities:
            raise ConfigurationError(
                "enabled calibration requires solar statistic_entities"
            )
        if calibration.get("algorithm") != "bounded-temperature-residual-2":
            raise ConfigurationError(
                "enabled calibration requires bounded-temperature-residual-2"
            )
        minimum_hours = int(calibration.get("minimum_training_hours", 48))
        minimum_days = int(calibration.get("minimum_training_days", 3))
        hours_per_day = int(calibration.get("minimum_valid_hours_per_day", 4))
        rolling_days = int(calibration.get("rolling_window_days", 180))
        if minimum_hours < 1 or minimum_days < 3 or hours_per_day < 1:
            raise ConfigurationError("calibration training thresholds are unsafe")
        if rolling_days < minimum_days:
            raise ConfigurationError(
                "calibration.rolling_window_days must cover minimum_training_days"
            )

    return {
        **config,
        "site": {
            **site,
            "id": site_id,
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone_name,
        },
        "arrays": normalized_arrays,
        "pv": {**pv, "mode": pv_mode},
        "model": {
            **model,
            "base_performance_ratio": performance_ratio,
            "albedo": _finite(model.get("albedo", 0.20), "model.albedo"),
            "temperature_coefficient_per_c": _finite(
                model.get("temperature_coefficient_per_c", -0.004),
                "model.temperature_coefficient_per_c",
            ),
            "nominal_operating_cell_temperature_c": _finite(
                model.get("nominal_operating_cell_temperature_c", 45),
                "model.nominal_operating_cell_temperature_c",
            ),
            "uncertainty_percent": _finite(
                model.get("uncertainty_percent", 15),
                "model.uncertainty_percent",
            ),
        },
    }


def _validate_entity_ids(values, name, required=False):
    if not isinstance(values, list) or (required and not values):
        raise ConfigurationError(f"{name} must be a non-empty list")
    normalized = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or "." not in value or value.strip() != value:
            raise ConfigurationError(f"{name}[{index}] is not a valid entity id")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise ConfigurationError(f"{name} contains duplicate entity ids")


def azimuth_to_direction(azimuth):
    """Return a readable compass sector for azimuth (0=N, 90=E)."""
    azimuth = float(azimuth) % 360
    if azimuth < 45 or azimuth >= 315:
        return "north"
    if azimuth < 135:
        return "east"
    if azimuth < 225:
        return "south"
    return "west"


def solar_position(when, latitude, longitude):
    """Approximate solar elevation and azimuth using NOAA equations."""
    if when.tzinfo is None:
        raise ValueError("solar_position requires a timezone-aware datetime")

    local = when
    day = local.timetuple().tm_yday
    local_hour = (
        local.hour
        + local.minute / 60.0
        + local.second / 3600.0
        + local.microsecond / 3_600_000_000.0
    )
    year_days = 366 if _is_leap(local.year) else 365
    gamma = 2.0 * math.pi / year_days * (day - 1 + (local_hour - 12.0) / 24.0)

    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    utc_offset_hours = local.utcoffset().total_seconds() / 3600.0
    time_offset = equation_of_time + 4.0 * longitude - 60.0 * utc_offset_hours
    true_solar_minutes = (local_hour * 60.0 + time_offset) % 1440.0
    hour_angle_deg = true_solar_minutes / 4.0 - 180.0
    hour_angle = math.radians(hour_angle_deg)
    latitude_rad = math.radians(latitude)

    cos_zenith = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    elevation = 90.0 - math.degrees(math.acos(cos_zenith))

    azimuth = (
        math.degrees(
            math.atan2(
                math.sin(hour_angle),
                math.cos(hour_angle) * math.sin(latitude_rad)
                - math.tan(declination) * math.cos(latitude_rad),
            )
        )
        + 180.0
    ) % 360.0
    return elevation, azimuth


def sunrise_sunset(target_date, timezone_name, latitude, longitude):
    """Calculate local sunrise and sunset by bracketing elevation zero."""
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(target_date, time(0, 0), tzinfo=zone)
    step = timedelta(minutes=5)
    previous_time = start
    previous_elevation = solar_position(previous_time, latitude, longitude)[0]
    sunrise = None
    sunset = None
    current = start + step
    end = start + timedelta(days=1)
    while current <= end:
        elevation = solar_position(current, latitude, longitude)[0]
        if previous_elevation <= 0 < elevation and sunrise is None:
            sunrise = _zero_crossing(
                previous_time, current, previous_elevation, elevation
            )
        if previous_elevation > 0 >= elevation:
            sunset = _zero_crossing(
                previous_time, current, previous_elevation, elevation
            )
        previous_time = current
        previous_elevation = elevation
        current += step
    return (
        sunrise.isoformat() if sunrise else None,
        sunset.isoformat() if sunset else None,
    )


def _zero_crossing(before, after, before_value, after_value):
    span = after_value - before_value
    fraction = 0.5 if span == 0 else max(0.0, min(1.0, -before_value / span))
    return before + (after - before) * fraction


def plane_incidence_cosine(solar_elevation, solar_azimuth, tilt, panel_azimuth):
    """Cosine of incidence angle between sun ray and panel normal."""
    elevation = math.radians(solar_elevation)
    sun_azimuth = math.radians(solar_azimuth)
    surface_tilt = math.radians(tilt)
    surface_azimuth = math.radians(panel_azimuth)
    cosine = (
        math.sin(elevation) * math.cos(surface_tilt)
        + math.cos(elevation)
        * math.sin(surface_tilt)
        * math.cos(sun_azimuth - surface_azimuth)
    )
    return max(0.0, cosine)


def _is_leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _weather_symbol(data):
    for period in ("next_1_hours", "next_6_hours", "next_12_hours"):
        symbol = (
            ((data.get(period) or {}).get("summary") or {}).get("symbol_code")
        )
        if symbol:
            return str(symbol)
    return "unknown"


def _symbol_weight(symbol):
    normalized = str(symbol).lower()
    for suffix in ("_day", "_night", "_polartwilight"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    weights = {
        "clearsky": 1.00,
        "fair": 0.88,
        "partlycloudy": 0.72,
        "cloudy": 0.42,
        "fog": 0.30,
        "lightrain": 0.36,
        "rain": 0.25,
        "heavyrain": 0.16,
        "lightsleet": 0.30,
        "sleet": 0.20,
        "heavysleet": 0.14,
        "lightsnow": 0.32,
        "snow": 0.22,
        "heavysnow": 0.14,
    }
    for key, value in weights.items():
        if normalized.startswith(key):
            return value
    return 0.55


def _weather_at(item):
    data = item.get("data") or {}
    details = ((data.get("instant") or {}).get("details") or {})
    symbol = _weather_symbol(data)
    cloud = _percent_or_none(details.get("cloud_area_fraction"))
    temperature = details.get("air_temperature")
    temperature = 20.0 if temperature is None else float(temperature)
    return {
        "symbol_code": symbol,
        "cloud_area_fraction_percent": cloud,
        "cloud_area_fraction_low_percent": _percent_or_none(
            details.get("cloud_area_fraction_low")
        ),
        "cloud_area_fraction_medium_percent": _percent_or_none(
            details.get("cloud_area_fraction_medium")
        ),
        "cloud_area_fraction_high_percent": _percent_or_none(
            details.get("cloud_area_fraction_high")
        ),
        "air_temperature_c": temperature,
        "shortwave_radiation_w_m2": _nonnegative_or_none(
            details.get("shortwave_radiation")
        ),
        "direct_radiation_w_m2": _nonnegative_or_none(
            details.get("direct_radiation")
        ),
        "diffuse_radiation_w_m2": _nonnegative_or_none(
            details.get("diffuse_radiation")
        ),
        "direct_normal_irradiance_w_m2": _nonnegative_or_none(
            details.get("direct_normal_irradiance")
        ),
    }


def _transmission(weather):
    cloud = weather["cloud_area_fraction_percent"]
    if cloud is None:
        return _symbol_weight(weather["symbol_code"])
    cloud_factor = max(0.08, 1.0 - 0.75 * (cloud / 100.0) ** 3.4)
    # Cloud cover is the numeric basis; symbol moderates edge cases without
    # applying the full weather loss twice.
    return max(
        0.05,
        min(1.0, 0.82 * cloud_factor + 0.18 * _symbol_weight(weather["symbol_code"])),
    )


def _clear_sky_ghi(elevation):
    if elevation <= 0:
        return 0.0
    cos_zenith = max(0.001, math.sin(math.radians(elevation)))
    return 1098.0 * cos_zenith * math.exp(-0.059 / cos_zenith)


def _percent_or_none(value):
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def _nonnegative_or_none(value):
    if value is None:
        return None
    return max(0.0, float(value))


def _irradiance_components(elevation, weather):
    """Return GHI, direct horizontal, DHI and DNI in W/m².

    Archived providers can supply the original radiation fields. Live MET
    Locationforecast currently does not, so the physical clear-sky/cloud model
    remains the documented fallback.
    """
    if elevation <= 0:
        return 0.0, 0.0, 0.0, 0.0

    cos_zenith = max(0.001, math.sin(math.radians(elevation)))
    supplied_ghi = weather.get("shortwave_radiation_w_m2")
    supplied_direct = weather.get("direct_radiation_w_m2")
    supplied_diffuse = weather.get("diffuse_radiation_w_m2")
    supplied_dni = weather.get("direct_normal_irradiance_w_m2")

    if supplied_ghi is not None:
        ghi = supplied_ghi
        if supplied_diffuse is None and supplied_direct is not None:
            supplied_diffuse = max(0.0, ghi - supplied_direct)
        if supplied_direct is None and supplied_diffuse is not None:
            supplied_direct = max(0.0, ghi - supplied_diffuse)
        if supplied_direct is None and supplied_dni is not None:
            supplied_direct = max(0.0, supplied_dni * cos_zenith)
        if supplied_diffuse is None:
            supplied_diffuse = ghi * 0.35
        if supplied_direct is None:
            supplied_direct = max(0.0, ghi - supplied_diffuse)
        if supplied_dni is None:
            supplied_dni = supplied_direct / max(0.08, cos_zenith)
        return ghi, supplied_direct, supplied_diffuse, supplied_dni

    clear_ghi = _clear_sky_ghi(elevation)
    transmission = _transmission(weather)
    ghi = clear_ghi * transmission
    diffuse_fraction = min(0.90, 0.15 + 0.65 * (1.0 - transmission))
    dhi = ghi * diffuse_fraction
    direct_horizontal = max(0.0, ghi - dhi)
    dni = direct_horizontal / max(0.08, cos_zenith)
    return ghi, direct_horizontal, dhi, dni


def _interval_samples(timeseries, issued_at):
    """Expand MET's later 6-hour spacing to hourly samples."""
    parsed = []
    for item in timeseries:
        try:
            parsed.append((_parse_time(item["time"]), item))
        except (KeyError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda row: row[0])

    for index, (start, item) in enumerate(parsed):
        if start < issued_at:
            continue
        if index + 1 < len(parsed):
            hours = (parsed[index + 1][0] - start).total_seconds() / 3600.0
            hours = max(1, min(6, int(round(hours))))
        else:
            hours = 1
        weather = _weather_at(item)
        for offset in range(hours):
            sample_start = start + timedelta(hours=offset)
            if sample_start < issued_at:
                continue
            yield sample_start, 1.0, weather


def _array_power(array, solar, weather, model):
    elevation, sun_azimuth = solar
    if elevation <= float(model.get("minimum_solar_elevation_deg", 0)):
        return 0.0, 0.0

    ghi, direct_horizontal, dhi, dni = _irradiance_components(
        elevation, weather
    )

    incidence = plane_incidence_cosine(
        elevation,
        sun_azimuth,
        array["tilt_deg"],
        array["azimuth_deg"],
    )
    tilt_rad = math.radians(array["tilt_deg"])
    beam = dni * incidence
    diffuse = dhi * (1.0 + math.cos(tilt_rad)) / 2.0
    reflected = (
        ghi
        * model["albedo"]
        * (1.0 - math.cos(tilt_rad))
        / 2.0
    )
    poa = max(0.0, beam + diffuse + reflected)

    temperature_factor = 1.0
    if model.get("temperature_correction_enabled", True):
        noct = model["nominal_operating_cell_temperature_c"]
        cell_temperature = (
            weather["air_temperature_c"] + (noct - 20.0) / 800.0 * poa
        )
        temperature_factor = max(
            0.70,
            min(
                1.15,
                1.0
                + model["temperature_coefficient_per_c"]
                * (cell_temperature - 25.0),
            ),
        )

    power = (
        array["capacity_kwp"]
        * poa
        / 1000.0
        * model["base_performance_ratio"]
        * temperature_factor
    )
    return max(0.0, power), poa


def build_forecast(site_config, met_payload, issued_at=None):
    """Build hourly and daily solar energy forecasts."""
    config = validate_site_config(site_config)
    timeseries = ((met_payload.get("properties") or {}).get("timeseries") or [])
    if not timeseries:
        raise ValueError("MET payload has no properties.timeseries")

    issued_at = issued_at or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    issued_at = issued_at.astimezone(UTC)

    site = config["site"]
    model = config["model"]
    local_zone = ZoneInfo(site["timezone"])
    by_day = {}
    hourly = []
    sun_windows = {}
    total_by_direction = defaultdict(float)
    total_by_array = defaultdict(float)

    for interval_start, interval_hours, weather in _interval_samples(timeseries, issued_at):
        midpoint_utc = interval_start + timedelta(hours=interval_hours / 2.0)
        midpoint_local = midpoint_utc.astimezone(local_zone)
        elevation, sun_azimuth = solar_position(
            midpoint_local,
            site["latitude"],
            site["longitude"],
        )
        ghi, direct_horizontal, dhi, dni = _irradiance_components(
            elevation, weather
        )

        energy_by_direction = defaultdict(float)
        energy_by_array = {}
        poa_by_array = {}
        power_total = 0.0
        for array in config["arrays"]:
            power, poa = _array_power(
                array,
                (elevation, sun_azimuth),
                weather,
                model,
            )
            energy = power * interval_hours
            direction = array["orientation"]
            power_total += power
            energy_by_array[array["id"]] = energy
            poa_by_array[array["id"]] = poa
            energy_by_direction[direction] += energy
            total_by_array[array["id"]] += energy
            total_by_direction[direction] += energy

        expected_kwh = power_total * interval_hours
        local_start = interval_start.astimezone(local_zone)
        date_key = local_start.date().isoformat()
        if date_key not in sun_windows:
            sun_windows[date_key] = sunrise_sunset(
                local_start.date(),
                site["timezone"],
                site["latitude"],
                site["longitude"],
            )
        sunrise, sunset = sun_windows[date_key]
        hourly_item = {
            "time": interval_start.isoformat().replace("+00:00", "Z"),
            "target_time": interval_start.isoformat().replace("+00:00", "Z"),
            "local_time": local_start.isoformat(),
            "target_date": date_key,
            "forecast_issued_at": issued_at.isoformat().replace("+00:00", "Z"),
            "lead_hours": round(
                (interval_start - issued_at).total_seconds() / 3600.0, 3
            ),
            "interval_hours": interval_hours,
            "estimated_power_kw": round(power_total, 3),
            "expected_kwh": round(expected_kwh, 3),
            "expected_kwh_by_direction": _rounded_dict(energy_by_direction, 3),
            "expected_kwh_by_array": _rounded_dict(energy_by_array, 3),
            "solar_elevation_deg": round(elevation, 2),
            "solar_azimuth_deg": round(sun_azimuth, 2),
            "is_day": elevation > 0,
            "sunrise": sunrise,
            "sunset": sunset,
            "ghi_w_m2": round(ghi, 2),
            "direct_radiation_w_m2": round(direct_horizontal, 2),
            "diffuse_radiation_w_m2": round(dhi, 2),
            "direct_normal_irradiance_w_m2": round(dni, 2),
            "poa_w_m2_by_array": _rounded_dict(poa_by_array, 1),
            "air_temperature_c": round(weather["air_temperature_c"], 1),
            "cloud_area_fraction_percent": weather[
                "cloud_area_fraction_percent"
            ],
            "cloud_area_fraction_low_percent": weather[
                "cloud_area_fraction_low_percent"
            ],
            "cloud_area_fraction_medium_percent": weather[
                "cloud_area_fraction_medium_percent"
            ],
            "cloud_area_fraction_high_percent": weather[
                "cloud_area_fraction_high_percent"
            ],
            "symbol_code": weather["symbol_code"],
        }
        hourly.append(hourly_item)

        if date_key not in by_day:
            target_midnight = datetime.combine(
                local_start.date(),
                datetime.min.time(),
                tzinfo=local_zone,
            )
            by_day[date_key] = {
                "date": date_key,
                "target_date": date_key,
                "forecast_issued_at": issued_at.isoformat().replace("+00:00", "Z"),
                "forecast_horizon_hours": round(
                    max(
                        0.0,
                        (
                            target_midnight.astimezone(UTC) - issued_at
                        ).total_seconds()
                        / 3600.0,
                    ),
                    1,
                ),
                "sunrise": sunrise,
                "sunset": sunset,
                "expected_kwh": 0.0,
                "expected_kwh_by_direction": defaultdict(float),
                "expected_kwh_by_array": defaultdict(float),
                "_temperatures": [],
                "_clouds": [],
                "_symbols": [],
            }
        day = by_day[date_key]
        day["expected_kwh"] += expected_kwh
        for key, value in energy_by_direction.items():
            day["expected_kwh_by_direction"][key] += value
        for key, value in energy_by_array.items():
            day["expected_kwh_by_array"][key] += value
        # Daily weather fields describe production hours, not the longer
        # night-time period which otherwise tends to dominate the symbol count.
        if elevation > 0:
            day["_temperatures"].append(weather["air_temperature_c"])
            if weather["cloud_area_fraction_percent"] is not None:
                day["_clouds"].append(weather["cloud_area_fraction_percent"])
            day["_symbols"].append(weather["symbol_code"])

    uncertainty = model["uncertainty_percent"] / 100.0
    daily = []
    for date_key in sorted(by_day):
        day = by_day[date_key]
        expected = day["expected_kwh"]
        temperatures = day.pop("_temperatures")
        clouds = day.pop("_clouds")
        symbols = day.pop("_symbols")
        day["expected_kwh"] = round(expected, 1)
        day["expected_kwh_by_direction"] = _rounded_dict(
            day["expected_kwh_by_direction"], 1
        )
        day["expected_kwh_by_array"] = _rounded_dict(
            day["expected_kwh_by_array"], 1
        )
        day["uncertainty"] = {
            "p10_kwh": round(max(0.0, expected * (1.0 - uncertainty)), 1),
            "p50_kwh": round(expected, 1),
            "p90_kwh": round(expected * (1.0 + uncertainty), 1),
        }
        day["weather"] = {
            "air_temperature_mean_c": _mean_or_none(temperatures),
            "cloud_area_fraction_mean_percent": _mean_or_none(clouds),
            "dominant_symbol_code": (
                Counter(symbols).most_common(1)[0][0] if symbols else "unknown"
            ),
        }
        day["model_version"] = MODEL_VERSION
        daily.append(day)

    expected_total = sum(item["expected_kwh"] for item in daily)
    best = max(daily, key=lambda item: item["expected_kwh"]) if daily else None
    worst = min(daily, key=lambda item: item["expected_kwh"]) if daily else None
    config_fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "weather_error": None,
        "site": {
            "id": site["id"],
            "name": site.get("name", site["id"]),
            "latitude": site["latitude"],
            "longitude": site["longitude"],
            "timezone": site["timezone"],
        },
        "system": {
            "pv_mode": (config.get("pv") or {}).get("mode", "installed"),
            "panel_count": sum(
                int(item.get("module_count") or 0) for item in config["arrays"]
            ),
            "installed_capacity_kwp": round(
                sum(item["capacity_kwp"] for item in config["arrays"]), 3
            ),
        },
        "forecast_summary": {
            "generated_at": issued_at.isoformat().replace("+00:00", "Z"),
            "forecast_issued_at": issued_at.isoformat().replace("+00:00", "Z"),
            "model_version": MODEL_VERSION,
            "configuration_fingerprint": config_fingerprint,
            "days": len(daily),
            "expected_kwh_total": round(expected_total, 1),
            "best_day": _day_summary(best),
            "worst_day": _day_summary(worst),
        },
        "daily_forecast": daily,
        "hourly_forecast": hourly,
        "expected_kwh_by_direction": _rounded_dict(total_by_direction, 1),
        "expected_kwh_by_array": _rounded_dict(total_by_array, 1),
        "panel_configuration": [
            {
                "id": item["id"],
                "module_count": item.get("module_count"),
                "module_power_wp": item.get("module_power_wp"),
                "capacity_kwp": item["capacity_kwp"],
                "tilt_deg": item["tilt_deg"],
                "azimuth_deg": item["azimuth_deg"],
                "direction": item["orientation"],
            }
            for item in config["arrays"]
        ],
    }


def _rounded_dict(values, digits):
    return {key: round(float(value), digits) for key, value in sorted(values.items())}


def _mean_or_none(values):
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _day_summary(day):
    if not day:
        return None
    return {
        "date": day["date"],
        "expected_kwh": day["expected_kwh"],
    }
