"""Archived weather-run adapters for reproducible forecast backfill.

Open-Meteo Single Runs is the primary archive transport. MET Norway MEPS
OPeNDAP can enrich Norwegian runs with cloud layers that are currently null in
Open-Meteo's ``metno_nordic`` archive. No credentials are used by either
adapter.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
OPEN_METEO_SINGLE_RUNS = "https://single-runs-api.open-meteo.com/v1/forecast"
MEPS_DODS_ROOT = "https://thredds.met.no/thredds/dodsC/meps25epsarchive"
ARCHIVE_MODEL = "metno_nordic"
HOURLY_FIELDS = (
    "temperature_2m",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "is_day",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
)


def comparison_cutoff(target_date, timezone_name):
    """Return the documented 18:00 local cutoff on the previous day."""
    target = (
        target_date
        if isinstance(target_date, date)
        else date.fromisoformat(str(target_date))
    )
    return datetime.combine(
        target - timedelta(days=1),
        time(18, 0),
        tzinfo=ZoneInfo(timezone_name),
    ).astimezone(UTC)


def select_model_run(target_date, timezone_name, availability_delay_hours=2):
    """Select the latest three-hour MEPS run available by the cutoff."""
    cutoff = comparison_cutoff(target_date, timezone_name)
    candidate = cutoff - timedelta(hours=float(availability_delay_hours))
    cycle_hour = candidate.hour - candidate.hour % 3
    return candidate.replace(
        hour=cycle_hour, minute=0, second=0, microsecond=0
    )


class OpenMeteoSingleRunsClient:
    def __init__(self, opener=None, timeout=60):
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def fetch(self, latitude, longitude, model_run_at, forecast_days=7):
        run = _as_utc(model_run_at)
        parameters = {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "models": ARCHIVE_MODEL,
            "run": run.strftime("%Y-%m-%dT%H:%M"),
            "hourly": ",".join(HOURLY_FIELDS),
            "forecast_days": int(forecast_days),
            "timezone": "UTC",
        }
        url = f"{OPEN_METEO_SINGLE_RUNS}?{urllib.parse.urlencode(parameters)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "LocalSolarForecast/0.2",
            },
        )
        with self.opener(request, timeout=self.timeout) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError(str(payload.get("reason") or payload["error"]))
        return payload, {
            "provider": "open_meteo_single_runs",
            "model": ARCHIVE_MODEL,
            "model_run_at": _iso(run),
            "request": {
                "endpoint": OPEN_METEO_SINGLE_RUNS,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "models": ARCHIVE_MODEL,
                "run": run.strftime("%Y-%m-%dT%H:%M"),
                "hourly": list(HOURLY_FIELDS),
            },
        }


def open_meteo_to_engine_payload(payload, layer_enrichment=None):
    """Convert Open-Meteo hourly arrays to the engine's weather structure."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    enrichment = layer_enrichment or {}
    timeseries = []
    for index, timestamp in enumerate(times):
        normalized = _iso(_parse_time(timestamp))
        layer = enrichment.get(normalized, {})
        details = {
            "air_temperature": _at(hourly, "temperature_2m", index),
            "cloud_area_fraction": _at(hourly, "cloud_cover", index),
            "cloud_area_fraction_low": _first_not_none(
                layer.get("cloud_cover_low_percent"),
                _at(hourly, "cloud_cover_low", index),
            ),
            "cloud_area_fraction_medium": _first_not_none(
                layer.get("cloud_cover_medium_percent"),
                _at(hourly, "cloud_cover_mid", index),
            ),
            "cloud_area_fraction_high": _first_not_none(
                layer.get("cloud_cover_high_percent"),
                _at(hourly, "cloud_cover_high", index),
            ),
            "shortwave_radiation": _at(hourly, "shortwave_radiation", index),
            "direct_radiation": _at(hourly, "direct_radiation", index),
            "diffuse_radiation": _at(hourly, "diffuse_radiation", index),
            "direct_normal_irradiance": _at(
                hourly, "direct_normal_irradiance", index
            ),
        }
        details = {key: value for key, value in details.items() if value is not None}
        timeseries.append(
            {
                "time": normalized,
                "data": {
                    "instant": {"details": details},
                    "next_1_hours": {
                        "summary": {
                            "symbol_code": _cloud_symbol(
                                details.get("cloud_area_fraction"),
                                bool(_at(hourly, "is_day", index)),
                            )
                        }
                    },
                },
            }
        )
    return {"properties": {"timeseries": timeseries}}


