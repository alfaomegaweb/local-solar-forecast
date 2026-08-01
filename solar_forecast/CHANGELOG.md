# Changelog

## 0.3.0

- Normalize immutable hourly forecasts with issue, target and lead time.
- Preserve raw MET/Open-Meteo/MEPS inputs with SHA-256 digests.
- Add Open-Meteo Single Runs backfill and optional MEPS cloud-layer adapter.
- Capture hourly HA Recorder PV/temperature observations.
- Add daylight-only hourly comparison API and dashboard table.
- Add bounded, versioned automatic temperature-residual calibration.
- Remove forecast-history pruning.

## 0.2.0

- Import immutable legacy `sun.php` forecast snapshots.
- Read completed daily production statistics from Home Assistant Recorder.
- Compare forecast and actual production with a fixed 18:00 baseline.
- Add empirical API, dashboard table, MAE and bias.

## 0.1.0

- First generic Home Assistant app.
- MET Norway hourly weather input.
- Solar position and plane-of-array calculation per configured array.
- Immutable SQLite forecast snapshots.
- HTTP API compatible with the existing energy matrix and Node-RED collector.
