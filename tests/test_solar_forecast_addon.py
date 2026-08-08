import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "solar_forecast"))

from forecast_engine import (  # noqa: E402
    ConfigurationError,
    build_forecast,
    plane_incidence_cosine,
    solar_position,
    validate_site_config,
)
from history_store import ForecastHistory  # noqa: E402
from backfill import (  # noqa: E402
    MepsThreddsClient,
    comparison_cutoff,
    open_meteo_to_engine_payload,
    select_model_run,
)
from calibration import apply_calibration, fit_calibration  # noqa: E402
from regulator import (  # noqa: E402
    PLAN_VERSION,
    RegulatorInputError,
    build_observation_plan,
)
from ha_collector import (  # noqa: E402
    COLLECTOR_SCHEMA,
    HACollectorError,
    collect_regulator_inputs,
    next_soc_checkpoint,
)
from tools.rehearse_lsf_migration import rehearse  # noqa: E402
from tools.verify_lsf_fleet import verify_registry  # noqa: E402
import app as addon_app  # noqa: E402


UTC = timezone.utc


def sample_config(site_id="test_site"):
    return {
        "site": {
            "id": site_id,
            "name": "Generic test site",
            "latitude": 59.7736631,
            "longitude": 9.9051538,
            "timezone": "Europe/Oslo",
        },
        "weather": {},
        "model": {
            "base_performance_ratio": 0.82,
            "albedo": 0.2,
            "temperature_correction_enabled": True,
        },
        "arrays": [
            {
                "id": "east",
                "module_count": 20,
                "module_power_wp": 400,
                "tilt_deg": 30,
                "azimuth_deg": 90,
                "orientation": "east",
            },
            {
                "id": "west",
                "module_count": 20,
                "module_power_wp": 400,
                "tilt_deg": 30,
                "azimuth_deg": 270,
                "orientation": "west",
            },
            {
                "id": "south",
                "module_count": 10,
                "module_power_wp": 400,
                "tilt_deg": 30,
                "azimuth_deg": 180,
                "orientation": "south",
            },
        ],
    }


def met_payload(start, hours=72, cloud=0):
    timeseries = []
    for offset in range(hours):
        when = start + timedelta(hours=offset)
        timeseries.append(
            {
                "time": when.isoformat().replace("+00:00", "Z"),
                "data": {
                    "instant": {
                        "details": {
                            "air_temperature": 20,
                            "cloud_area_fraction": cloud,
                        }
                    },
                    "next_1_hours": {
                        "summary": {"symbol_code": "clearsky_day"}
                    },
                },
            }
        )
    return {"properties": {"timeseries": timeseries}}


class ForecastEngineTests(unittest.TestCase):
    def test_registration_only_sites_do_not_block_common_release(self):
        result = verify_registry(PROJECT / "fleet" / "sites.yaml")
        self.assertTrue(result["passed"], result["errors"])
        by_site = {row["site_id"]: row for row in result["sites"]}
        self.assertTrue(by_site["hf39"]["release_required"])
        self.assertTrue(by_site["bb86"]["release_required"])
        self.assertFalse(by_site["kv464"]["release_required"])
        self.assertFalse(by_site["kv464"]["passed"])
        self.assertEqual(by_site["kv464"]["readiness"], "not_ready")

    def test_candidate_package_can_be_verified_during_staged_rollout(self):
        result = verify_registry(PROJECT / "fleet" / "sites.yaml")
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["package_config_version"], "0.5.2")

    def test_canonical_fleet_profiles_are_valid_and_distinct(self):
        if addon_app.yaml is None:
            self.skipTest("PyYAML is unavailable")
        hf39 = addon_app.load_site_config(PROJECT / "solar-site-hf39.yaml")
        bb86 = addon_app.load_site_config(PROJECT / "sites" / "bb86.yaml")
        self.assertEqual(hf39["site"]["id"], "hf39")
        self.assertEqual(bb86["site"]["id"], "bb86")
        self.assertAlmostEqual(
            sum(row["capacity_kwp"] for row in hf39["arrays"]), 54.55
        )
        self.assertAlmostEqual(
            sum(row["capacity_kwp"] for row in bb86["arrays"]), 31.59
        )
        hf39_entities = hf39["measurements"]["solar_energy"][
            "statistic_entities"
        ]
        self.assertIn("sensor.solaredge_i1_ac_energy", hf39_entities)
        self.assertNotIn("sensor.solaredge_energy_today", hf39_entities)
        self.assertFalse(any("slave" in item for item in hf39_entities))
        bb86_entities = bb86["measurements"]["solar_energy"][
            "statistic_entities"
        ]
        self.assertEqual(
            bb86_entities,
            [
                "sensor.solis_energy_total_1",
                "sensor.solis_energy_total_2",
                "sensor.solis_energy_total_4",
                "sensor.deyeinvertermaster_summary_total_pv",
            ],
        )

    def test_solar_position_uses_geodata(self):
        oslo_noon = datetime.fromisoformat("2026-06-21T13:15:00+02:00")
        elevation, azimuth = solar_position(oslo_noon, 59.77, 9.91)
        self.assertGreater(elevation, 50)
        self.assertGreater(azimuth, 160)
        self.assertLess(azimuth, 220)

    def test_panel_incidence_distinguishes_east_and_west(self):
        east = plane_incidence_cosine(30, 100, 30, 90)
        west = plane_incidence_cosine(30, 100, 30, 270)
        self.assertGreater(east, west)

    def test_forecast_is_generic_and_energy_balances(self):
        start = datetime(2026, 6, 21, 0, tzinfo=UTC)
        result = build_forecast(
            sample_config("site_alpha"),
            met_payload(start),
            issued_at=start - timedelta(minutes=1),
        )
        self.assertEqual(result["site"]["id"], "site_alpha")
        self.assertEqual(result["system"]["installed_capacity_kwp"], 20.0)
        self.assertTrue(result["daily_forecast"])
        self.assertTrue(result["hourly_forecast"])
        for day in result["daily_forecast"]:
            directional = sum(day["expected_kwh_by_direction"].values())
            self.assertAlmostEqual(day["expected_kwh"], directional, delta=0.25)

        morning = next(
            row for row in result["hourly_forecast"] if "T06:00:00+02:00" in row["local_time"]
        )
        afternoon = next(
            row for row in result["hourly_forecast"] if "T18:00:00+02:00" in row["local_time"]
        )
        self.assertGreater(
            morning["expected_kwh_by_direction"]["east"],
            morning["expected_kwh_by_direction"]["west"],
        )
        self.assertGreater(
            afternoon["expected_kwh_by_direction"]["west"],
            afternoon["expected_kwh_by_direction"]["east"],
        )

    def test_missing_arrays_is_rejected(self):
        config = sample_config()
        config["arrays"] = []
        with self.assertRaises(ConfigurationError):
            build_forecast(
                config,
                met_payload(datetime(2026, 6, 21, tzinfo=UTC)),
            )

    def test_site_without_pv_returns_explicit_zero_forecast(self):
        config = sample_config()
        config["pv"] = {"mode": "none"}
        config["arrays"] = []
        config["system"] = {"installed_capacity_kwp": 0, "panel_count": 0}
        config["measurements"] = {}
        config["calibration"] = {"enabled": False}
        forecast = build_forecast(
            config,
            met_payload(datetime(2026, 6, 21, tzinfo=UTC)),
        )
        self.assertEqual(forecast["system"]["pv_mode"], "none")
        self.assertEqual(forecast["system"]["panel_count"], 0)
        self.assertEqual(forecast["system"]["installed_capacity_kwp"], 0)
        self.assertEqual(forecast["forecast_summary"]["expected_kwh_total"], 0)
        self.assertEqual(forecast["panel_configuration"], [])

    def test_site_without_pv_rejects_pv_arrays(self):
        config = sample_config()
        config["pv"] = {"mode": "none"}
        with self.assertRaisesRegex(ConfigurationError, "arrays must be empty"):
            validate_site_config(config)

    def test_declared_capacity_must_match_modules(self):
        config = sample_config()
        config["arrays"][0]["capacity_kwp"] = 99
        with self.assertRaises(ConfigurationError):
            validate_site_config(config)

    def test_duplicate_empirical_entities_are_rejected(self):
        config = sample_config()
        config["measurements"] = {
            "solar_energy": {
                "statistic_entities": [
                    "sensor.pv_energy",
                    "sensor.pv_energy",
                ],
                "data_quality": {
                    "require_all_entities": True,
                    "minimum_daily_total_kwh": 0.05,
                },
            }
        }
        with self.assertRaises(ConfigurationError):
            validate_site_config(config)

    def test_learning_must_be_explicit_and_use_current_safe_algorithm(self):
        config = sample_config()
        config["measurements"] = {
            "solar_energy": {
                "statistic_entities": ["sensor.pv_energy"],
                "data_quality": {"require_all_entities": True},
            }
        }
        config["calibration"] = {
            "enabled": True,
            "algorithm": "bounded-temperature-residual-1",
            "minimum_training_days": 3,
        }
        with self.assertRaises(ConfigurationError):
            validate_site_config(config)