class MepsThreddsClient:
    """Read one MEPS grid point from the public deterministic surface archive."""

    # Fixed MEPS 2.5 km Lambert grid definition published in the dataset.
    EARTH_RADIUS_M = 6_371_000.0
    STANDARD_PARALLEL_DEG = 63.3
    CENTRAL_MERIDIAN_DEG = 15.0
    ORIGIN_LATITUDE_DEG = 63.3
    X0_M = -1_060_084.0
    Y0_M = -1_332_517.9
    GRID_STEP_M = 2_500.0

    VARIABLES = {
        "air_temperature_2m": "temperature_2m_c",
        "cloud_area_fraction": "cloud_cover_total_percent",
        "low_type_cloud_area_fraction": "cloud_cover_low_percent",
        "medium_type_cloud_area_fraction": "cloud_cover_medium_percent",
        "high_type_cloud_area_fraction": "cloud_cover_high_percent",
        "surface_downwelling_shortwave_flux_in_air": "ghi_w_m2",
    }

    def __init__(self, opener=None, timeout=90):
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def fetch_point(self, latitude, longitude, model_run_at, hours=67):
        run = _as_utc(model_run_at)
        x_index, y_index = self.grid_index(latitude, longitude)
        path = (
            f"{run:%Y/%m/%d}/"
            f"meps_det_sfc_{run:%Y%m%dT%H}Z.ncml"
        )
        slices = ",".join(
            f"{name}[0:1:{int(hours) - 1}][0][{y_index}][{x_index}]"
            for name in self.VARIABLES
        )
        slices += f",time[0:1:{int(hours) - 1}]"
        url = f"{MEPS_DODS_ROOT}/{path}.ascii?{slices}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "LocalSolarForecast/0.2"},
        )
        with self.opener(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
        fields = {
            name: _parse_dods_values(body, name)
            for name in (*self.VARIABLES, "time")
        }
        if not fields["time"]:
            raise RuntimeError("MEPS OPeNDAP response contained no time values")
        result = {}
        for index, seconds in enumerate(fields["time"]):
            target = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                seconds=float(seconds)
            )
            item = {}
            for source_name, target_name in self.VARIABLES.items():
                value = _list_at(fields[source_name], index)
                if value is None or abs(value) > 1e20:
                    value = None
                elif source_name == "air_temperature_2m":
                    value = float(value) - 273.15
                elif "cloud" in source_name:
                    value = max(0.0, min(100.0, float(value) * 100.0))
                else:
                    value = max(0.0, float(value))
                item[target_name] = value
            result[_iso(target)] = item
        return result, {
            "provider": "met_no_thredds_meps",
            "model": "meps_det_sfc",
            "model_run_at": _iso(run),
            "request": {
                "dataset": path,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "x_index": x_index,
                "y_index": y_index,
                "hours": int(hours),
                "variables": list(self.VARIABLES),
            },
            "payload": result,
        }

    @classmethod
    def grid_index(cls, latitude, longitude):
        x_value, y_value = cls._project(latitude, longitude)
        return (
            int(round((x_value - cls.X0_M) / cls.GRID_STEP_M)),
            int(round((y_value - cls.Y0_M) / cls.GRID_STEP_M)),
        )

    @classmethod
    def _project(cls, latitude, longitude):
        phi = math.radians(float(latitude))
        lam = math.radians(float(longitude))
        phi0 = math.radians(cls.ORIGIN_LATITUDE_DEG)
        phi1 = math.radians(cls.STANDARD_PARALLEL_DEG)
        lam0 = math.radians(cls.CENTRAL_MERIDIAN_DEG)
        n = math.sin(phi1)
        f = (
            math.cos(phi1)
            * math.tan(math.pi / 4.0 + phi1 / 2.0) ** n
            / n
        )
        rho = (
            cls.EARTH_RADIUS_M
            * f
            / math.tan(math.pi / 4.0 + phi / 2.0) ** n
        )
        rho0 = (
            cls.EARTH_RADIUS_M
            * f
            / math.tan(math.pi / 4.0 + phi0 / 2.0) ** n
        )
        theta = n * (lam - lam0)
        return rho * math.sin(theta), rho0 - rho * math.cos(theta)


def make_raw_source(open_meteo_payload, open_meteo_metadata, meps=None):
    payload = {"open_meteo": open_meteo_payload}
    request = {"open_meteo": open_meteo_metadata["request"]}
    if meps:
        payload["meps"] = meps.get("payload")
        request["meps"] = meps.get("request")
    return {
        "provider": "open_meteo_single_runs+met_no_meps" if meps else "open_meteo_single_runs",
        "model": ARCHIVE_MODEL,
        "model_run_at": open_meteo_metadata["model_run_at"],
        "retrieved_at": _iso(datetime.now(UTC)),
        "request": request,
        "payload": payload,
    }


def _parse_dods_values(body, variable):
    match = re.search(
        rf"(?m)^{re.escape(variable)}(?:\[[^\n=]*\])?\s*=\s*([^;]+);",
        body,
    )
    if not match:
        return []
    return [
        float(value)
        for value in re.findall(
            r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?",
            match.group(1),
        )
    ]


def _cloud_symbol(cloud, is_day):
    suffix = "_day" if is_day else "_night"
    if cloud is None:
        return f"partlycloudy{suffix}"
    if float(cloud) < 15:
        return f"clearsky{suffix}"
    if float(cloud) < 55:
        return f"fair{suffix}"
    if float(cloud) < 85:
        return f"partlycloudy{suffix}"
    return f"cloudy{suffix}"


def _at(mapping, key, index):
    return _list_at(mapping.get(key) or [], index)


def _list_at(values, index):
    return values[index] if index < len(values) else None


def _first_not_none(*values):
    return next((value for value in values if value is not None), None)


def _parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_time(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value):
    return _as_utc(value).isoformat().replace("+00:00", "Z")
