# Local Solar Forecast

This Home Assistant app replaces a remote `sun.php`/LAMP service. It runs the
forecast locally, exposes JSON to Node-RED and stores every forecast issue as
an immutable SQLite snapshot.

## Site configuration

Create `/config/solar_forecast/site.yaml`. Start with `site-template.yaml`.
`example-site.yaml` is a runnable example with fictional values. The existing
solar-site generator can generate the same structure. Only the site file
changes between installations.

Required fields:

- `site.id`, `site.latitude`, `site.longitude`, `site.timezone`
- one or more `arrays` with capacity, tilt and azimuth
- a useful contact string in `weather.user_agent`

Create a separate array entry whenever direction, tilt, module type or
measurement source differs. The template contains copyable patterns for south,
east, west, north and vertical fields.

Azimuth is `0°` north, `90°` east, `180°` south and `270°` west. The engine
calculates solar elevation, solar azimuth and incidence for every array and
hour. Orientation factors such as south `0.90` and east/west `0.75` are
documentation/fallback values only and are not applied on top of the physical
calculation.

## API

Within Home Assistant's internal app network:

- `http://local-solar-forecast:8099/api/forecast`
- `http://local-solar-forecast:8099/api/history`
- `http://local-solar-forecast:8099/api/history?target_date=2026-07-27`
- `http://local-solar-forecast:8099/api/empirics`
- `http://local-solar-forecast:8099/health`

The direct host port is disabled by default. Enable port `8099` in the app's
Network settings only when a device outside Home Assistant must read the API.

The forecast response keeps the fields used by the existing energy matrix:

- `forecast_summary.forecast_issued_at`
- `forecast_summary.model_version`
- `daily_forecast[].expected_kwh`
- `daily_forecast[].expected_kwh_by_direction`
- `hourly_forecast[].estimated_power_kw`
- `hourly_forecast[].expected_kwh`

## Forecast history

The persistent database is `/data/forecast-history.sqlite` inside the app.
Every run has a unique hash and is inserted without updating previous rows.

Historical comparison uses this fixed rule:

1. latest snapshot at or before 18:00 local time the previous day;
2. fallback: latest snapshot before midnight starting the target day;
3. fallback: earliest available snapshot for the target day.

`/api/history?target_date=YYYY-MM-DD` returns the selected snapshot and the
selection rule. Actual energy is intentionally not invented by this app; the
energy matrix combines the selected forecast with Home Assistant Recorder
statistics.

## Empirical production

Add the daily production statistics under
`measurements.solar_energy.statistic_entities`. The app reads completed daily
changes from Home Assistant Recorder, stores each observation without
overwriting older observations, and compares it with the fixed forecast
baseline. The dashboard and `/api/empirics` expose:

`Date | Forecast | Actual | Deviation kWh | Deviation % | forecast_issued_at`

Legacy `sun.php` snapshots can be imported idempotently from
`/config/solar_forecast/legacy-forecast-snapshots.ndjson`.

## Safe migration

Keep the old `sun.php` running during comparison. Point Node-RED to the local
API with `SOLAR_FORECAST_URL`, leave `dry_run: true`, and compare several
complete days before enabling any battery actuator.
