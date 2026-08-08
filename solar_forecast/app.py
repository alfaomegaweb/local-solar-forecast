#!/usr/bin/env python3
"""HTTP service for the Local Solar Forecast Home Assistant app."""

from __future__ import annotations

import hashlib
import copy
import json
import logging
import math
import os
import shutil
import sqlite3
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
from regulator import RegulatorInputError, build_observation_plan
from ha_collector import (
    HACollectorError,
    collect_regulator_inputs,
    next_soc_checkpoint,
)


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
    "history_database_import_path": "",
}

DASHBOARD_TRANSLATIONS = {
    "no": {
        "language": "Språk", "running": "Kjører", "waiting": "Venter",
        "site": "Anlegg", "forecast": "Prognose", "model": "Modell",
        "empiricalDays": "Empiridøgn", "forecastAhead": "Prognose fremover",
        "date": "Dato", "weather": "Vær", "hoursAhead": "Timer frem",
        "forecastVsActual": "Prognose mot faktisk",
        "fixedComparison": "Fast sammenligning: siste prognose kl. 18 eller tidligere dagen før.",
        "actual": "Faktisk", "deviationKwh": "Avvik kWh",
        "deviationPercent": "Avvik %", "issued": "Utstedt",
        "hourlyVerification": "Timevis dagslysverifikasjon",
        "target": "Måltime", "cloud": "Skydekke %",
        "forecastTemp": "Prognose °C", "actualTemp": "Faktisk °C",
        "source": "Kilde", "noCompletedDay": "Ingen avsluttet dag",
        "snapshots": "Prognosehistorikk", "empirics": "Empiri",
        "hourly": "Timevis"
    },
    "en": {
        "language": "Language", "running": "Running", "waiting": "Waiting",
        "site": "Site", "forecast": "Forecast", "model": "Model",
        "empiricalDays": "Empirical days", "forecastAhead": "Forecast ahead",
        "date": "Date", "weather": "Weather", "hoursAhead": "Hours ahead",
        "forecastVsActual": "Forecast versus actual",
        "fixedComparison": "Fixed comparison: latest forecast at or before 18:00 the previous day.",
        "actual": "Actual", "deviationKwh": "Deviation kWh",
        "deviationPercent": "Deviation %", "issued": "Issued",
        "hourlyVerification": "Hourly daylight verification",
        "target": "Target hour", "cloud": "Cloud %",
        "forecastTemp": "Forecast °C", "actualTemp": "Actual °C",
        "source": "Source", "noCompletedDay": "No completed day",
        "snapshots": "Forecast history", "empirics": "Empirics",
        "hourly": "Hourly"
    },
    "pt": {
        "language": "Idioma", "running": "Em execução", "waiting": "A aguardar",
        "site": "Instalação", "forecast": "Previsão", "model": "Modelo",
        "empiricalDays": "Dias medidos", "forecastAhead": "Previsão futura",
        "date": "Data", "weather": "Tempo", "hoursAhead": "Horas à frente",
        "forecastVsActual": "Previsão versus real",
        "fixedComparison": "Comparação fixa: última previsão até às 18:00 do dia anterior.",
        "actual": "Real", "deviationKwh": "Desvio kWh",
        "deviationPercent": "Desvio %", "issued": "Emitida",
        "hourlyVerification": "Verificação horária com luz solar",
        "target": "Hora-alvo", "cloud": "Nuvens %",
        "forecastTemp": "Previsão °C", "actualTemp": "Real °C",
        "source": "Fonte", "noCompletedDay": "Nenhum dia concluído",
        "snapshots": "Histórico de previsões", "empirics": "Medições",
        "hourly": "Horário"
    },
    "es": {
        "language": "Idioma", "running": "En ejecución", "waiting": "En espera",
        "site": "Instalación", "forecast": "Pronóstico", "model": "Modelo",
        "empiricalDays": "Días medidos", "forecastAhead": "Pronóstico futuro",
        "date": "Fecha", "weather": "Tiempo", "hoursAhead": "Horas por delante",
        "forecastVsActual": "Pronóstico frente a real",
        "fixedComparison": "Comparación fija: último pronóstico hasta las 18:00 del día anterior.",
        "actual": "Real", "deviationKwh": "Desviación kWh",
        "deviationPercent": "Desviación %", "issued": "Emitido",
        "hourlyVerification": "Verificación horaria con luz solar",
        "target": "Hora objetivo", "cloud": "Nubes %",
        "forecastTemp": "Pronóstico °C", "actualTemp": "Real °C",
        "source": "Fuente", "noCompletedDay": "Ningún día completado",
        "snapshots": "Historial de pronósticos", "empirics": "Mediciones",
        "hourly": "Horario"
    },
    "uk": {
        "language": "Мова", "running": "Працює", "waiting": "Очікування",
        "site": "Об’єкт", "forecast": "Прогноз", "model": "Модель",
        "empiricalDays": "Дні вимірювань", "forecastAhead": "Майбутній прогноз",
        "date": "Дата", "weather": "Погода", "hoursAhead": "Годин уперед",
        "forecastVsActual": "Прогноз і фактичне значення",
        "fixedComparison": "Фіксоване порівняння: останній прогноз до 18:00 попереднього дня.",
        "actual": "Фактично", "deviationKwh": "Відхилення кВт·год",
        "deviationPercent": "Відхилення %", "issued": "Створено",
        "hourlyVerification": "Погодинна перевірка за денного світла",
        "target": "Цільова година", "cloud": "Хмарність %",
        "forecastTemp": "Прогноз °C", "actualTemp": "Фактично °C",
        "source": "Джерело", "noCompletedDay": "Немає завершеного дня",
        "snapshots": "Історія прогнозів", "empirics": "Вимірювання",
        "hourly": "Погодинно"
    },
    "de": {
        "language": "Sprache", "running": "Läuft", "waiting": "Wartet",
        "site": "Anlage", "forecast": "Prognose", "model": "Modell",
        "empiricalDays": "Messtage", "forecastAhead": "Künftige Prognose",
        "date": "Datum", "weather": "Wetter", "hoursAhead": "Stunden voraus",
        "forecastVsActual": "Prognose gegenüber Istwert",
        "fixedComparison": "Fester Vergleich: letzte Prognose bis 18:00 Uhr am Vortag.",
        "actual": "Istwert", "deviationKwh": "Abweichung kWh",
        "deviationPercent": "Abweichung %", "issued": "Erstellt",
        "hourlyVerification": "Stündliche Tageslichtprüfung",
        "target": "Zielstunde", "cloud": "Bewölkung %",
        "forecastTemp": "Prognose °C", "actualTemp": "Istwert °C",
        "source": "Quelle", "noCompletedDay": "Kein abgeschlossener Tag",
        "snapshots": "Prognoseverlauf", "empirics": "Messwerte",
        "hourly": "Stündlich"
    },
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


def import_history_database_once(source_path, destination_path):
    """Safely seed an empty app data directory from a preserved SQLite DB."""
    source = Path(str(source_path or ""))
    destination = Path(destination_path)
    if not source_path or not source.is_file() or destination.exists():
        return {"imported": False, "reason": "not_requested_or_destination_exists"}

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"History database import failed quick_check: {result}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".importing")
    if temporary.exists():
        temporary.unlink()
    shutil.copy2(source, temporary)
    try:
        with sqlite3.connect(temporary) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(
                    f"Copied history database failed quick_check: {result}"
                )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "imported": True,
        "source": str(source),
        "destination": str(destination),
        "bytes": destination.stat().st_size,
    }


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
        history_path = data_dir / "forecast-history.sqlite"
        self.database_import = import_history_database_once(
            options.get("history_database_import_path"), history_path
        )
        if self.database_import.get("imported"):
            LOG.info("Preserved history database imported: %s", self.database_import)
        self.history = ForecastHistory(history_path)
        self.config = load_site_config(options["site_config_path"])
        self._quarantine_existing_actuals()
        legacy_path = Path(str(options.get("legacy_history_path") or ""))
        if legacy_path.is_file():
            self.legacy_import = self.history.import_legacy_ndjson(legacy_path)
            LOG.info("Legacy forecast import: %s", self.legacy_import)

    def _quarantine_existing_actuals(self):
        """Apply explicit, reversible site quality gates to preserved history."""
        solar_energy = (
            (self.config.get("measurements") or {}).get("solar_energy") or {}
        )
        quality = solar_energy.get("data_quality") or {}
        if quality.get("auto_quarantine_existing") is not True:
            return {"enabled": False, "candidates": 0, "inserted": 0}
        minimum_total = float(quality.get("minimum_daily_total_kwh", 0.05))
        maximum_specific_yield = float(
            quality.get("maximum_daily_specific_yield_kwh_per_kwp", 8.0)
        )
        installed_capacity = sum(
            float(item["capacity_kwp"]) for item in self.config["arrays"]
        )
        maximum_total = installed_capacity * maximum_specific_yield
        site_id = self.config["site"]["id"]
        findings = self.history.audit_daily_actuals(
            site_id,
            zero_threshold_kwh=minimum_total,
            maximum_daily_kwh=maximum_total,
        )
        candidates = [
            item for item in findings if item["reasons"] and not item["excluded"]
        ]
        result = self.history.exclude_observations(
            site_id,
            "daily",
            [item["observation_id"] for item in candidates],
            "automatic_site_data_quality_gate",
            details={
                "minimum_daily_total_kwh": minimum_total,
                "maximum_daily_specific_yield_kwh_per_kwp": (
                    maximum_specific_yield
                ),
                "maximum_daily_total_kwh": maximum_total,
                "raw_observations_preserved": True,
            },
        )
        LOG.info(
            "Historical empirical quarantine: candidates=%s inserted=%s",
            len(candidates),
            result["inserted"],
        )
        return {
            "enabled": True,
            "candidates": len(candidates),
            "inserted": result["inserted"],
            "missing": result["missing"],
        }

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
                if calibration_options.get("enabled", False)
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
                    minimum_complete_days=calibration_options.get(
                        "minimum_training_days", 3
                    ),
                    minimum_valid_hours_per_day=calibration_options.get(
                        "minimum_valid_hours_per_day", 4
                    ),
                    rolling_window_days=calibration_options.get(
                        "rolling_window_days", 180
                    ),
                    factor_min=calibration_options.get("factor_min", 0.70),
                    factor_max=calibration_options.get("factor_max", 1.30),
                    minimum_mae_improvement_percent=calibration_options.get(
                        "minimum_mae_improvement_percent", 2
                    ),
                    minimum_actual_kwh=calibration_options.get(
                        "minimum_actual_hourly_kwh", 0.01
                    ),
                    required_measurement_source_fingerprint=(
                        _measurement_source_details(
                            (
                                (self.config.get("measurements") or {})
                                .get("solar_energy", {})
                                .get("statistic_entities", [])
                            ),
                            aggregation=(
                                (self.config.get("measurements") or {})
                                .get("solar_energy", {})
                                .get("aggregation", "sum")
                            ),
                            normalized_unit="kWh",
                        )["measurement_source_fingerprint"]
                    ),
                )
                if calibration_options.get("enabled", False)
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

    def collect_regulator_observation(self, collected_at=None):
        """Read and normalize HA states without invoking any HA service."""
        contract = self.config.get("energy_regulator_vnext") or {}
        collector = contract.get("collector") or {}
        if not collector.get("enabled", False):
            raise HACollectorError("site collector is disabled")
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise HACollectorError("SUPERVISOR_TOKEN is unavailable")
        forecast = self.current_forecast()
        if forecast is None:
            raise HACollectorError("forecast is unavailable")
        observed_at = collected_at or _iso(datetime.now(UTC))
        runtime_config = copy.deepcopy(self.config)
        runtime_collector = runtime_config["energy_regulator_vnext"]["collector"]
        runtime_static = runtime_collector.setdefault("static", {})
        checkpoint_options = (
            (runtime_config.get("planning") or {}).get("soc_checkpoint") or {}
        )
        runtime_static["checkpoint_at"] = next_soc_checkpoint(
            forecast,
            observed_at,
            checkpoint_options.get("minutes_before_sunset", 60),
        )
        request = urllib.request.Request(
            "http://supervisor/core/api/states",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            states = json.load(response)
        result = collect_regulator_inputs(runtime_config, states, observed_at)
        result["runtime_site_config"] = runtime_config
        result["forecast"] = forecast
        return result

    def refresh_actuals(self):
        solar_energy = (
            (self.config.get("measurements") or {}).get("solar_energy") or {}
        )
        statistic_ids = solar_energy.get("statistic_entities", [])
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
            # Recorder converts every energy statistic to one common unit,
            # including sources whose native state is MWh.
            "units": {"energy": "kWh"},
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
                        change = float(point["change"])
                        if not math.isfinite(change) or change < 0:
                            continue
                    except (KeyError, TypeError, ValueError):
                        continue
                    if target_date >= today:
                        continue
                    day = by_date.setdefault(
                        target_date, {"entities": {}}
                    )
                    day["entities"][entity_id] = change
            observed_at = _iso(now)
            quality = solar_energy.get("data_quality") or {}
            require_all = quality.get("require_all_entities", True)
            minimum_total = float(
                quality.get("minimum_daily_total_kwh", 0.05)
            )
            maximum_specific_yield = float(
                quality.get("maximum_daily_specific_yield_kwh_per_kwp", 8.0)
            )
            installed_capacity = sum(
                float(item["capacity_kwp"]) for item in self.config["arrays"]
            )
            maximum_total = installed_capacity * maximum_specific_yield
            accepted = 0
            accepted_dates = set()
            rejected = {}
            for target_date, item in by_date.items():
                missing = [
                    entity_id
                    for entity_id in statistic_ids
                    if entity_id not in item["entities"]
                ]
                total = sum(item["entities"].values())
                reasons = []
                if require_all and missing:
                    reasons.append("missing_entities")
                if total < minimum_total:
                    reasons.append("daily_total_below_minimum")
                if total > maximum_total:
                    reasons.append("daily_specific_yield_above_maximum")
                if reasons:
                    rejected[target_date] = {
                        "reasons": reasons,
                        "missing_entities": missing,
                        "total_kwh": total,
                        "maximum_daily_total_kwh": maximum_total,
                    }
                    continue
                self.history.append_actual(
                    self.config["site"]["id"],
                    target_date,
                    total,
                    "home_assistant_recorder_statistics",
                    details={
                        "entities_kwh": item["entities"],
                        **_measurement_source_details(
                            statistic_ids,
                            aggregation=solar_energy.get("aggregation", "sum"),
                            normalized_unit="kWh",
                        ),
                        "quality_valid": True,
                        "quality_rules": {
                            "require_all_entities": bool(require_all),
                            "minimum_daily_total_kwh": minimum_total,
                            "maximum_daily_specific_yield_kwh_per_kwp": (
                                maximum_specific_yield
                            ),
                            "maximum_daily_total_kwh": maximum_total,
                        },
                    },
                    observed_at=observed_at,
                )
                accepted += 1
                accepted_dates.add(target_date)
            latest_completed_date = (
                datetime.now(zone).date() - timedelta(days=1)
            ).isoformat()
            quality_messages = []
            if accepted == 0:
                quality_messages.append("no complete empirical day was accepted")
            if latest_completed_date not in accepted_dates:
                quality_messages.append(
                    f"latest completed day {latest_completed_date} is unavailable"
                )
            if rejected:
                recent_rejected = dict(sorted(rejected.items(), reverse=True)[:10])
                quality_messages.append(
                    f"rejected {len(rejected)} day(s): "
                    + json.dumps(recent_rejected, sort_keys=True)
                )
            if latest_completed_date in accepted_dates:
                self.last_empirical_success_at = observed_at
            self.last_empirical_error = (
                "; ".join(quality_messages) if quality_messages else None
            )
            LOG.info(
                "Empirical production updated: accepted=%s rejected=%s",
                accepted,
                len(rejected),
            )
        except Exception as exc:
            self.last_empirical_error = str(exc)
            LOG.warning("Empirical production refresh failed: %s", exc)

    def refresh_hourly_actuals(self):
        """Capture hourly PV energy and outdoor temperature from HA Recorder."""
        measurements = self.config.get("measurements") or {}
        solar_energy = measurements.get("solar_energy") or {}
        solar_ids = (
            solar_energy.get("statistic_entities", [])
            or solar_energy.get("entities", [])
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
            "units": {"energy": "kWh", "temperature": "°C"},
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
                        change = float(point["change"])
                        if not math.isfinite(change) or change < 0:
                            continue
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
                missing = [
                    entity_id
                    for entity_id in solar_ids
                    if entity_id not in item["entities"]
                ]
                pv_value = item["pv_kwh"]
                if missing:
                    pv_value = None
                if pv_value is not None and pv_value <= 0:
                    pv_value = None
                if pv_value is None and item["temperature_c"] is None:
                    continue
                self.history.append_hourly_actual(
                    self.config["site"]["id"],
                    target_time,
                    actual_pv_kwh=pv_value,
                    actual_temperature_c=item["temperature_c"],
                    source="home_assistant_recorder_hourly_statistics",
                    details={
                        "solar_entities_kwh": item["entities"],
                        **_measurement_source_details(
                            solar_ids,
                            aggregation=solar_energy.get("aggregation", "sum"),
                            normalized_unit="kWh",
                        ),
                        "quality_valid": not missing and pv_value is not None,
                        "missing_entities": missing,
                    },
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
            details = item.get("details") or {}
            minimum_daily = float(
                (((self.config.get("measurements") or {}).get("solar_energy") or {})
                 .get("data_quality") or {})
                .get("minimum_daily_total_kwh", 0.05)
            )
            maximum_specific_yield = float(
                (((self.config.get("measurements") or {}).get("solar_energy") or {})
                 .get("data_quality") or {})
                .get("maximum_daily_specific_yield_kwh_per_kwp", 8.0)
            )
            maximum_daily = maximum_specific_yield * sum(
                float(row["capacity_kwp"]) for row in self.config["arrays"]
            )
            if actual < minimum_daily and not details.get(
                "confirmed_zero_production"
            ):
                raise ValueError(
                    "daily actual below quality minimum; set "
                    "details.confirmed_zero_production=true only for a "
                    "verified real zero-production day"
                )
            if actual > maximum_daily:
                raise ValueError(
                    "daily actual exceeds the configured physical specific-yield "
                    "limit"
                )
            _, inserted = self.history.append_actual(
                site_id,
                target_date,
                actual,
                source,
                details=details,
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
                "empirical_exclusions": self.history.exclusion_summary(
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
        if path == "/api/regulator/history":
            site_id = _first(
                query, "site_id", self.server.state.config["site"]["id"]
            )
            limit = _integer(_first(query, "limit", "200"), 200, 1, 5000)
            rows = self.server.state.history.list_regulator_plans(
                site_id, limit=limit
            )
            self._json(
                {
                    "site_id": site_id,
                    "count": len(rows),
                    "append_only": True,
                    "plans": rows,
                }
            )
            return
        if path == "/api/regulator/replay":
            decision_id = _first(query, "decision_id")
            if not decision_id:
                self._json({"error": "decision_id_required"}, 400)
                return
            stored = self.server.state.history.regulator_plan(decision_id)
            if stored is None:
                self._json({"error": "regulator_plan_not_found"}, 404)
                return
            inputs = stored["input"]
            try:
                replayed = build_observation_plan(
                    inputs["site_config"],
                    inputs["forecast"],
                    inputs["snapshot"],
                    inputs["prices"],
                )
            except (KeyError, TypeError, ValueError, RegulatorInputError) as exc:
                self._json(
                    {
                        "decision_id": decision_id,
                        "replay_matches": False,
                        "error": "stored_regulator_input_invalid",
                        "message": str(exc),
                    },
                    500,
                )
                return
            matches = replayed == stored["plan"]
            self._json(
                {
                    "decision_id": decision_id,
                    "site_id": stored["site_id"],
                    "input_fingerprint": stored["input_fingerprint"],
                    "stored_plan_version": stored["plan_version"],
                    "replayed_plan_version": replayed["plan_version"],
                    "replay_matches": matches,
                    "actuation_authorized": False,
                },
                200 if matches else 409,
            )
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
                    "exclusions": self.server.state.history.exclusion_summary(
                        site_id
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
        if path == "/api/regulator/collect-plan":
            try:
                collected = self.server.state.collect_regulator_observation()
                result = build_observation_plan(
                    collected["runtime_site_config"],
                    collected["forecast"],
                    collected["snapshot"],
                    collected["prices"],
                )
                input_bundle = {
                    "site_config": collected["runtime_site_config"],
                    "forecast": collected["forecast"],
                    "snapshot": collected["snapshot"],
                    "prices": collected["prices"],
                }
                decision_id, inserted = (
                    self.server.state.history.append_regulator_plan(
                        result, input_bundle
                    )
                )
            except (HACollectorError, RegulatorInputError, TypeError, ValueError) as exc:
                self._json(
                    {
                        "error": "regulator_collection_failed",
                        "message": str(exc),
                        "actuation_authorized": False,
                    },
                    409,
                )
                return
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                self._json(
                    {
                        "error": "home_assistant_read_failed",
                        "message": str(exc),
                        "actuation_authorized": False,
                    },
                    503,
                )
                return
            response = dict(result)
            response["collector"] = {
                "schema": collected["snapshot"]["schema"],
                "source_fingerprint": collected["snapshot"]["source_fingerprint"],
                "price_quality": collected["price_quality"],
                "read_only": True,
                "ha_service_calls": 0,
            }
            response["snapshot_storage"] = {
                "decision_id": decision_id,
                "inserted": inserted,
                "database": "forecast-history.sqlite",
                "append_only": True,
                "replay_endpoint": f"/api/regulator/replay?decision_id={decision_id}",
            }
            self._json(response, 200)
            return
        if path == "/api/regulator/plan":
            contract = self.server.state.config.get("energy_regulator_vnext")
            if not isinstance(contract, dict):
                self._json(
                    {
                        "error": "regulator_contract_not_configured",
                        "actuation_authorized": False,
                    },
                    409,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 5 * 1024 * 1024:
                    raise ValueError("invalid Content-Length")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                forecast = self.server.state.current_forecast()
                if forecast is None:
                    self._json(
                        {
                            "error": "forecast_unavailable",
                            "actuation_authorized": False,
                        },
                        503,
                    )
                    return
                result = build_observation_plan(
                    self.server.state.config,
                    forecast,
                    payload.get("snapshot") or {},
                    payload.get("prices") or [],
                )
            except (json.JSONDecodeError, TypeError, ValueError, RegulatorInputError) as exc:
                self._json(
                    {
                        "error": "invalid_regulator_input",
                        "message": str(exc),
                        "actuation_authorized": False,
                    },
                    400,
                )
                return
            input_bundle = {
                "site_config": self.server.state.config,
                "forecast": forecast,
                "snapshot": payload.get("snapshot") or {},
                "prices": payload.get("prices") or [],
            }
            decision_id, inserted = (
                self.server.state.history.append_regulator_plan(
                    result, input_bundle
                )
            )
            response = dict(result)
            response["snapshot_storage"] = {
                "decision_id": decision_id,
                "inserted": inserted,
                "database": "forecast-history.sqlite",
                "append_only": True,
                "replay_endpoint": (
                    f"/api/regulator/replay?decision_id={decision_id}"
                ),
            }
            self._json(response, 200)
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
    status_key = "running" if status["forecast_available"] else "waiting"
    hourly_day = (
        _escape(hourly_result.get("target_date"))
        if hourly_result.get("target_date")
        else '<span data-i18n="noCompletedDay">Ingen avsluttet dag</span>'
    )
    translations_json = json.dumps(DASHBOARD_TRANSLATIONS, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="no">
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
    .toolbar {{ display:flex; justify-content:flex-end; align-items:center; gap:8px; }}
    select {{ padding:7px 10px; border-radius:8px; }}
  </style>
</head>
<body><main>
  <div class="toolbar"><label for="language" data-i18n="language">Språk</label>
    <select id="language">
      <option value="no">Norsk</option><option value="en">English</option>
      <option value="pt">Português</option><option value="es">Español</option>
      <option value="uk">Українська</option><option value="de">Deutsch</option>
    </select>
  </div>
  <h1>Local Solar Forecast</h1>
  <p class="{'ok' if status['ok'] else 'error'}">
    <span data-i18n="{status_key}">{'Kjører' if status['forecast_available'] else 'Venter'}</span>
  </p>
  {error}
  <section class="cards">
    <div class="card"><span data-i18n="site">Anlegg</span><div class="value">{_escape(status['site_id'])}</div></div>
    <div class="card"><span data-i18n="forecast">Prognose</span><div class="value">{summary.get('expected_kwh_total', '–')} kWh</div></div>
    <div class="card"><span data-i18n="model">Modell</span><div class="value">{MODEL_VERSION}</div></div>
    <div class="card"><span data-i18n="empiricalDays">Empiridøgn</span><div class="value">{metrics['days']}</div></div>
    <div class="card">MAE<div class="value">{_number(metrics['mae_kwh'])} kWh</div></div>
    <div class="card">Bias<div class="value">{_signed_number(metrics['bias_kwh'])} kWh</div></div>
  </section>
  <h2 data-i18n="forecastAhead">Prognose fremover</h2>
  <table>
    <thead><tr><th data-i18n="date">Dato</th><th>kWh</th><th data-i18n="weather">Vær</th><th data-i18n="hoursAhead">Timer frem</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2 data-i18n="forecastVsActual">Prognose mot faktisk</h2>
  <p data-i18n="fixedComparison">Fast sammenligning: siste prognose kl. 18 eller tidligere dagen før.</p>
  <table>
    <thead><tr><th data-i18n="date">Dato</th><th data-i18n="forecast">Prognose</th>
      <th data-i18n="actual">Faktisk</th><th data-i18n="deviationKwh">Avvik kWh</th>
      <th data-i18n="deviationPercent">Avvik %</th><th data-i18n="issued">Utstedt</th></tr></thead>
    <tbody>{empirical_rows}</tbody>
  </table>
  <h2 data-i18n="hourlyVerification">Timevis dagslysverifikasjon</h2>
  <p>{hourly_day}</p>
  <table>
    <thead><tr><th data-i18n="target">Måltime</th><th><span data-i18n="forecast">Prognose</span> kWh</th>
      <th><span data-i18n="actual">Faktisk</span> kWh</th><th data-i18n="deviationKwh">Avvik kWh</th><th data-i18n="cloud">Skydekke %</th>
      <th data-i18n="forecastTemp">Prognose °C</th><th data-i18n="actualTemp">Faktisk °C</th><th data-i18n="hoursAhead">Timer frem</th>
      <th data-i18n="source">Kilde</th></tr></thead>
    <tbody>{hourly_rows}</tbody>
  </table>
  <p><a href="./api/forecast">JSON API</a> ·
    <a href="./api/history" data-i18n="snapshots">Prognosehistorikk</a> ·
    <a href="./api/empirics" data-i18n="empirics">Empiri</a> ·
    <a href="./api/hourly-comparison?target_date={_escape(hourly_result.get('target_date') or '')}" data-i18n="hourly">Timevis</a></p>
</main>
<script>
  const translations = {translations_json};
  const selector = document.getElementById("language");
  function setLanguage(language) {{
    const selected = translations[language] ? language : "no";
    document.documentElement.lang = selected;
    selector.value = selected;
    document.querySelectorAll("[data-i18n]").forEach(element => {{
      const value = translations[selected][element.dataset.i18n];
      if (value !== undefined) element.textContent = value;
    }});
    localStorage.setItem("lsf-language", selected);
  }}
  selector.addEventListener("change", event => setLanguage(event.target.value));
  setLanguage(localStorage.getItem("lsf-language") || "no");
</script>
</body></html>"""


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


def _measurement_source_details(entity_ids, aggregation, normalized_unit):
    """Return a stable, non-secret fingerprint for an empirical source set."""
    source = {
        "entity_ids": sorted(str(item) for item in entity_ids),
        "aggregation": str(aggregation),
        "normalized_unit": str(normalized_unit),
    }
    canonical = json.dumps(
        source, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "measurement_source": source,
        "measurement_source_fingerprint": hashlib.sha256(canonical).hexdigest(),
    }


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
