import copy
import json
import os
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
)
from history_store import ForecastHistory  # noqa: E402
from backfill import (  # noqa: E402
    MepsThreddsClient,
    comparison_cutoff,
    open_meteo_to_engine_payload,
    select_model_run,
)
from calibration import apply_calibration  # noqa: E402
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


class HistoryTests(unittest.TestCase):
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


class ApiTests(unittest.TestCase):
    def test_http_forecast_and_history_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "site.json"
            config_path.write_text(json.dumps(sample_config()), encoding="utf-8")
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
                with urllib.request.urlopen(f"{base}/api/forecast") as response:
                    forecast = json.load(response)
                self.assertEqual(forecast["site"]["id"], "test_site")
                self.assertTrue(forecast["snapshot_storage"]["append_only"])

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
                    "observed_at": "2026-08-01T00:45:00Z",
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
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
