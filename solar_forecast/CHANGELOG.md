# Changelog

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
