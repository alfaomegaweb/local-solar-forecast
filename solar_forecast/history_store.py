"""Append-only SQLite storage for solar forecast snapshots."""

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
                """
            )

    def append(self, forecast):
        summary = forecast["forecast_summary"]
        site_id = forecast["site"]["id"]
        issued_at = summary["forecast_issued_at"]
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
                    run_id, site_id, forecast_issued_at, model_version,
                    configuration_fingerprint, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    site_id,
                    issued_at,
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
        return run_id, bool(inserted)

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
        cutoff = (
            datetime.now(UTC) - timedelta(days=max(30, int(history_days)))
        ).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            old_runs = [
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM forecast_runs WHERE forecast_issued_at < ?",
                    (cutoff,),
                )
            ]
            if not old_runs:
                return 0
            placeholders = ",".join("?" for _ in old_runs)
            connection.execute(
                f"DELETE FROM daily_forecasts WHERE run_id IN ({placeholders})",
                old_runs,
            )
            connection.execute(
                f"DELETE FROM forecast_runs WHERE run_id IN ({placeholders})",
                old_runs,
            )
            return len(old_runs)


def _parse_utc(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
