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
- `POST http://local-solar-forecast:8099/api/empirics/import`
- `http://local-solar-forecast:8099/api/hourly-comparison?target_date=2026-07-27`
- `http://local-solar-forecast:8099/health`

The direct host port is disabled by default. Enable port `8099` in the app's
Network settings only when a device outside Home Assistant must read the API.
The empirical import endpoint is consequently available only through
authenticated ingress or Home Assistant's internal app network by default.

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
There is no automatic history pruning. Raw provider responses are stored with
a SHA-256 digest and non-secret request metadata.

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

Backfills from an authoritative meter API can be appended without rewriting
older observations:

```json
{
  "site_id": "example_site",
  "source": "meter_api_backfill",
  "observed_at": "2026-08-01T00:45:00Z",
  "daily": [
    {"target_date": "2026-07-31", "actual_kwh_total": 42.5}
  ],
  "hourly": [
    {"target_time": "2026-07-31T10:00:00+02:00", "actual_pv_kwh": 3.2}
  ]
}
```

The endpoint returns HTTP 201 and inserted/received counters. Repeating the
same payload is idempotent; a later corrected observation is appended with a
new `observed_at`, and the old value remains in SQLite for audit.

Legacy `sun.php` snapshots can be imported idempotently from
`/config/solar_forecast/legacy-forecast-snapshots.ndjson`.

For hourly verification, configure
`measurements.outdoor_temperature_statistic_entity` and the solar energy
statistics. The app reads hourly Recorder `change` values for PV and `mean`
for temperature. `/api/hourly-comparison` returns only daylight hours and
includes total/low/mid/high cloud cover, forecast and actual temperature,
irradiance, source, model run, issue time and lead hours.

## Archived backfill

Plan a local backfill without writing:

```sh
python3 tools/backfill_forecast_history.py \
  --site-config solar-site-hf39.yaml \
  --database /tmp/forecast-history.sqlite \
  --start-date 2026-07-17
```

Add `--apply` to fetch Open-Meteo Single Runs and append it to that local
database. MEPS THREDDS is attempted for Norwegian low/mid/high cloud layers;
`--without-meps` retains the Open-Meteo run without enrichment. The command
does not connect to Home Assistant or production MySQL.

## Automatic calibration

The physical calculation already applies the configured negative panel
temperature coefficient to estimated cell temperature. After at least 48
usable daylight observations, the add-on also evaluates a bounded local
temperature-residual calibration. A candidate is activated automatically only
when it improves training MAE by at least 2 percent. Every candidate is stored
as a versioned record; accepted factors are limited to 0.70–1.30 by default.
Original hourly values remain available as `uncalibrated_expected_kwh`.

These settings are site-configurable under `calibration`. This provides a
traceable hourly input for a later Node-RED `work_limit` manager; actuator
control is not part of the forecast add-on.

## Safe migration

Keep the old `sun.php` running during comparison. Point Node-RED to the local
API with `SOLAR_FORECAST_URL`, leave `dry_run: true`, and compare several
complete days before enabling any battery actuator.
