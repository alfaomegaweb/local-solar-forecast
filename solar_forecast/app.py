#!/usr/bin/env python3
"""HTTP service for the Local Solar Forecast Home Assistant app."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import yaml
except ModuleNotFoundError:  # Allows JSON-only local tests without PyYAML.
    yaml = None

from forecast_engine import MODEL_VERSION, build_forecast, validate_site_config
from history_store import ForecastHistory
from calibration import apply_calibration, fit_calibration


UTC = timezone.utc
LOG = logging.getLogger("solar_forecast")
DEFAULT_OPTIONS = {
    "site_config_path": "/config/solar_forecast/site.yaml",
    "refresh_minutes": 30,
    "history_days": 730,
    "hourly_empirical_lookback_days": 90,
    "listen_port": 8099,
    "log_level": "info",
    "legacy_history_path": (
        "/config/solar_forecast/legacy-forecast-snapshots.ndjson"
    ),
}


def load_options():
    options = dict(DEFAULT_OPTIONS)
    path = Path(os.environ.get("SOLAR_FORECAST_OPTIONS", "/data/options.json"))
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            options.update(json.load(handle))
    if os.environ.get("SOLAR_FORECAST_SITE_CONFIG"):
        options["site_config_path"] = os.environ["SOLAR_FORECAST_SITE_CONFIG"]
    return options


def load_site_config(path):
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        if config_path.suffix.lower() == ".json":
            loaded = json.load(handle)
        elif yaml is not None:
            loaded = yaml.safe_load(handle)
        else:
            raise RuntimeError(
                "PyYAML is required for YAML site files; JSON is also supported"
            )
    return validate_site_config(loaded)


class ServiceState:
    def __init__(self, options):
        self.options = options
        self.lock = threading.RLock()
        self.forecast = None
        self.last_error = None
        self.last_attempt_at = None
        self.last_success_at = None
        self.last_empirical_success_at = None
        self.last_empirical_error = None
        self.legacy_import = None
        self.stop_event = threading.Event()
        data_dir = Path(os.environ.get("SOLAR_FORECAST_DATA_DIR", "/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = data_dir / "met-locationforecast.json"
        self.history = ForecastHistory(data_dir / "forecast-history.sqlite")
        self.config = load_site_config(options["site_config_path"])
        legacy_path = Path(str(options.get("legacy_history_path") or ""))
        if legacy_path.is_file():
            self.legacy_import = self.history.import_legacy_ndjson(legacy_path)
            LOG.info("Legacy forecast import: %s", self.legacy_import)

    def refresh(self):
        now = datetime.now(UTC)
        with self.lock:
            self.last_attempt_at = _iso(now)
        try:
            payload, source = self._fetch_weather()
            forecast = build_forecast(self.config, payload, issued_at=now)
            forecast["weather_source"] = {
                "provider": "met_no_locationforecast",
                "source": source,
            }
            model_run_at = (
                ((payload.get("properties") or {}).get("meta") or {}).get(
                    "updated_at"
                )
            )
            forecast["forecast_summary"]["model_run_at"] = model_run_at
            forecast["forecast_summary"][
                "issued_at_semantics"
            ] = "forecast_generated_at_after_retrieval"
            forecast["forecast_summary"]["source"] = "met_no_locationforecast"
            calibration_options = self.config.get("calibration") or {}
            calibration = (
                self.history.active_calibration(self.config["site"]["id"])
                if calibration_options.get("enabled", True)
                else None
            )
            apply_calibration(forecast, calibration)
            run_id, inserted = self.history.append(
                forecast,
                raw_source={
                    "provider": "met_no_locationforecast",
                    "model": "locationforecast_compact",
                    "model_run_at": model_run_at,
                    "retrieved_at": _iso(now),
                    "request": {
                        "endpoint": (
                            (self.config.get("weather") or {}).get(
                                "provider_url"
                            )
                            or (
                                "https://api.met.no/weatherapi/"
                                "locationforecast/2.0/compact"
                            )
                        ),
                        "latitude": self.config["site"]["latitude"],
                        "longitude": self.config["site"]["longitude"],
                        "cache_status": source,
                    },
                    "payload": payload,
                },
            )
            forecast["snapshot_storage"] = {
                "run_id": run_id,
                "inserted": inserted,
                "database": "forecast-history.sqlite",
                "append_only": True,
                "comparison_basis": (
                    "latest snapshot at or before 18:00 local time the previous day; "
                    "fallback latest before target midnight"
                ),
            }
            self.refresh_actuals()
            self.refresh_hourly_actuals()
            learned = (
                fit_calibration(
                    self.history,
                    self.config["site"]["id"],
                    self.config["site"]["timezone"],
                    minimum_samples=calibration_options.get(
                        "minimum_training_hours", 48
                    ),
                    factor_min=calibration_options.get("factor_min", 0.70),
                    factor_max=calibration_options.get("factor_max", 1.30),
                    minimum_mae_improvement_percent=calibration_options.get(
                        "minimum_mae_improvement_percent", 2
                    ),
                )
                if calibration_options.get("enabled", True)
                else None
            )
            if learned:
                calibration_id, calibration_inserted = (
                    self.history.append_calibration(learned)
                )
                LOG.info(
                    "Calibration evaluated: id=%s accepted=%s inserted=%s "
                    "samples=%s mae_before=%s mae_after=%s",
                    calibration_id[:12],
                    learned["accepted"],
                    calibration_inserted,
                    learned["sample_count"],
                    learned["mae_before_kwh"],
                    learned["mae_after_kwh"],
                )
            with self.lock:
                self.forecast = forecast
                self.last_error = None
                self.last_success_at = _iso(datetime.now(UTC))
            LOG.info(
                "Forecast updated: site=%s days=%s total_kwh=%s snapshot=%s",
                forecast["site"]["id"],
                forecast["forecast_summary"]["days"],
                forecast["forecast_summary"]["expected_kwh_total"],
                run_id[:12],
            )
        except Exception as exc:
            LOG.exception("Forecast refresh failed")
            with self.lock:
                self.last_error = str(exc)

    def refresh_actuals(self):
        statistic_ids = (
            ((self.config.get("measurements") or {}).get("solar_energy") or {})
            .get("statistic_entities", [])
        )
        if not statistic_ids:
            return
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            self.last_empirical_error = "SUPERVISOR_TOKEN is unavailable"
            return

        now = datetime.now(UTC)
        lookback = max(30, int(self.options["history_days"]))
        payload = {
            "statistic_ids": statistic_ids,
            "start_time": _iso(now - timedelta(days=lookback)),
            "end_time": _iso(now),
            "period": "day",
            "types": ["change"],
            "units": {},
        }
        request = urllib.request.Request(
            (
                "http://supervisor/core/api/services/"
                "recorder/get_statistics?return_response"
            ),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                response_payload = json.load(response)
            statistics = _find_statistics(response_payload, statistic_ids)
            zone = ZoneInfo(self.config["site"]["timezone"])
            today = datetime.now(zone).date().isoformat()
            by_date = {}
            for entity_id in statistic_ids:
                for point in statistics.get(entity_id, []):
                    if not isinstance(point, dict):
                        continue
                    try:
                        target_date = (
                            datetime.fromisoformat(
                                str(point["start"]).replace("Z", "+00:00")
                            )
                            .astimezone(zone)
                            .date()
                            .isoformat()
                        )
                        change = max(0.0, float(point["change"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if target_date >= today:
                        continue
                    day = by_date.setdefault(
                        target_date, {"total": 0.0, "entities": {}}
                    )
                    day["total"] += change
                    day["entities"][entity_id] = change
            observed_at = _iso(now)
            for target_date, item in by_date.items():
                self.history.append_actual(
                    self.config["site"]["id"],
                    target_date,
                    item["total"],
                    "home_assistant_recorder_statistics",
                    details={"entities_kwh": item["entities"]},
                    observed_at=observed_at,
                )
            self.last_empirical_success_at = observed_at
            self.last_empirical_error = None
            LOG.info("Empirical production updated: days=%s", len(by_date))
        except Exception as exc:
            self.last_empirical_error = str(exc)
            LOG.warning("Empirical production refresh failed: %s", exc)

    def refresh_hourly_actuals(self):
        """Capture hourly PV energy and outdoor temperature from HA Recorder."""
        measurements = self.config.get("measurements") or {}
        solar_ids = (
            (measurements.get("solar_energy") or {}).get(
                "statistic_entities", []
            )
            or (measurements.get("solar_energy") or {}).get("entities", [])
        )
        temperature_id = measurements.get(
            "outdoor_temperature_statistic_entity"
        )
        statistic_ids = list(solar_ids)
        if temperature_id:
            statistic_ids.append(temperature_id)
        if not statistic_ids:
            return
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return
        now = datetime.now(UTC)
        lookback = max(
            2,
            min(
                int(self.options.get("hourly_empirical_lookback_days", 90)),
                int(self.options["history_days"]),
            ),
        )
        payload = {
            "statistic_ids": statistic_ids,
            "start_time": _iso(now - timedelta(days=lookback)),
            "end_time": _iso(now),
            "period": "hour",
            "types": ["change", "mean"],
            "units": {},
        }
        request = urllib.request.Request(
            (
                "http://supervisor/core/api/services/"
                "recorder/get_statistics?return_response"
            ),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_payload = json.load(response)
            statistics = _find_statistics(response_payload, statistic_ids)
            by_time = {}
            for entity_id in solar_ids:
                for point in statistics.get(entity_id, []):
                    try:
                        target_time = _iso(
                            datetime.fromisoformat(
                                str(point["start"]).replace("Z", "+00:00")
                            )
                        )
                        change = max(0.0, float(point["change"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if _parse_utc(target_time) + timedelta(hours=1) > now:
                        continue
                    item = by_time.setdefault(
                        target_time,
                        {"pv_kwh": 0.0, "temperature_c": None, "entities": {}},
                    )
                    item["pv_kwh"] += change
                    item["entities"][entity_id] = change
            if temperature_id:
                for point in statistics.get(temperature_id, []):
                    try:
                        target_time = _iso(
                            datetime.fromisoformat(
                                str(point["start"]).replace("Z", "+00:00")
                            )
                        )
                        temperature = float(point["mean"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    item = by_time.setdefault(
                        target_time,
                        {"pv_kwh": None, "temperature_c": None, "entities": {}},
                    )
                    item["temperature_c"] = temperature
            observed_at = _iso(now)
            for target_time, item in by_time.items():
                self.history.append_hourly_actual(
                    self.config["site"]["id"],
                    target_time,
                    actual_pv_kwh=item["pv_kwh"],
                    actual_temperature_c=item["temperature_c"],
                    source="home_assistant_recorder_hourly_statistics",
                    details={"solar_entities_kwh": item["entities"]},
                    observed_at=observed_at,
                )
            LOG.info("Hourly empirical observations updated: hours=%s", len(by_time))
        except Exception as exc:
            self.last_empirical_error = str(exc)
            LOG.warning("Hourly empirical refresh failed: %s", exc)

    def import_empirics(self, payload):
        """Append externally measured daily/hourly production observations."""
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        configured_site_id = str(self.config["site"]["id"])
        site_id = str(payload.get("site_id") or configured_site_id)
        if site_id != configured_site_id:
            raise ValueError("site_id does not match the configured site")
        source = str(payload.get("source") or "").strip()
        if not source:
            raise ValueError("source is required")
        observed_at = payload.get("observed_at") or _iso(datetime.now(UTC))
        _parse_utc(str(observed_at))
        daily = payload.get("daily") or []
        hourly = payload.get("hourly") or []
        if not isinstance(daily, list) or not isinstance(hourly, list):
            raise ValueError("daily and hourly must be arrays")
        if not daily and not hourly:
            raise ValueError("at least one daily or hourly observation is required")
        if len(daily) > 3660 or len(hourly) > 20000:
            raise ValueError("empirical import is too large")

        result = {
            "site_id": site_id,
            "source": source,
            "observed_at": str(observed_at),
            "daily_received": len(daily),
            "daily_inserted": 0,
            "hourly_received": len(hourly),
            "hourly_inserted": 0,
            "append_only": True,
        }
        for item in daily:
            if not isinstance(item, dict):
                raise ValueError("each daily observation must be an object")
            target_date = str(item.get("target_date") or "")
            try:
                datetime.strptime(target_date, "%Y-%m-%d")
                actual = float(item["actual_kwh_total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid daily observation") from exc
            if not math.isfinite(actual) or actual < 0:
                raise ValueError(
                    "daily actual_kwh_total must be finite and non-negative"
                )
            _, inserted = self.history.append_actual(
                site_id,
                target_date,
                actual,
                source,
                details=item.get("details") or {},
                observed_at=str(observed_at),
            )
            result["daily_inserted"] += int(inserted)

        for item in hourly:
            if not isinstance(item, dict):
                raise ValueError("each hourly observation must be an object")
            target_time = str(item.get("target_time") or "")
            pv = item.get("actual_pv_kwh")
            temperature = item.get("actual_temperature_c")
            if pv is not None:
                pv = float(pv)
                if not math.isfinite(pv) or pv < 0:
                    raise ValueError(
                        "hourly actual_pv_kwh must be finite and non-negative"
                    )
            if temperature is not None:
                temperature = float(temperature)
                if not math.isfinite(temperature):
                    raise ValueError("hourly temperature must be finite")
            _, inserted = self.history.append_hourly_actual(
                site_id,
                target_time,
                actual_pv_kwh=pv,
                actual_temperature_c=temperature,
                source=source,
                details=item.get("details") or {},
                observed_at=str(observed_at),
            )
            result["hourly_inserted"] += int(inserted)
        return result

    def _fetch_weather(self):
        site = self.config["site"]
        weather = self.config.get("weather") or {}
        base_url = weather.get(
            "provider_url",
            "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        )
        query = urllib.parse.urlencode(
            {"lat": site["latitude"], "lon": site["longitude"]}
        )
        url = f"{base_url}?{query}"
        user_agent = weather.get(
            "user_agent",
            "LocalSolarForecast/0.1 (Home Assistant app)",
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            temporary = self.cache_path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            temporary.replace(self.cache_path)
            return payload, "live"
        except (OSError, urllib.error.URLError, ValueError):
            if self.cache_path.is_file():
                LOG.warning("MET request failed; using last local weather cache")
                with self.cache_path.open("r", encoding="utf-8") as handle:
                    return json.load(handle), "cache"
            raise

    def status(self):
        with self.lock:
            forecast = self.forecast
            return {
                "ok": forecast is not None and self.last_error is None,
                "model_version": MODEL_VERSION,
                "site_id": self.config["site"]["id"],
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_error": self.last_error,
                "forecast_available": forecast is not None,
                "last_empirical_success_at": self.last_empirical_success_at,
                "last_empirical_error": self.last_empirical_error,
                "legacy_import": self.legacy_import,
                "active_calibration": self.history.active_calibration(
                    self.config["site"]["id"]
                ),
            }

    def current_forecast(self):
        with self.lock:
            return self.forecast


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalSolarForecast/0.1"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._html(_dashboard_html(self.server.state))
            return
        if path == "/health":
            status = self.server.state.status()
            self._json(status, 200 if status["forecast_available"] else 503)
            return
        if path in ("/forecast", "/api/forecast"):
            forecast = self.server.state.current_forecast()
            if forecast is None:
                self._json(
                    {
                        "weather_error": self.server.state.status()["last_error"]
                        or "Forecast has not completed yet"
                    },
                    503,
                )
            else:
                self._json(forecast)
            return
        if path == "/api/history":
            target_date = _first(query, "target_date")
            site_id = _first(
                query, "site_id", self.server.state.config["site"]["id"]
            )
            limit = _integer(_first(query, "limit", "200"), 200, 1, 5000)
            rows = self.server.state.history.list_daily(
                site_id=site_id,
                target_date=target_date,
                limit=limit,
            )
            response = {
                "site_id": site_id,
                "target_date": target_date,
                "count": len(rows),
                "snapshots": rows,
            }
            if target_date:
                response["comparison_baseline"] = (
                    self.server.state.history.comparison_baseline(
                        site_id,
                        target_date,
                        self.server.state.config["site"]["timezone"],
                    )
                )
            self._json(response)
            return
        if path == "/api/empirics":
            site_id = _first(
                query, "site_id", self.server.state.config["site"]["id"]
            )
            limit = _integer(_first(query, "limit", "730"), 730, 1, 5000)
            timezone_name = self.server.state.config["site"]["timezone"]
            comparisons = self.server.state.history.comparisons(
                site_id, timezone_name, limit
            )
            self._json(
                {
                    "site_id": site_id,
                    "comparison_basis": (
                        "latest snapshot at or before 18:00 local time the "
                        "previous day; fallback latest before target midnight"
                    ),
                    "count": len(comparisons),
                    "metrics": self.server.state.history.empirical_metrics(
                        site_id, timezone_name, limit
                    ),
                    "days": comparisons,
                }
            )
            return
        if path == "/api/hourly-comparison":
            site_id = _first(
                query, "site_id", self.server.state.config["site"]["id"]
            )
            target_date = _first(query, "target_date")
            if not target_date:
                self._json(
                    {
                        "error": "target_date_required",
                        "example": "/api/hourly-comparison?target_date=2026-07-17",
                    },
                    400,
                )
                return
            try:
                result = self.server.state.history.hourly_comparisons(
                    site_id,
                    target_date,
                    self.server.state.config["site"]["timezone"],
                )
            except ValueError:
                self._json({"error": "invalid_target_date"}, 400)
                return
            self._json(result)
            return
        self._json({"error": "not_found", "path": path}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/api/refresh":
            threading.Thread(
                target=self.server.state.refresh,
                name="manual-refresh",
                daemon=True,
            ).start()
            self._json({"accepted": True}, 202)
            return
        if path == "/api/empirics/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 5 * 1024 * 1024:
                    raise ValueError("invalid Content-Length")
                payload = json.loads(self.rfile.read(length))
                result = self.server.state.import_empirics(payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                self._json(
                    {
                        "error": "invalid_empirical_import",
                        "message": str(exc),
                    },
                    400,
                )
                return
            self._json(result, 201)
            return
        self._json({"error": "not_found", "path": path}, 404)

    def log_message(self, message, *args):
        LOG.info("%s - %s", self.address_string(), message % args)

    def _json(self, payload, status=200):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            separators=(",", ": "),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, status=200):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class ForecastServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, state):
        super().__init__(address, handler)
        self.state = state


def _background_loop(state):
    while not state.stop_event.wait(
        max(5, int(state.options["refresh_minutes"])) * 60
    ):
        state.refresh()


def _dashboard_html(state):
    status = state.status()
    forecast = state.current_forecast()
    summary = (forecast or {}).get("forecast_summary") or {}
    days = (forecast or {}).get("daily_forecast") or []
    comparisons = state.history.comparisons(
        state.config["site"]["id"], state.config["site"]["timezone"], 730
    )
    metrics = state.history.empirical_metrics(
        state.config["site"]["id"], state.config["site"]["timezone"], 730
    )
    hourly_result = (
        state.history.hourly_comparisons(
            state.config["site"]["id"],
            comparisons[0]["target_date"],
            state.config["site"]["timezone"],
        )
        if comparisons
        else {"hours": [], "target_date": None}
    )
    rows = "".join(
        (
            f"<tr><td>{item['target_date']}</td>"
            f"<td>{item['expected_kwh']:.1f}</td>"
            f"<td>{item['weather']['dominant_symbol_code']}</td>"
            f"<td>{item['forecast_horizon_hours']:.1f}</td></tr>"
        )
        for item in days
    )
    empirical_rows = "".join(
        (
            f"<tr><td>{item['target_date']}</td>"
            f"<td>{_number(item['forecast_kwh'])}</td>"
            f"<td>{_number(item['actual_kwh'])}</td>"
            f"<td class='{_deviation_class(item['deviation_kwh'])}'>"
            f"{_signed_number(item['deviation_kwh'])}</td>"
            f"<td class='{_deviation_class(item['deviation_percent'])}'>"
            f"{_signed_number(item['deviation_percent'])} %</td>"
            f"<td>{_escape(item['forecast_issued_at'] or '–')}</td></tr>"
        )
        for item in comparisons
    )
    hourly_rows = "".join(
        (
            f"<tr><td>{_escape(item['target_time'])}</td>"
            f"<td>{_number(item['forecast_kwh'])}</td>"
            f"<td>{_number(item['actual_kwh'])}</td>"
            f"<td>{_signed_number(item['deviation_kwh'])}</td>"
            f"<td>{_number(item['cloud_cover_total_percent'])}</td>"
            f"<td>{_number(item['forecast_temperature_c'])}</td>"
            f"<td>{_number(item['actual_temperature_c'])}</td>"
            f"<td>{_number(item['lead_hours'])}</td>"
            f"<td>{_escape(item['source'])}</td></tr>"
        )
        for item in hourly_result["hours"]
    )
    error = (
        f"<p class='error'>{_escape(status['last_error'])}</p>"
        if status["last_error"]
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Local Solar Forecast</title>
  <style>
    :root {{ color-scheme: light dark; font: 16px system-ui, sans-serif; }}
    body {{ margin: 0; padding: 24px; background: #101820; color: #eef4f7; }}
    main {{ max-width: 920px; margin: auto; }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
    .card, table {{ background:#182630; border:1px solid #334752; border-radius:12px; }}
    .card {{ padding:16px; }} .value {{ font-size:1.6rem; margin-top:8px; }}
    table {{ width:100%; margin-top:18px; border-collapse:collapse; overflow:hidden; }}
    th,td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #334752; }}
    .ok {{ color:#7ee787; }} .error {{ color:#ff7b72; }}
    .positive {{ color:#7ee787; }} .negative {{ color:#ff7b72; }}
    h2 {{ margin-top:32px; }}
    a {{ color:#78c7ff; }}
  </style>
</head>
<body><main>
  <h1>Local Solar Forecast</h1>
  <p class="{'ok' if status['ok'] else 'error'}">
    {'Running / Kjører' if status['forecast_available'] else 'Waiting / Venter'}
  </p>
  {error}
  <section class="cards">
    <div class="card">Site / Anlegg<div class="value">{_escape(status['site_id'])}</div></div>
    <div class="card">Forecast / Prognose<div class="value">{summary.get('expected_kwh_total', '–')} kWh</div></div>
    <div class="card">Model / Modell<div class="value">{MODEL_VERSION}</div></div>
    <div class="card">Empirical days / Empiridøgn<div class="value">{metrics['days']}</div></div>
    <div class="card">MAE<div class="value">{_number(metrics['mae_kwh'])} kWh</div></div>
    <div class="card">Bias<div class="value">{_signed_number(metrics['bias_kwh'])} kWh</div></div>
  </section>
  <h2>Forecast ahead / Prognose fremover</h2>
  <table>
    <thead><tr><th>Date / Dato</th><th>kWh</th><th>Weather / Vær</th><th>Horizon h</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Forecast versus actual / Prognose mot faktisk</h2>
  <p>Fast comparison: latest forecast at or before 18:00 the previous day.</p>
  <table>
    <thead><tr><th>Date / Dato</th><th>Forecast / Prognose</th>
      <th>Actual / Faktisk</th><th>Deviation kWh / Avvik</th>
      <th>Deviation % / Avvik</th><th>Issued / Utstedt</th></tr></thead>
    <tbody>{empirical_rows}</tbody>
  </table>
  <h2>Hourly daylight verification / Timevis dagslysverifikasjon</h2>
  <p>{_escape(hourly_result.get('target_date') or 'No completed day / Ingen avsluttet dag')}</p>
  <table>
    <thead><tr><th>Target / Måltime</th><th>Forecast kWh</th>
      <th>Actual kWh</th><th>Deviation kWh</th><th>Cloud %</th>
      <th>Forecast °C</th><th>Actual °C</th><th>Lead h</th>
      <th>Source / Kilde</th></tr></thead>
    <tbody>{hourly_rows}</tbody>
  </table>
  <p><a href="./api/forecast">JSON API</a> ·
    <a href="./api/history">Snapshots / Prognosehistorikk</a> ·
    <a href="./api/empirics">Empirics / Empiri</a> ·
    <a href="./api/hourly-comparison?target_date={_escape(hourly_result.get('target_date') or '')}">Hourly / Timevis</a></p>
</main></body></html>"""


def _escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _first(query, key, default=None):
    values = query.get(key)
    return values[0] if values else default


def _integer(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _find_statistics(value, statistic_ids):
    if not isinstance(value, dict):
        return {}
    if any(isinstance(value.get(item), list) for item in statistic_ids):
        return value
    for key in ("statistics", "service_response", "result", "response", "data"):
        found = _find_statistics(value.get(key), statistic_ids)
        if found:
            return found
    return {}


def _number(value):
    return "–" if value is None else f"{float(value):.1f}"


def _signed_number(value):
    if value is None:
        return "–"
    return f"{float(value):+.1f}"


def _deviation_class(value):
    if value is None or float(value) == 0:
        return ""
    return "positive" if float(value) > 0 else "negative"


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main():
    options = load_options()
    logging.basicConfig(
        level=getattr(logging, str(options["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = ServiceState(options)
    state.refresh()
    worker = threading.Thread(
        target=_background_loop,
        args=(state,),
        name="forecast-refresh",
        daemon=True,
    )
    worker.start()
    server = ForecastServer(
        ("0.0.0.0", int(options["listen_port"])),
        RequestHandler,
        state,
    )
    LOG.info("Listening on port %s", options["listen_port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
