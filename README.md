# Local Solar Forecast for Home Assistant

A local and site-independent replacement for the original `sun.php` service.
The Home Assistant app calculates hourly and daily PV forecasts from:

- latitude, longitude and timezone;
- each array's installed kWp, tilt and azimuth;
- hourly weather and cloud cover from MET Norway;
- configurable system losses and temperature correction.

Forecast issues are stored as immutable SQLite snapshots. The JSON API retains
hourly and daily values, including totals per panel direction and array.

## Installation

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open **Repositories** and add:
   `https://github.com/alfaomegaweb/local-solar-forecast`
3. Install **Local Solar Forecast**.
4. Copy `solar_forecast/site-template.yaml` to
   `/config/solar_forecast/site.yaml` and enter the site's geodata, PV arrays
   and production statistic entities.
5. Start the app and open its web interface.

## Repository layout

- `solar_forecast/` — installable Home Assistant app
- `solar_forecast/site-template.yaml` — generic PV layout template
- `node-red/lsf-work-limit-mqtt-dry-run-flow.json` — importable, non-actuating
  Node-RED receiver for version 0.5.4 work-limit proposals

Installation and API details are in
[`solar_forecast/DOCS.md`](solar_forecast/DOCS.md).
