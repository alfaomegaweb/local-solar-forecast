"""Append-only SQLite storage for solar forecast and empirical snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


class ForecastHistory:
    def __init__(self, path):
        self.path = path
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS forecast_runs (
                    run_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    forecast_issued_at TEXT NOT NULL,
                    model_run_at TEXT,
                    issued_at_semantics TEXT NOT NULL DEFAULT 'forecast_generated_at',
                    source TEXT NOT NULL DEFAULT 'unknown',
                    model_version TEXT NOT NULL,
                    configuration_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_forecasts (
                    run_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    forecast_issued_at TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    forecast_horizon_hours REAL,
                    expected_kwh_total REAL NOT NULL,
                    expected_kwh_by_direction_json TEXT NOT NULL,
                    weather_json TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    PRIMARY KEY (run_id, target_date),
                    FOREIGN KEY (run_id) REFERENCES forecast_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_target
                ON daily_forecasts(site_id, target_date, forecast_issued_at);

                CREATE TABLE IF NOT EXISTS hourly_forecasts (
                    run_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    forecast_issued_at TEXT NOT NULL,
                    target_time TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    lead_hours REAL NOT NULL,
                    interval_hours REAL NOT NULL,
                    expected_kwh_total REAL NOT NULL,
                    expected_kwh_by_direction_json TEXT NOT NULL,
                    air_temperature_2m_c REAL,
                    cloud_cover_total_percent REAL,
                    cloud_cover_low_percent REAL,
                    cloud_cover_medium_percent REAL,
                    cloud_cover_high_percent REAL,
                    is_day INTEGER NOT NULL,
                    sunrise TEXT,
                    sunset TEXT,
                    solar_elevation_deg REAL,
                    solar_azimuth_deg REAL,
                    ghi_w_m2 REAL,
                    direct_radiation_w_m2 REAL,
                    diffuse_radiation_w_m2 REAL,
                    direct_normal_irradiance_w_m2 REAL,
                    source TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, target_time),
                    FOREIGN KEY (run_id) REFERENCES forecast_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_hourly_target
                ON hourly_forecasts(site_id, target_time, forecast_issued_at);

                CREATE TABLE IF NOT EXISTS actual_observations (
                    observation_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    actual_kwh_total REAL NOT NULL,
                    source TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_actual_target
                ON actual_observations(site_id, target_date, observed_at);

                CREATE TABLE IF NOT EXISTS hourly_actual_observations (
                    observation_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    target_time TEXT NOT NULL,
                    actual_pv_kwh REAL,
                    actual_temperature_c REAL,
                    source TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_hourly_actual_target
                ON hourly_actual_observations(site_id, target_time, observed_at);

                CREATE TABLE IF NOT EXISTS raw_forecast_payloads (
                    raw_payload_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    model_run_at TEXT,
                    retrieved_at TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES forecast_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_raw_payload_run
                ON raw_forecast_payloads(run_id, retrieved_at);

                CREATE TABLE IF NOT EXISTS model_calibrations (
                    calibration_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    fitted_at TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    training_start TEXT NOT NULL,
                    training_end TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    intercept_factor REAL NOT NULL,
                    temperature_slope_per_c REAL NOT NULL,
                    mae_before_kwh REAL NOT NULL,
                    mae_after_kwh REAL NOT NULL,
                    accepted INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_calibration_site
                ON model_calibrations(site_id, fitted_at, accepted);
                """
            )
            self._ensure_column(
                connection,
                "forecast_runs",
                "model_run_at",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "forecast_runs",
                "issued_at_semantics",
                "TEXT NOT NULL DEFAULT 'forecast_generated_at'",
            )
            self._ensure_column(
                connection,
                "forecast_runs",
                "source",
                "TEXT NOT NULL DEFAULT 'unknown'",
            )

    @staticmethod
    def _ensure_column(connection, table, column, declaration):
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def append(self, forecast, raw_source=None):
        summary = forecast["forecast_summary"]
        site_id = forecast["site"]["id"]
        issued_at = summary["forecast_issued_at"]
        source = (
            (forecast.get("weather_source") or {}).get("provider")
            or summary.get("source")
            or "unknown"
        )
        model_run_at = summary.get("model_run_at")
        issued_at_semantics = summary.get(
            "issued_at_semantics", "forecast_generated_at"
        )
        canonical = json.dumps(
            forecast, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        run_id = hashlib.sha256(
            f"{site_id}\n{issued_at}\n{canonical}".encode("utf-8")
        ).hexdigest()
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO forecast_runs (
                    run_id, site_id, forecast_issued_at, model_run_at,
                    issued_at_semantics, source, model_version,
                    configuration_fingerprint, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    site_id,
                    issued_at,
                    model_run_at,
                    issued_at_semantics,
                    source,
                    summary["model_version"],
                    summary["configuration_fingerprint"],
                    canonical,
                    created_at,
                ),
            ).rowcount
            if inserted:
                for day in forecast.get("daily_forecast", []):
                    connection.execute(
                        """
                        INSERT INTO daily_forecasts (
                            run_id, site_id, forecast_issued_at, target_date,
                            forecast_horizon_hours, expected_kwh_total,
                            expected_kwh_by_direction_json, weather_json,
                            model_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            site_id,
                            issued_at,
                            day["target_date"],
                            day.get("forecast_horizon_hours"),
                            day["expected_kwh"],
                            json.dumps(
                                day.get("expected_kwh_by_direction", {}),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            json.dumps(
                                day.get("weather", {}),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            day.get("model_version", summary["model_version"]),
                        ),
                    )
                for hour in forecast.get("hourly_forecast", []):
                    target_time = hour.get("target_time") or hour.get("time")
                    if not target_time:
                        continue
                    connection.execute(
                        """
                        INSERT INTO hourly_forecasts (
                            run_id, site_id, forecast_issued_at, target_time,
                            target_date, lead_hours, interval_hours,
                            expected_kwh_total,
                            expected_kwh_by_direction_json,
                            air_temperature_2m_c,
                            cloud_cover_total_percent,
                            cloud_cover_low_percent,
                            cloud_cover_medium_percent,
                            cloud_cover_high_percent,
                            is_day, sunrise, sunset, solar_elevation_deg,
                            solar_azimuth_deg, ghi_w_m2,
                            direct_radiation_w_m2,
                            diffuse_radiation_w_m2,
                            direct_normal_irradiance_w_m2,
                            source, model_version, raw_json
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            run_id,
                            site_id,
                            issued_at,
                            str(target_time),
                            str(hour["target_date"]),
                            float(
                                hour.get("lead_hours")
                                if hour.get("lead_hours") is not None
                                else (
                                    _parse_utc(target_time)
                                    - _parse_utc(issued_at)
                                ).total_seconds()
                                / 3600.0
                            ),
                            float(hour.get("interval_hours", 1.0)),
                            float(hour.get("expected_kwh", 0.0)),
                            json.dumps(
                                hour.get("expected_kwh_by_direction", {}),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            hour.get("air_temperature_c"),
                            hour.get("cloud_area_fraction_percent"),
                            hour.get("cloud_area_fraction_low_percent"),
                            hour.get("cloud_area_fraction_medium_percent"),
                            hour.get("cloud_area_fraction_high_percent"),
                            int(bool(hour.get("is_day"))),
                            hour.get("sunrise"),
                            hour.get("sunset"),
                            hour.get("solar_elevation_deg"),
                            hour.get("solar_azimuth_deg"),
                            hour.get("ghi_w_m2"),
                            hour.get("direct_radiation_w_m2"),
                            hour.get("diffuse_radiation_w_m2"),
                            hour.get("direct_normal_irradiance_w_m2"),
                            source,
                            hour.get(
                                "model_version", summary["model_version"]
                            ),
                            json.dumps(
                                hour, ensure_ascii=False, sort_keys=True
                            ),
                        ),
                    )
                if raw_source:
                    self._append_raw_payload(
                        connection, run_id, raw_source, created_at
                    )
        return run_id, bool(inserted)

    @staticmethod
    def _append_raw_payload(connection, run_id, raw_source, retrieved_at):
        payload = raw_source.get("payload")
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_sha256 = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        request_json = json.dumps(
            raw_source.get("request") or {},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        raw_payload_id = hashlib.sha256(
            (
                f"{run_id}\n{raw_source.get('provider')}\n"
                f"{raw_source.get('model_run_at')}\n{payload_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT OR IGNORE INTO raw_forecast_payloads (
                raw_payload_id, run_id, provider, model, model_run_at,
                retrieved_at, request_json, payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_payload_id,
                run_id,
                str(raw_source.get("provider") or "unknown"),
                raw_source.get("model"),
                raw_source.get("model_run_at"),
                str(raw_source.get("retrieved_at") or retrieved_at),
                request_json,
                payload_json,
                payload_sha256,
            ),
        )

    def import_legacy_ndjson(self, path):
        """Import immutable snapshots made by the legacy sun.php recorder.

        The source run id and issue timestamp are retained. Re-running the
        import is safe because both run and daily keys use INSERT OR IGNORE.
        """
        imported_runs = 0
        imported_days = 0
        invalid_lines = 0
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    run_id = str(record["run_id"])
                    site_id = str(record["site_id"])
                    issued_at = str(record["forecast_issued_at"])
                    model_version = str(
                        record.get("model_version")
                        or "legacy-sun-php-pre-snapshot"
                    )
                    days = record["forecast_days"]
                    if not isinstance(days, list):
                        raise ValueError("forecast_days must be a list")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    invalid_lines += 1
                    continue

                canonical = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                with self._connect() as connection:
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO forecast_runs (
                            run_id, site_id, forecast_issued_at, model_version,
                            configuration_fingerprint, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            site_id,
                            issued_at,
                            model_version,
                            "legacy-sun-php",
                            canonical,
                            created_at,
                        ),
                    ).rowcount
                    if inserted:
                        imported_runs += 1
                    for day in days:
                        day_inserted = connection.execute(
                            """
                            INSERT OR IGNORE INTO daily_forecasts (
                                run_id, site_id, forecast_issued_at, target_date,
                                forecast_horizon_hours, expected_kwh_total,
                                expected_kwh_by_direction_json, weather_json,
                                model_version
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                site_id,
                                issued_at,
                                str(day["target_date"]),
                                day.get("forecast_horizon_hours"),
                                float(day["expected_kwh_total"]),
                                json.dumps(
                                    day.get("expected_kwh_by_direction", {}),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                json.dumps(
                                    day.get("weather", {}),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                model_version,
                            ),
                        ).rowcount
                        imported_days += day_inserted
        return {
            "imported_runs": imported_runs,
            "imported_days": imported_days,
            "invalid_lines": invalid_lines,
        }

    def append_actual(
        self,
        site_id,
        target_date,
        actual_kwh_total,
        source,
        details=None,
        observed_at=None,
    ):
        """Append a measured daily production observation without overwriting."""
        details = details or {}
        observed_at = observed_at or datetime.now(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        canonical = json.dumps(
            {
                "site_id": site_id,
                "target_date": target_date,
                "actual_kwh_total": round(float(actual_kwh_total), 6),
                "source": source,
                "details": details,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        observation_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO actual_observations (
                    observation_id, site_id, target_date, actual_kwh_total,
                    source, details_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    site_id,
                    target_date,
                    float(actual_kwh_total),
                    source,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    observed_at,
                ),
            ).rowcount
        return observation_id, bool(inserted)

    def list_actuals(self, site_id, limit=730):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT observation_id, site_id, target_date, actual_kwh_total,
                       source, details_json, observed_at
                FROM actual_observations
                WHERE site_id = ?
                ORDER BY target_date DESC, observed_at DESC
                """,
                (site_id,),
            ).fetchall()
        latest = {}
        for row in rows:
            if row["target_date"] not in latest:
                item = dict(row)
                item["details"] = json.loads(item.pop("details_json"))
                latest[row["target_date"]] = item
        return list(latest.values())[: max(1, min(int(limit), 5000))]

    def append_hourly_actual(
        self,
        site_id,
        target_time,
        actual_pv_kwh=None,
        actual_temperature_c=None,
        source="unknown",
        details=None,
        observed_at=None,
    ):
        """Append one immutable hourly PV/temperature observation."""
        if actual_pv_kwh is None and actual_temperature_c is None:
            raise ValueError("at least one hourly actual value is required")
        normalized_target = _parse_utc(target_time).isoformat().replace(
            "+00:00", "Z"
        )
        observed_at = observed_at or datetime.now(UTC).isoformat().replace(
            "+00:00", "Z"
        )
        details = details or {}
        values = {
            "site_id": str(site_id),
            "target_time": normalized_target,
            "actual_pv_kwh": (
                None if actual_pv_kwh is None else round(float(actual_pv_kwh), 6)
            ),
            "actual_temperature_c": (
                None
                if actual_temperature_c is None
                else round(float(actual_temperature_c), 6)
            ),
            "source": str(source),
            "details": details,
            "observed_at": str(observed_at),
        }
        canonical = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        observation_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO hourly_actual_observations (
                    observation_id, site_id, target_time, actual_pv_kwh,
                    actual_temperature_c, source, details_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    values["site_id"],
                    normalized_target,
                    values["actual_pv_kwh"],
                    values["actual_temperature_c"],
                    values["source"],
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    values["observed_at"],
                ),
            ).rowcount
        return observation_id, bool(inserted)

    def list_hourly(self, site_id, target_date=None, run_id=None, limit=5000):
        clauses = ["h.site_id = ?"]
        parameters = [site_id]
        if target_date:
            clauses.append("h.target_date = ?")
            parameters.append(target_date)
        if run_id:
            clauses.append("h.run_id = ?")
            parameters.append(run_id)
        parameters.append(max(1, min(int(limit), 20000)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT h.*, r.model_run_at, r.issued_at_semantics
                FROM hourly_forecasts h
                JOIN forecast_runs r ON r.run_id = h.run_id
                WHERE {' AND '.join(clauses)}
                ORDER BY h.target_time ASC, h.forecast_issued_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["expected_kwh_by_direction"] = json.loads(
                item.pop("expected_kwh_by_direction_json")
            )
            item["raw"] = json.loads(item.pop("raw_json"))
            result.append(item)
        return result

    def hourly_comparisons(self, site_id, target_date, timezone_name):
        """Compare daylight hours from the fixed daily baseline with actuals."""
        baseline = self.comparison_baseline(
            site_id, target_date, timezone_name
        )
        daily = baseline.get("forecast")
        if not daily:
            return {
                "target_date": target_date,
                "selection_basis": baseline["selection_basis"],
                "forecast_issued_at": None,
                "hours": [],
            }
        forecast_rows = self.list_hourly(
            site_id, target_date=target_date, run_id=daily["run_id"]
        )
        start = datetime.combine(
            date.fromisoformat(target_date),
            time(0, 0),
            tzinfo=ZoneInfo(timezone_name),
        ).astimezone(UTC)
        end = start + timedelta(days=1, hours=2)
        with self._connect() as connection:
            actual_rows = connection.execute(
                """
                SELECT *
                FROM hourly_actual_observations
                WHERE site_id = ? AND target_time >= ? AND target_time < ?
                ORDER BY target_time ASC, observed_at DESC
                """,
                (
                    site_id,
                    start.isoformat().replace("+00:00", "Z"),
                    end.isoformat().replace("+00:00", "Z"),
                ),
            ).fetchall()
        actual_by_time = {}
        for row in actual_rows:
            normalized = _parse_utc(row["target_time"]).isoformat().replace(
                "+00:00", "Z"
            )
            actual_by_time.setdefault(normalized, dict(row))

        hours = []
        for forecast in forecast_rows:
            if not forecast["is_day"] or (
                forecast["solar_elevation_deg"] is not None
                and forecast["solar_elevation_deg"] <= 0
            ):
                continue
            normalized = _parse_utc(
                forecast["target_time"]
            ).isoformat().replace("+00:00", "Z")
            actual = actual_by_time.get(normalized)
            predicted = float(forecast["expected_kwh_total"])
            measured = (
                None
                if not actual or actual["actual_pv_kwh"] is None
                else float(actual["actual_pv_kwh"])
            )
            deviation = (
                None if measured is None else measured - predicted
            )
            actual_temperature = (
                None
                if not actual or actual["actual_temperature_c"] is None
                else float(actual["actual_temperature_c"])
            )
            forecast_temperature = forecast["air_temperature_2m_c"]
            hours.append(
                {
                    "target_time": normalized,
                    "forecast_kwh": predicted,
                    "actual_kwh": measured,
                    "deviation_kwh": deviation,
                    "deviation_percent": (
                        None
                        if deviation is None or predicted == 0
                        else 100.0 * deviation / predicted
                    ),
                    "forecast_temperature_c": forecast_temperature,
                    "actual_temperature_c": actual_temperature,
                    "temperature_deviation_c": (
                        None
                        if actual_temperature is None
                        or forecast_temperature is None
                        else actual_temperature - forecast_temperature
                    ),
                    "cloud_cover_total_percent": forecast[
                        "cloud_cover_total_percent"
                    ],
                    "cloud_cover_low_percent": forecast[
                        "cloud_cover_low_percent"
                    ],
                    "cloud_cover_medium_percent": forecast[
                        "cloud_cover_medium_percent"
                    ],
                    "cloud_cover_high_percent": forecast[
                        "cloud_cover_high_percent"
                    ],
                    "solar_elevation_deg": forecast["solar_elevation_deg"],
                    "ghi_w_m2": forecast["ghi_w_m2"],
                    "source": forecast["source"],
                    "model_version": forecast["model_version"],
                    "forecast_issued_at": forecast["forecast_issued_at"],
                    "model_run_at": forecast["model_run_at"],
                    "issued_at_semantics": forecast["issued_at_semantics"],
                    "lead_hours": forecast["lead_hours"],
                    "actual_source": actual["source"] if actual else None,
                }
            )
        return {
            "target_date": target_date,
            "selection_basis": baseline["selection_basis"],
            "forecast_issued_at": daily["forecast_issued_at"],
            "run_id": daily["run_id"],
            "hours": hours,
        }

    def hourly_actual_dates(self, site_id, timezone_name, limit=180):
        zone = ZoneInfo(timezone_name)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT target_time
                FROM hourly_actual_observations
                WHERE site_id = ?
                ORDER BY target_time DESC
                LIMIT ?
                """,
                (site_id, max(24, min(int(limit) * 24, 20000))),
            ).fetchall()
        dates = []
        seen = set()
        for row in rows:
            target = _parse_utc(row["target_time"]).astimezone(zone)
            value = target.date().isoformat()
            if value not in seen:
                dates.append(value)
                seen.add(value)
        return dates[: max(1, int(limit))]

    def append_calibration(self, calibration):
        canonical = json.dumps(
            calibration,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        calibration_id = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO model_calibrations (
                    calibration_id, site_id, fitted_at, algorithm_version,
                    training_start, training_end, sample_count,
                    intercept_factor, temperature_slope_per_c,
                    mae_before_kwh, mae_after_kwh, accepted, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    calibration_id,
                    calibration["site_id"],
                    calibration["fitted_at"],
                    calibration["algorithm_version"],
                    calibration["training_start"],
                    calibration["training_end"],
                    int(calibration["sample_count"]),
                    float(calibration["intercept_factor"]),
                    float(calibration["temperature_slope_per_c"]),
                    float(calibration["mae_before_kwh"]),
                    float(calibration["mae_after_kwh"]),
                    int(bool(calibration["accepted"])),
                    canonical,
                ),
            ).rowcount
        return calibration_id, bool(inserted)

    def active_calibration(self, site_id):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT calibration_id, details_json
                FROM model_calibrations
                WHERE site_id = ? AND accepted = 1
                ORDER BY fitted_at DESC
                LIMIT 1
                """,
                (site_id,),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["details_json"])
        result["calibration_id"] = row["calibration_id"]
        return result

    def comparisons(self, site_id, timezone_name, limit=730):
        rows = []
        for actual in self.list_actuals(site_id, limit=limit):
            baseline = self.comparison_baseline(
                site_id, actual["target_date"], timezone_name
            )
            forecast = baseline.get("forecast")
            predicted = (
                float(forecast["expected_kwh_total"]) if forecast else None
            )
            measured = float(actual["actual_kwh_total"])
            deviation = measured - predicted if predicted is not None else None
            deviation_percent = (
                100.0 * deviation / predicted
                if deviation is not None and predicted != 0
                else None
            )
            rows.append(
                {
                    "target_date": actual["target_date"],
                    "forecast_kwh": predicted,
                    "actual_kwh": measured,
                    "deviation_kwh": deviation,
                    "deviation_percent": deviation_percent,
                    "forecast_issued_at": (
                        forecast["forecast_issued_at"] if forecast else None
                    ),
                    "selection_basis": baseline["selection_basis"],
                    "model_version": (
                        forecast["model_version"] if forecast else None
                    ),
                    "actual_source": actual["source"],
                    "actual_observed_at": actual["observed_at"],
                }
            )
        return rows

    def empirical_metrics(self, site_id, timezone_name, limit=730):
        rows = [
            row
            for row in self.comparisons(site_id, timezone_name, limit)
            if row["forecast_kwh"] is not None
        ]
        if not rows:
            return {
                "days": 0,
                "mae_kwh": None,
                "bias_kwh": None,
                "wmape_percent": None,
            }
        deviations = [row["deviation_kwh"] for row in rows]
        actual_sum = sum(row["actual_kwh"] for row in rows)
        return {
            "days": len(rows),
            "mae_kwh": sum(abs(value) for value in deviations) / len(rows),
            "bias_kwh": sum(deviations) / len(rows),
            "wmape_percent": (
                100.0 * sum(abs(value) for value in deviations) / actual_sum
                if actual_sum
                else None
            ),
        }

    def list_daily(self, site_id=None, target_date=None, limit=200):
        clauses = []
        parameters = []
        if site_id:
            clauses.append("site_id = ?")
            parameters.append(site_id)
        if target_date:
            clauses.append("target_date = ?")
            parameters.append(target_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(int(limit), 5000)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, site_id, forecast_issued_at, target_date,
                       forecast_horizon_hours, expected_kwh_total,
                       expected_kwh_by_direction_json, weather_json,
                       model_version
                FROM daily_forecasts
                {where}
                ORDER BY forecast_issued_at DESC, target_date ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["expected_kwh_by_direction"] = json.loads(
                item.pop("expected_kwh_by_direction_json")
            )
            item["weather"] = json.loads(item.pop("weather_json"))
            result.append(item)
        return result

    def comparison_baseline(self, site_id, target_date, timezone_name):
        """Select the fixed historical comparison forecast.

        Primary rule: latest snapshot issued at or before 18:00 local time on
        the day before the target date.  Fallback: latest snapshot before local
        midnight starting the target date.  Last fallback: earliest available
        snapshot for the target date.
        """
        target = date.fromisoformat(target_date)
        zone = ZoneInfo(timezone_name)
        primary_cutoff = datetime.combine(
            target - timedelta(days=1), time(18, 0), tzinfo=zone
        ).astimezone(UTC)
        midnight_cutoff = datetime.combine(
            target, time(0, 0), tzinfo=zone
        ).astimezone(UTC)
        rows = self.list_daily(site_id=site_id, target_date=target_date, limit=1000)
        chronological = sorted(
            rows,
            key=lambda item: item["forecast_issued_at"],
        )

        primary = [
            row
            for row in chronological
            if _parse_utc(row["forecast_issued_at"]) <= primary_cutoff
        ]
        if primary:
            return {
                "selection_basis": "latest_at_or_before_18_local_previous_day",
                "cutoff": primary_cutoff.isoformat().replace("+00:00", "Z"),
                "forecast": primary[-1],
            }

        before_midnight = [
            row
            for row in chronological
            if _parse_utc(row["forecast_issued_at"]) < midnight_cutoff
        ]
        if before_midnight:
            return {
                "selection_basis": "fallback_latest_before_target_midnight",
                "cutoff": midnight_cutoff.isoformat().replace("+00:00", "Z"),
                "forecast": before_midnight[-1],
            }
        if chronological:
            return {
                "selection_basis": "fallback_earliest_available_for_target",
                "cutoff": None,
                "forecast": chronological[0],
            }
        return {
            "selection_basis": "no_snapshot_available",
            "cutoff": primary_cutoff.isoformat().replace("+00:00", "Z"),
            "forecast": None,
        }

    def prune(self, history_days):
        """Retained for API compatibility; immutable forecast history is never pruned."""
        return 0


def _parse_utc(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