class HistoryTests(unittest.TestCase):
    def test_history_tables_reject_update_and_delete(self):
        weather = met_payload(datetime(2026, 6, 21, 0, tzinfo=UTC), hours=30)
        forecast = build_forecast(
            sample_config(), weather, issued_at=datetime(2026, 6, 20, tzinfo=UTC)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            history = ForecastHistory(path)
            run_id, inserted = history.append(forecast)
            self.assertTrue(inserted)
            with sqlite3.connect(path) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE forecast_runs SET source = 'changed' WHERE run_id = ?",
                        (run_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM daily_forecasts WHERE run_id = ?", (run_id,)
                    )
            rows = history.list_daily(site_id="test_site")
            self.assertTrue(any(row["run_id"] == run_id for row in rows))

    def test_snapshots_are_append_only_and_baseline_is_18_previous_day(self):
        weather = met_payload(datetime(2026, 6, 21, 0, tzinfo=UTC), hours=30)
        issues = [
            datetime(2026, 6, 20, 15, 0, tzinfo=UTC),  # 17:00 local
            datetime(2026, 6, 20, 17, 0, tzinfo=UTC),  # 19:00 local
        ]
        forecasts = [
            build_forecast(sample_config(), weather, issued_at=issued)
            for issued in issues
        ]
        with tempfile.TemporaryDirectory() as directory:
            history = ForecastHistory(Path(directory) / "history.sqlite")
            first_id, first_inserted = history.append(forecasts[0])
            second_id, second_inserted = history.append(forecasts[1])
            duplicate_id, duplicate_inserted = history.append(forecasts[0])
            self.assertTrue(first_inserted)
            self.assertTrue(second_inserted)
            self.assertFalse(duplicate_inserted)
            self.assertEqual(first_id, duplicate_id)
            self.assertNotEqual(first_id, second_id)

            rows = history.list_daily(
                site_id="test_site", target_date="2026-06-21"
            )
            self.assertEqual(len(rows), 2)
            baseline = history.comparison_baseline(
                "test_site", "2026-06-21", "Europe/Oslo"
            )
            self.assertEqual(
                baseline["selection_basis"],
                "latest_at_or_before_18_local_previous_day",
            )
            self.assertEqual(
                baseline["forecast"]["forecast_issued_at"],
                "2026-06-20T15:00:00Z",
            )

    def test_legacy_import_and_empirical_comparison_are_idempotent(self):
        record = {
            "run_id": "legacy-run-1",
            "site_id": "test_site",
            "forecast_issued_at": "2026-06-20T15:00:00Z",
            "model_version": "legacy-test",
            "forecast_days": [
                {
                    "target_date": "2026-06-21",
                    "forecast_horizon_hours": 31,
                    "expected_kwh_total": 100.0,
                    "expected_kwh_by_direction": {"south": 100.0},
                    "weather": {"dominant_weather": "fair_day"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.ndjson"
            legacy.write_text(json.dumps(record) + "\n", encoding="utf-8")
            history = ForecastHistory(root / "history.sqlite")
            first = history.import_legacy_ndjson(legacy)
            second = history.import_legacy_ndjson(legacy)
            self.assertEqual(first["imported_runs"], 1)
            self.assertEqual(second["imported_runs"], 0)

            _, inserted = history.append_actual(
                "test_site",
                "2026-06-21",
                110.0,
                "test_meter",
            )
            _, duplicate = history.append_actual(
                "test_site",
                "2026-06-21",
                110.0,
                "test_meter",
            )
            self.assertTrue(inserted)
            self.assertFalse(duplicate)
            comparison = history.comparisons(
                "test_site", "Europe/Oslo", limit=30
            )[0]
            self.assertEqual(comparison["forecast_kwh"], 100.0)
            self.assertEqual(comparison["actual_kwh"], 110.0)
            self.assertEqual(comparison["deviation_kwh"], 10.0)
            metrics = history.empirical_metrics(
                "test_site", "Europe/Oslo", limit=30
            )
            self.assertEqual(metrics["days"], 1)
            self.assertEqual(metrics["mae_kwh"], 10.0)

    def test_invalid_zero_empirics_can_be_quarantined_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ForecastHistory(Path(directory) / "history.sqlite")
            observation_id, inserted = history.append_actual(
                "test_site",
                "2026-06-21",
                0.0,
                "home_assistant_recorder_statistics",
                observed_at="2026-06-22T00:45:00Z",
            )
            self.assertTrue(inserted)
            findings = history.audit_daily_actuals("test_site")
            self.assertEqual(len(findings), 1)
            self.assertIn("daily_total_below_minimum", findings[0]["reasons"])
            result = history.exclude_observations(
                "test_site",
                "daily",
                [observation_id],
                "test_invalid_zero",
            )
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(history.list_actuals("test_site"), [])
            with history._connect() as connection:
                preserved = connection.execute(
                    "SELECT actual_kwh_total FROM actual_observations WHERE observation_id = ?",
                    (observation_id,),
                ).fetchone()
            self.assertEqual(preserved["actual_kwh_total"], 0.0)

    def test_physically_impossible_daily_empirics_are_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ForecastHistory(Path(directory) / "history.sqlite")
            history.append_actual(
                "test_site",
                "2026-06-21",
                250.0,
                "home_assistant_recorder_statistics",
            )
            findings = history.audit_daily_actuals(
                "test_site", maximum_daily_kwh=160.0
            )
            self.assertEqual(len(findings), 1)
            self.assertIn(
                "daily_specific_yield_above_maximum", findings[0]["reasons"]
            )

    def test_hourly_forecast_actual_comparison_is_daylight_only(self):
        issued = datetime(2026, 6, 20, 15, tzinfo=UTC)
        forecast = build_forecast(
            sample_config(),
            met_payload(datetime(2026, 6, 21, 0, tzinfo=UTC), hours=24),
            issued_at=issued,
        )
        with tempfile.TemporaryDirectory() as directory:
            history = ForecastHistory(Path(directory) / "history.sqlite")
            run_id, inserted = history.append(
                forecast,
                raw_source={
                    "provider": "test_archive",
                    "model": "test_model",
                    "model_run_at": "2026-06-20T12:00:00Z",
                    "request": {"safe": True},
                    "payload": {"raw": "preserved"},
                },
            )
            self.assertTrue(inserted)
            daylight = next(
                row
                for row in history.list_hourly(
                    "test_site", target_date="2026-06-21", run_id=run_id
                )
                if row["is_day"] and row["expected_kwh_total"] > 0
            )
            history.append_hourly_actual(
                "test_site",
                daylight["target_time"],
                actual_pv_kwh=daylight["expected_kwh_total"] + 1,
                actual_temperature_c=22,
                source="test_recorder",
            )
            result = history.hourly_comparisons(
                "test_site", "2026-06-21", "Europe/Oslo"
            )
            self.assertTrue(result["hours"])
            self.assertTrue(all(row["solar_elevation_deg"] > 0 for row in result["hours"]))
            compared = next(
                row
                for row in result["hours"]
                if row["target_time"] == daylight["target_time"]
            )
            self.assertAlmostEqual(compared["deviation_kwh"], 1.0)
            self.assertEqual(compared["actual_temperature_c"], 22)
            self.assertEqual(history.prune(1), 0)
            self.assertTrue(
                history.list_hourly(
                    "test_site", target_date="2026-06-21", run_id=run_id
                )
            )


class BackfillTests(unittest.TestCase):
    def test_cutoff_and_selected_run_have_explicit_semantics(self):
        cutoff = comparison_cutoff("2026-07-17", "Europe/Oslo")
        selected = select_model_run("2026-07-17", "Europe/Oslo")
        self.assertEqual(cutoff.isoformat(), "2026-07-16T16:00:00+00:00")
        self.assertEqual(selected.isoformat(), "2026-07-16T12:00:00+00:00")

    def test_open_meteo_conversion_preserves_hourly_weather(self):
        payload = {
            "hourly": {
                "time": ["2026-07-17T10:00"],
                "temperature_2m": [23.5],
                "cloud_cover": [70],
                "cloud_cover_low": [None],
                "cloud_cover_mid": [None],
                "cloud_cover_high": [None],
                "is_day": [1],
                "shortwave_radiation": [600],
                "direct_radiation": [350],
                "diffuse_radiation": [250],
                "direct_normal_irradiance": [500],
            }
        }
        converted = open_meteo_to_engine_payload(
            payload,
            {
                "2026-07-17T10:00:00Z": {
                    "cloud_cover_low_percent": 20,
                    "cloud_cover_medium_percent": 30,
                    "cloud_cover_high_percent": 40,
                }
            },
        )
        details = converted["properties"]["timeseries"][0]["data"]["instant"][
            "details"
        ]
        self.assertEqual(details["air_temperature"], 23.5)
        self.assertEqual(details["cloud_area_fraction"], 70)
        self.assertEqual(details["cloud_area_fraction_low"], 20)
        self.assertEqual(details["shortwave_radiation"], 600)

    def test_meps_projection_hits_verified_hf39_grid_cell(self):
        self.assertEqual(
            MepsThreddsClient.grid_index(59.7736631, 9.9051538),
            (310, 381),
        )


class CalibrationTests(unittest.TestCase):
    def test_learning_never_mixes_measurement_source_fingerprints(self):
        current = "current-source"

        class FakeHistory:
            dates = ["2026-06-18", "2026-06-19"]

            def hourly_actual_dates(self, site_id, timezone_name, limit=180):
                return self.dates

            def list_actuals(self, site_id, limit=180):
                return [
                    {
                        "target_date": value,
                        "details": {
                            "quality_valid": True,
                            "measurement_source_fingerprint": (
                                current if value.endswith("19") else "legacy-source"
                            ),
                        },
                    }
                    for value in self.dates
                ]

            def hourly_comparisons(self, site_id, target_date, timezone_name):
                fingerprint = (
                    current if target_date.endswith("19") else "legacy-source"
                )
                return {
                    "hours": [
                        {
                            "target_time": f"{target_date}T{hour:02d}:00:00Z",
                            "forecast_kwh": 1.0,
                            "actual_kwh": 1.1,
                            "actual_temperature_c": 20.0,
                            "forecast_temperature_c": 20.0,
                            "solar_elevation_deg": 20.0,
                            "actual_quality_valid": True,
                            "actual_measurement_source_fingerprint": fingerprint,
                        }
                        for hour in range(8, 12)
                    ]
                }

        learned = fit_calibration(
            FakeHistory(),
            "test_site",
            "Europe/Oslo",
            minimum_samples=4,
            minimum_complete_days=1,
            minimum_valid_hours_per_day=4,
            required_measurement_source_fingerprint=current,
        )
        self.assertIsNotNone(learned)
        self.assertEqual(learned["training_days"], ["2026-06-19"])
        self.assertEqual(learned["sample_count"], 4)
        self.assertEqual(learned["measurement_source_fingerprint"], current)
        self.assertTrue(
            learned["guardrails"]["requires_current_measurement_source"]
        )

    def test_bounded_temperature_calibration_is_traceable(self):
        start = datetime(2026, 6, 21, 0, tzinfo=UTC)
        forecast = build_forecast(
            sample_config(), met_payload(start), issued_at=start - timedelta(hours=1)
        )
        before = forecast["forecast_summary"]["expected_kwh_total"]
        calibration = {
            "calibration_id": "cal-test",
            "accepted": True,
            "algorithm_version": "bounded-temperature-residual-1",
            "fitted_at": "2026-07-01T00:00:00Z",
            "training_start": "2026-06-01T00:00:00Z",
            "training_end": "2026-06-30T23:00:00Z",
            "sample_count": 100,
            "intercept_factor": 1.1,
            "temperature_slope_per_c": -0.004,
            "mae_before_kwh": 1.0,
            "mae_after_kwh": 0.8,
        }
        apply_calibration(forecast, calibration)
        self.assertNotEqual(
            forecast["forecast_summary"]["expected_kwh_total"], before
        )
        self.assertEqual(
            forecast["forecast_summary"]["calibration"]["calibration_id"],
            "cal-test",
        )
        factors = [
            row["calibration_factor"]
            for row in forecast["hourly_forecast"]
        ]
        self.assertTrue(all(0.70 <= value <= 1.30 for value in factors))

    def test_false_zero_hours_are_never_training_samples(self):
        class FakeHistory:
            def hourly_actual_dates(self, site_id, timezone_name, limit=180):
                return ["2026-06-21"]

            def list_actuals(self, site_id, limit=180):
                return [
                    {
                        "target_date": "2026-06-21",
                        "details": {"quality_valid": True},
                    }
                ]

            def hourly_comparisons(self, site_id, target_date, timezone_name):
                return {
                    "hours": [
                        {
                            "target_time": f"2026-06-21T{hour:02d}:00:00Z",
                            "forecast_kwh": 1.0,
                            "actual_kwh": 0.0,
                            "actual_temperature_c": 20.0,
                            "forecast_temperature_c": 20.0,
                            "solar_elevation_deg": 20.0,
                            "actual_quality_valid": True,
                        }
                        for hour in range(10)
                    ]
                }

        learned = fit_calibration(
            FakeHistory(),
            "test_site",
            "Europe/Oslo",
            minimum_samples=1,
            minimum_complete_days=1,
            minimum_valid_hours_per_day=1,
        )
        self.assertIsNone(learned)

    def test_learning_requires_multiple_trusted_completed_days(self):
        class FakeHistory:
            dates = ["2026-06-18", "2026-06-19", "2026-06-20"]

            def hourly_actual_dates(self, site_id, timezone_name, limit=180):
                return self.dates

            def list_actuals(self, site_id, limit=180):
                return [
                    {
                        "target_date": value,
                        "details": {"quality_valid": True},
                    }
                    for value in self.dates
                ]

            def hourly_comparisons(self, site_id, target_date, timezone_name):
                return {
                    "hours": [
                        {
                            "target_time": f"{target_date}T{hour:02d}:00:00Z",
                            "forecast_kwh": 1.0,
                            "actual_kwh": 1.1,
                            "actual_temperature_c": 15.0 + hour,
                            "forecast_temperature_c": 15.0 + hour,
                            "solar_elevation_deg": 20.0,
                            "actual_quality_valid": True,
                        }
                        for hour in range(8, 12)
                    ]
                }

        history = FakeHistory()
        history.dates = history.dates[:2]
        self.assertIsNone(
            fit_calibration(
                history,
                "test_site",
                "Europe/Oslo",
                minimum_samples=1,
                minimum_complete_days=3,
                minimum_valid_hours_per_day=4,
            )
        )
        history.dates = ["2026-06-18", "2026-06-19", "2026-06-20"]
        learned = fit_calibration(
            history,
            "test_site",
            "Europe/Oslo",
            minimum_samples=12,
            minimum_complete_days=3,
            minimum_valid_hours_per_day=4,
        )
        self.assertIsNotNone(learned)
        self.assertEqual(learned["training_day_count"], 3)
        self.assertEqual(len(learned["training_days"]), 3)
        self.assertTrue(
            learned["guardrails"]["requires_trusted_daily_observation"]
        )


class RegulatorPlannerTests(unittest.TestCase):
    NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)

    def regulator_forecast(self, solar_by_hour, cloud=10):
        rows = []
        for index, solar in enumerate(solar_by_hour):
            start = self.NOW + timedelta(hours=index)
            rows.append(
                {
                    "time": start.isoformat().replace("+00:00", "Z"),
                    "interval_hours": 1,
                    "expected_kwh": solar,
                    "cloud_area_fraction_percent": cloud,
                }
            )
        return {
            "forecast_summary": {
                "forecast_issued_at": (
                    self.NOW - timedelta(minutes=10)
                ).isoformat().replace("+00:00", "Z")
            },
            "hourly_forecast": rows,
        }

    def regulator_snapshot(self, **overrides):
        snapshot = {
            "observed_at": self.NOW.isoformat().replace("+00:00", "Z"),
            "soc_percent": 80,
            "usable_battery_kwh": 30,
            "minimum_soc_percent": 20,
            "target_checkpoint_soc_percent": 95,
            "checkpoint_at": (
                self.NOW + timedelta(hours=24)
            ).isoformat().replace("+00:00", "Z"),
            "base_load_kw": 1,
            "absolute_import_limit_kw": 4.7,
            "grid_margin_kw": 0.5,
            "work_limit_options": [
                -4.7, -2, -1, -0.5, -0.25, 0, 0.25, 0.5, 1, 2, 4.7
            ],
            "minimum_active_export_kw": 0.25,
            "maximum_export_kw": 4.7,
        }
        snapshot.update(overrides)
        return snapshot

    def regulator_prices(self, hours=72, cheapest_first=False):
        rows = []
        for index in range(hours):
            rows.append(
                {
                    "start": (
                        self.NOW + timedelta(hours=index)
                    ).isoformat().replace("+00:00", "Z"),
                    "price_nok_per_kwh": (
                        0.1 if cheapest_first and index == 0 else 1 + index / 100
                    ),
                }
            )
        return rows

    def test_sunny_plan_funds_reserve_before_export(self):
        solar = ([0] * 6 + [10] * 8 + [0] * 10) * 3
        plan = build_observation_plan(
            sample_config("sunny"),
            self.regulator_forecast(solar),
            self.regulator_snapshot(),
            self.regulator_prices(),
        )
        self.assertEqual(plan["plan_version"], PLAN_VERSION)
        self.assertEqual(plan["mode"], "observe_only")
        self.assertFalse(plan["actuation_authorized"])
        self.assertGreater(plan["export_budget_kwh"], 0)
        self.assertLess(plan["selected_work_limit_option"], 0)
        self.assertEqual(plan["import_need_kwh"], 0)
        self.assertEqual(len(plan["decision_id"]), 64)
        repeated = build_observation_plan(
            sample_config("sunny"),
            self.regulator_forecast(solar),
            self.regulator_snapshot(),
            self.regulator_prices(),
        )
        self.assertEqual(plan["decision_id"], repeated["decision_id"])

    def test_cloud_period_schedules_early_import_before_reserve_breach(self):
        plan = build_observation_plan(
            sample_config("cloudy"),
            self.regulator_forecast([0] * 72, cloud=95),
            self.regulator_snapshot(
                soc_percent=30,
                usable_battery_kwh=12,
                base_load_kw=1.5,
            ),
            self.regulator_prices(cheapest_first=True),
        )
        self.assertGreater(plan["import_need_kwh"], 0)
        self.assertIsNotNone(plan["latest_safe_import_start"])
        self.assertGreater(plan["hours"][0]["planned_import_kwh"], 0)
        self.assertGreater(plan["selected_work_limit_option"], 0)
        self.assertTrue(plan["data_quality"]["import_schedule_feasible"])
        self.assertIn(
            plan["soc_checkpoint_feasibility"],
            ("achievable", "at_risk"),
        )

    def test_import_squeeze_is_explicitly_infeasible(self):
        plan = build_observation_plan(
            sample_config("squeeze"),
            self.regulator_forecast([0] * 72, cloud=95),
            self.regulator_snapshot(
                soc_percent=30,
                usable_battery_kwh=12,
                base_load_kw=1.5,
                absolute_import_limit_kw=2.0,
                grid_margin_kw=0.5,
            ),
            self.regulator_prices(),
        )
        self.assertFalse(plan["data_quality"]["import_schedule_feasible"])
        self.assertEqual(
            plan["soc_checkpoint_feasibility"],
            "infeasible_without_import_or_flexible_load",
        )
        self.assertEqual(sum(row["planned_import_kwh"] for row in plan["hours"]), 0)
        self.assertEqual(plan["proposed_work_limit_kw"], 0)

    def test_missing_timestamp_fails_closed(self):
        snapshot = self.regulator_snapshot()
        snapshot.pop("observed_at")
        with self.assertRaisesRegex(RegulatorInputError, "observed_at"):
            build_observation_plan(
                sample_config("invalid"),
                self.regulator_forecast([0] * 24),
                snapshot,
                self.regulator_prices(24),
            )

    def test_quarter_hour_prices_are_duration_weighted_not_overwritten(self):
        prices = []
        for index, price in enumerate([0.1, 0.2, 0.3, 0.8]):
            start = self.NOW + timedelta(minutes=15 * index)
            prices.append(
                {
                    "start": start.isoformat().replace("+00:00", "Z"),
                    "end": (start + timedelta(minutes=15))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "price_nok_per_kwh": price,
                }
            )
        prices.extend(self.regulator_prices(71)[1:])
        plan = build_observation_plan(
            sample_config("quarter-hour"),
            self.regulator_forecast([0] * 72, cloud=95),
            self.regulator_snapshot(soc_percent=100, base_load_kw=0.1),
            prices,
        )
        self.assertAlmostEqual(
            plan["hours"][0]["nord_pool_raw_nok_per_kwh"], 0.35
        )

    def test_regulator_plan_storage_is_idempotent_append_only(self):
        config = sample_config("stored")
        forecast = self.regulator_forecast(([0] * 6 + [10] * 8 + [0] * 10) * 3)
        snapshot = self.regulator_snapshot()
        prices = self.regulator_prices()
        plan = build_observation_plan(config, forecast, snapshot, prices)
        inputs = {
            "site_config": config,
            "forecast": forecast,
            "snapshot": snapshot,
            "prices": prices,
        }
        with tempfile.TemporaryDirectory() as directory:
            history = ForecastHistory(Path(directory) / "history.sqlite")
            decision_id, inserted = history.append_regulator_plan(plan, inputs)
            self.assertTrue(inserted)
            repeated_id, repeated_inserted = history.append_regulator_plan(
                plan, inputs
            )
            self.assertEqual(repeated_id, decision_id)
            self.assertFalse(repeated_inserted)
            stored = history.regulator_plan(decision_id)
            self.assertEqual(stored["input"], inputs)
            self.assertEqual(stored["plan"], plan)
            self.assertEqual(len(history.list_regulator_plans("stored")), 1)
            with history._connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE regulator_plans SET plan_version = 'changed' "
                        "WHERE decision_id = ?",
                        (decision_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM regulator_plans WHERE decision_id = ?",
                        (decision_id,),
                    )
            conflicting = copy.deepcopy(plan)
            conflicting["reason"] = "different"
            with self.assertRaisesRegex(ValueError, "collision"):
                history.append_regulator_plan(conflicting, inputs)

    def test_old_database_adds_regulator_table_without_changing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pre-regulator.sqlite"
            history = ForecastHistory(source)
            history.append_actual(
                "migration-site",
                "2026-08-01",
                12.3,
                "migration-test",
            )
            with history._connect() as connection:
                connection.execute("DROP TABLE regulator_plans")
            result = rehearse(source, "sqlite")
            self.assertTrue(result["passed"])
            self.assertIn(
                "regulator_plans",
                result["checks"]["additive_schema_tables"],
            )
            self.assertEqual(
                result["preserved_tables"]["actual_observations"]["rows"],
                1,
            )


class HACollectorTests(unittest.TestCase):
    NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)

    def config(self, **entity_overrides):
        config = sample_config("collector-site")
        config["load_model"] = {
            "daily_energy": {"fallback_kwh_per_day": 43.0}
        }
        entities = {
            "battery_soc": "sensor.battery_soc",
            "work_limit": "input_select.deye_work_limit",
            "absolute_import_limit": "input_select.absolute_grid_limit",
            "grid_power": "sensor.grid_power",
        }
        entities.update(entity_overrides)
        config["energy_regulator_vnext"] = {
            "collector": {
                "enabled": True,
                "maximum_state_age_seconds": 900,
                "sign_convention": "positive_import_negative_export",
                "entities": entities,
                "nord_pool": {
                    "enabled": True,
                    "entities": ["sensor.nord_pool_today"],
                    "intervals_attribute": "intervals",
                },
                "static": {
                    "usable_battery_kwh": 36,
                    "minimum_soc_percent": 20,
                    "target_checkpoint_soc_percent": 95,
                    "checkpoint_at": "2026-08-03T18:00:00Z",
                    "grid_margin_kw": 0.5,
                    "minimum_active_export_kw": 0.25,
                    "maximum_export_kw": 9.7,
                },
            }
        }
        return config

    def state(self, entity_id, value, unit="", attributes=None, minutes_old=1):
        attrs = dict(attributes or {})
        if unit:
            attrs["unit_of_measurement"] = unit
        return {
            "entity_id": entity_id,
            "state": str(value),
            "attributes": attrs,
            "last_updated": (self.NOW - timedelta(minutes=minutes_old))
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def prices(self, count=96, start=None):
        start = start or self.NOW.replace(hour=0)
        return [
            {
                "start": (start + timedelta(minutes=15 * index))
                .isoformat()
                .replace("+00:00", "Z"),
                "end": (start + timedelta(minutes=15 * (index + 1)))
                .isoformat()
                .replace("+00:00", "Z"),
                "price": 0.2 + index / 1000,
            }
            for index in range(count)
        ]

    def valid_states(self):
        return [
            self.state("sensor.battery_soc", 81, "%"),
            self.state(
                "input_select.deye_work_limit",
                -0.25,
                attributes={"options": ["-0.5", "-0.25", "0", "0.25", "4.7"]},
            ),
            self.state("input_select.absolute_grid_limit", 4.7),
            self.state("sensor.grid_power", -510, "W"),
            self.state(
                "sensor.nord_pool_today",
                "ready",
                attributes={"intervals": self.prices()},
            ),
        ]

    def test_valid_snapshot_preserves_native_quarter_hours_and_units(self):
        result = collect_regulator_inputs(
            self.config(), self.valid_states(), self.NOW.isoformat()
        )
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["schema"], COLLECTOR_SCHEMA)
        self.assertEqual(snapshot["soc_percent"], 81)
        self.assertAlmostEqual(snapshot["grid_power_kw"], -0.51)
        self.assertAlmostEqual(snapshot["base_load_kw"], 43 / 24)
        self.assertEqual(snapshot["base_load_source"], "site_profile_daily_fallback")
        self.assertEqual(len(result["prices"]), 96)
        self.assertTrue(all(row["interval_minutes"] == 15 for row in result["prices"]))
        self.assertFalse(snapshot["data_quality"]["actuation_authorized"])

    def test_stale_or_unavailable_required_state_fails_closed(self):
        stale = self.valid_states()
        stale[0] = self.state("sensor.battery_soc", 81, "%", minutes_old=20)
        with self.assertRaisesRegex(HACollectorError, "stale"):
            collect_regulator_inputs(self.config(), stale, self.NOW.isoformat())
        unavailable = self.valid_states()
        unavailable[0] = self.state("sensor.battery_soc", "unavailable", "%")
        with self.assertRaisesRegex(HACollectorError, "unavailable"):
            collect_regulator_inputs(
                self.config(), unavailable, self.NOW.isoformat()
            )

    def test_entity_swap_changes_source_fingerprint(self):
        original = collect_regulator_inputs(
            self.config(), self.valid_states(), self.NOW.isoformat()
        )
        swapped_states = self.valid_states()
        swapped_states[0] = self.state("sensor.replacement_soc", 81, "%")
        swapped = collect_regulator_inputs(
            self.config(battery_soc="sensor.replacement_soc"),
            swapped_states,
            self.NOW.isoformat(),
        )
        self.assertNotEqual(
            original["snapshot"]["source_fingerprint"],
            swapped["snapshot"]["source_fingerprint"],
        )

    def test_nord_pool_gap_is_not_invented(self):
        states = self.valid_states()
        intervals = self.prices()
        del intervals[20]
        states[-1] = self.state(
            "sensor.nord_pool_today", "ready", attributes={"intervals": intervals}
        )
        with self.assertRaisesRegex(HACollectorError, "gap or overlap"):
            collect_regulator_inputs(self.config(), states, self.NOW.isoformat())

    def test_missing_tomorrow_is_explicit_and_never_invented(self):
        config = self.config()
        config["energy_regulator_vnext"]["collector"]["nord_pool"]["entities"] = [
            "sensor.nord_pool_today",
            "sensor.nord_pool_tomorrow",
        ]
        result = collect_regulator_inputs(
            config, self.valid_states(), self.NOW.isoformat()
        )
        self.assertEqual(len(result["prices"]), 96)
        self.assertEqual(result["price_quality"]["status"], "incomplete")
        self.assertEqual(
            result["price_quality"]["missing_entities"],
            ["sensor.nord_pool_tomorrow"],
        )
        self.assertFalse(
            result["snapshot"]["data_quality"]["safe_for_aggressive_export"]
        )

    def test_work_limit_must_be_one_of_live_input_select_options(self):
        states = self.valid_states()
        states[1] = self.state(
            "input_select.deye_work_limit",
            -0.75,
            attributes={"options": ["-0.5", "-0.25", "0", "0.25"]},
        )
        with self.assertRaisesRegex(HACollectorError, "not present"):
            collect_regulator_inputs(self.config(), states, self.NOW.isoformat())

    def test_next_soc_checkpoint_uses_first_future_forecast_sunset(self):
        forecast = {
            "daily_forecast": [
                {"sunset": "2026-08-02T20:30:00Z"},
                {"sunset": "2026-08-03T20:28:00Z"},
            ]
        }
        self.assertEqual(
            next_soc_checkpoint(forecast, self.NOW.isoformat(), 60),
            "2026-08-02T19:30:00Z",
        )

    def test_service_reads_api_states_and_injects_dynamic_checkpoint(self):
        config = self.config()
        config["planning"] = {"soc_checkpoint": {"minutes_before_sunset": 60}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "site.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            options = {
                **addon_app.DEFAULT_OPTIONS,
                "site_config_path": str(config_path),
            }
            with mock.patch.dict(
                os.environ,
                {
                    "SOLAR_FORECAST_DATA_DIR": str(root),
                    "SUPERVISOR_TOKEN": "test-token",
                },
            ):
                service = addon_app.ServiceState(options)
                service.forecast = {
                    "daily_forecast": [{"sunset": "2026-08-02T20:30:00Z"}]
                }
                payload = io.BytesIO(json.dumps(self.valid_states()).encode("utf-8"))
                with mock.patch(
                    "urllib.request.urlopen", return_value=payload
                ) as request:
                    result = service.collect_regulator_observation(
                        self.NOW.isoformat()
                    )
            self.assertEqual(
                result["snapshot"]["checkpoint_at"], "2026-08-02T19:30:00Z"
            )
            self.assertEqual(request.call_args.args[0].method, "GET")
            self.assertEqual(
                request.call_args.args[0].full_url,
                "http://supervisor/core/api/states",
            )


class HistoryDatabaseImportTests(unittest.TestCase):
    def test_import_preserves_existing_database_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "preserved.sqlite"
            destination = root / "data" / "forecast-history.sqlite"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE preserved (value TEXT)")
                connection.execute("INSERT INTO preserved VALUES ('forecast')")

            result = addon_app.import_history_database_once(source, destination)
            self.assertTrue(result["imported"])
            with sqlite3.connect(destination) as connection:
                value = connection.execute(
                    "SELECT value FROM preserved"
                ).fetchone()[0]
            self.assertEqual(value, "forecast")

            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO preserved VALUES ('new')")
            repeated = addon_app.import_history_database_once(source, destination)
            self.assertFalse(repeated["imported"])
            with sqlite3.connect(destination) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM preserved"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_import_rejects_corrupt_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corrupt.sqlite"
            destination = root / "data" / "forecast-history.sqlite"
            source.write_bytes(b"not sqlite")
            with self.assertRaises(sqlite3.DatabaseError):
                addon_app.import_history_database_once(source, destination)
            self.assertFalse(destination.exists())


class ApiTests(unittest.TestCase):
    def test_dashboard_translations_cover_all_languages_and_keys(self):
        expected_languages = {"no", "en", "pt", "es", "uk", "de"}
        self.assertEqual(
            set(addon_app.DASHBOARD_TRANSLATIONS), expected_languages
        )
        norwegian_keys = set(addon_app.DASHBOARD_TRANSLATIONS["no"])
        self.assertIn("hoursAhead", norwegian_keys)
        self.assertEqual(
            addon_app.DASHBOARD_TRANSLATIONS["no"]["hoursAhead"],
            "Timer frem",
        )
        for translations in addon_app.DASHBOARD_TRANSLATIONS.values():
            self.assertEqual(set(translations), norwegian_keys)

    def test_existing_history_quarantine_is_explicit_reversible_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = sample_config()
            config["measurements"] = {
                "solar_energy": {
                    "statistic_entities": ["sensor.pv_total"],
                    "data_quality": {
                        "require_all_entities": True,
                        "minimum_daily_total_kwh": 0.05,
                        "maximum_daily_specific_yield_kwh_per_kwp": 8.0,
                        "auto_quarantine_existing": False,
                    },
                }
            }
            config_path = root / "site.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            options = {
                **addon_app.DEFAULT_OPTIONS,
                "site_config_path": str(config_path),
            }
            with mock.patch.dict(
                os.environ, {"SOLAR_FORECAST_DATA_DIR": str(root)}
            ):
                state = addon_app.ServiceState(options)
            for target_date, value in (
                ("2026-06-18", 0.0),
                ("2026-06-19", 250.0),
                ("2026-06-20", 100.0),
            ):
                state.history.append_actual(
                    "test_site", target_date, value, "legacy_recorder"
                )
            state.config["measurements"]["solar_energy"]["data_quality"][
                "auto_quarantine_existing"
            ] = True
            first = state._quarantine_existing_actuals()
            second = state._quarantine_existing_actuals()
            self.assertEqual(first["inserted"], 2)
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(
                state.history.exclusion_summary("test_site")["total"], 2
            )
            with state.history._connect() as connection:
                raw_count = connection.execute(
                    "SELECT COUNT(*) FROM actual_observations"
                ).fetchone()[0]
            self.assertEqual(raw_count, 3)

    def test_measurement_source_fingerprint_is_stable_and_entity_sensitive(self):
        first = addon_app._measurement_source_details(
            ["sensor.pv_b", "sensor.pv_a"], "sum", "kWh"
        )
        reordered = addon_app._measurement_source_details(
            ["sensor.pv_a", "sensor.pv_b"], "sum", "kWh"
        )
        changed = addon_app._measurement_source_details(
            ["sensor.pv_a", "sensor.pv_c"], "sum", "kWh"
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(
            first["measurement_source_fingerprint"],
            changed["measurement_source_fingerprint"],
        )
        self.assertEqual(
            first["measurement_source"]["entity_ids"],
            ["sensor.pv_a", "sensor.pv_b"],
        )

    def test_empirical_health_requires_latest_completed_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "site.json"
            config_path.write_text(json.dumps(sample_config()), encoding="utf-8")
            options = {
                **addon_app.DEFAULT_OPTIONS,
                "site_config_path": str(config_path),
            }
            with mock.patch.dict(
                os.environ, {"SOLAR_FORECAST_DATA_DIR": str(root)}
            ):
                state = addon_app.ServiceState(options)
            state.config["measurements"] = {
                "solar_energy": {
                    "statistic_entities": ["sensor.pv_total"],
                    "data_quality": {
                        "require_all_entities": True,
                        "minimum_daily_total_kwh": 0.05,
                    },
                }
            }
            zone = addon_app.ZoneInfo("Europe/Oslo")
            local_today = datetime.now(zone).date()
            old_date = local_today - timedelta(days=2)
            latest_date = local_today - timedelta(days=1)

            def response_for(value_date):
                start = datetime.combine(
                    value_date,
                    datetime.min.time(),
                    tzinfo=zone,
                ).astimezone(UTC)
                payload = {
                    "sensor.pv_total": [
                        {
                            "start": start.isoformat(),
                            "change": 42.0,
                        }
                    ]
                }
                return io.BytesIO(json.dumps(payload).encode("utf-8"))

            with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-token"}):
                with mock.patch(
                    "urllib.request.urlopen", return_value=response_for(old_date)
                ):
                    state.refresh_actuals()
                self.assertIsNone(state.last_empirical_success_at)
                self.assertIn(
                    "latest completed day", state.last_empirical_error
                )
                with mock.patch(
                    "urllib.request.urlopen", return_value=response_for(latest_date)
                ):
                    state.refresh_actuals()
                self.assertIsNotNone(state.last_empirical_success_at)
                self.assertIsNone(state.last_empirical_error)

    def test_recorder_requests_normalize_energy_and_temperature_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "site.json"
            config_path.write_text(json.dumps(sample_config()), encoding="utf-8")
            options = {
                **addon_app.DEFAULT_OPTIONS,
                "site_config_path": str(config_path),
            }
            with mock.patch.dict(
                os.environ,
                {"SOLAR_FORECAST_DATA_DIR": str(root)},
            ):
                state = addon_app.ServiceState(options)
            state.config["measurements"] = {
                "solar_energy": {
                    "statistic_entities": ["sensor.pv_total"],
                    "data_quality": {"require_all_entities": True},
                },
                "outdoor_temperature_statistic_entity": "sensor.outdoor_temp",
            }
            with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-token"}):
                with mock.patch(
                    "urllib.request.urlopen", side_effect=RuntimeError("stop")
                ) as daily_open:
                    state.refresh_actuals()
                daily_request = daily_open.call_args.args[0]
                self.assertEqual(
                    json.loads(daily_request.data)["units"], {"energy": "kWh"}
                )
                with mock.patch(
                    "urllib.request.urlopen", side_effect=RuntimeError("stop")
                ) as hourly_open:
                    state.refresh_hourly_actuals()
                hourly_request = hourly_open.call_args.args[0]
                self.assertEqual(
                    json.loads(hourly_request.data)["units"],
                    {"energy": "kWh", "temperature": "°C"},
                )

                complete_hour = datetime.now(UTC).replace(
                    minute=0, second=0, microsecond=0
                ) - timedelta(hours=2)
                successful_payload = {
                    "sensor.pv_total": [
                        {
                            "start": complete_hour.isoformat(),
                            "change": 2.5,
                        }
                    ],
                    "sensor.outdoor_temp": [
                        {
                            "start": complete_hour.isoformat(),
                            "mean": 18.0,
                        }
                    ],
                }
                with mock.patch(
                    "urllib.request.urlopen",
                    return_value=io.BytesIO(
                        json.dumps(successful_payload).encode("utf-8")
                    ),
                ):
                    state.refresh_hourly_actuals()
                with state.history._connect() as connection:
                    stored = connection.execute(
                        "SELECT actual_pv_kwh, details_json "
                        "FROM hourly_actual_observations"
                    ).fetchone()
                self.assertEqual(stored["actual_pv_kwh"], 2.5)
                details = json.loads(stored["details_json"])
                self.assertTrue(details["quality_valid"])
                self.assertIn("measurement_source_fingerprint", details)

    def test_http_forecast_and_history_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "site.json"
            api_config = sample_config()
            api_config["energy_regulator_vnext"] = {
                "enabled": False,
                "mode": "observe_only",
                "actuation_authorized": False,
            }
            config_path.write_text(json.dumps(api_config), encoding="utf-8")
            options = {
                **addon_app.DEFAULT_OPTIONS,
                "site_config_path": str(config_path),
                "history_days": 730,
            }
            with mock.patch.dict(
                os.environ,
                {"SOLAR_FORECAST_DATA_DIR": str(directory_path)},
            ):
                state = addon_app.ServiceState(options)
            with self.assertRaises(ValueError):
                state.import_empirics(
                    {
                        "site_id": "test_site",
                        "source": "test_import",
                        "daily": [
                            {
                                "target_date": "2026-06-20",
                                "actual_kwh_total": 999.0,
                            }
                        ],
                    }
                )
            weather = met_payload(datetime.now(UTC).replace(minute=0, second=0, microsecond=0))
            with mock.patch.object(
                state,
                "_fetch_weather",
                return_value=(weather, "test"),
            ):
                state.refresh()

            server = addon_app.ForecastServer(
                ("127.0.0.1", 0),
                addon_app.RequestHandler,
                state,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base) as response:
                    dashboard = response.read().decode("utf-8")
                self.assertIn('id="language"', dashboard)
                self.assertIn('value="uk"', dashboard)
                self.assertIn('data-i18n="hoursAhead"', dashboard)
                self.assertNotIn("Horizon h", dashboard)
                with urllib.request.urlopen(f"{base}/api/forecast") as response:
                    forecast = json.load(response)
                self.assertEqual(forecast["site"]["id"], "test_site")
                self.assertTrue(forecast["snapshot_storage"]["append_only"])

                observed_at = datetime.now(UTC)
                price_hour = observed_at.replace(minute=0, second=0, microsecond=0)
                regulator_payload = {
                    "snapshot": {
                        "observed_at": observed_at.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "soc_percent": 80,
                        "usable_battery_kwh": 30,
                        "minimum_soc_percent": 20,
                        "target_checkpoint_soc_percent": 95,
                        "checkpoint_at": (
                            observed_at + timedelta(hours=24)
                        ).isoformat().replace("+00:00", "Z"),
                        "base_load_kw": 1,
                        "absolute_import_limit_kw": 4.7,
                        "grid_margin_kw": 0.5,
                        "work_limit_options": [-1, -0.5, -0.25, 0, 0.5, 1, 2, 4.7],
                    },
                    "prices": [
                        {
                            "start": (
                                price_hour + timedelta(hours=index)
                            ).isoformat().replace("+00:00", "Z"),
                            "price_nok_per_kwh": 1 + index / 100,
                        }
                        for index in range(72)
                    ],
                }
                request = urllib.request.Request(
                    f"{base}/api/regulator/plan",
                    data=json.dumps(regulator_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    regulator_plan = json.load(response)
                self.assertEqual(regulator_plan["mode"], "observe_only")
                self.assertFalse(regulator_plan["actuation_authorized"])
                self.assertEqual(regulator_plan["site_id"], "test_site")
                self.assertTrue(regulator_plan["hours"])
                self.assertTrue(regulator_plan["snapshot_storage"]["inserted"])
                decision_id = regulator_plan["decision_id"]
                repeated_request = urllib.request.Request(
                    f"{base}/api/regulator/plan",
                    data=json.dumps(regulator_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(repeated_request) as response:
                    repeated_plan = json.load(response)
                self.assertEqual(repeated_plan["decision_id"], decision_id)
                self.assertFalse(repeated_plan["snapshot_storage"]["inserted"])
                with urllib.request.urlopen(
                    f"{base}/api/regulator/history"
                ) as response:
                    regulator_history = json.load(response)
                self.assertEqual(regulator_history["count"], 1)
                self.assertTrue(regulator_history["append_only"])
                with urllib.request.urlopen(
                    f"{base}/api/regulator/replay?decision_id={decision_id}"
                ) as response:
                    replay = json.load(response)
                self.assertTrue(replay["replay_matches"])
                self.assertFalse(replay["actuation_authorized"])

                target = forecast["daily_forecast"][0]["target_date"]
                with urllib.request.urlopen(
                    f"{base}/api/history?target_date={target}"
                ) as response:
                    history = json.load(response)
                self.assertGreaterEqual(history["count"], 1)
                self.assertIn("comparison_baseline", history)
                state.history.append_actual(
                    "test_site", target, 123.4, "test_meter"
                )
                with urllib.request.urlopen(f"{base}/api/empirics") as response:
                    empirics = json.load(response)
                self.assertEqual(empirics["count"], 1)
                self.assertEqual(empirics["days"][0]["actual_kwh"], 123.4)

                import_payload = {
                    "site_id": "test_site",
                    "source": "solaredge_monitoring_api_backfill",
                    # Make this immutable observation explicitly newer than
                    # the one appended immediately above.
                    "observed_at": (
                        datetime.now(UTC) + timedelta(minutes=1)
                    ).isoformat().replace("+00:00", "Z"),
                    "daily": [
                        {
                            "target_date": target,
                            "actual_kwh_total": 125.6,
                            "details": {"provider": "SolarEdge"},
                        }
                    ],
                    "hourly": [
                        {
                            "target_time": f"{target}T10:00:00+00:00",
                            "actual_pv_kwh": 4.2,
                        }
                    ],
                }
                request = urllib.request.Request(
                    f"{base}/api/empirics/import",
                    data=json.dumps(import_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    imported = json.load(response)
                    self.assertEqual(response.status, 201)
                self.assertEqual(imported["daily_inserted"], 1)
                self.assertEqual(imported["hourly_inserted"], 1)
                with urllib.request.urlopen(f"{base}/api/empirics") as response:
                    updated = json.load(response)
                self.assertEqual(updated["days"][0]["actual_kwh"], 125.6)
                self.assertEqual(updated["exclusions"]["total"], 0)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
