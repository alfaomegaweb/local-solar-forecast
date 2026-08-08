# Changelog

## 0.5.1

- Package the regulator and Home Assistant collector modules required by the
  application, preventing an import failure in built Home Assistant images.
- Enable the already fail-closed, reversible historical quality gate for HF39.
  Invalid zero observations remain preserved but are excluded from comparison
  and calibration; no production row is overwritten or deleted.
- Keep regulator collection and actuation disabled. This release changes data
  quality and packaging only.

## 0.6.0 draft

- Add the contract and generic site-template placeholder for evidence-based
  installation discovery and safe rediscovery after hardware, integration,
  entity, unit or source-regime changes. Discovery remains non-actuating and
  requires confirmation before a control mapping can change.
- Add a historical-reconstruction placeholder that preserves all forecasts,
  rebuilds empirical observations with source/interval provenance and records
  unresolved periods as gaps instead of false zero production.

- Add a generic, read-only Home Assistant state collector with strict
  timestamp/unit validation, live input-select options and fail-closed handling
  of stale or unavailable required entities.
- Add stable source-regime fingerprints so entity replacements cannot be
  mistaken for continuity of the same measurement series.
- Preserve native Nord Pool 15/60-minute boundaries, report missing tomorrow
  data explicitly and duration-weight quarter-hour prices in the hourly plan.
- Add disabled BB86 and HF39 collector mappings; no actuation is enabled.
- Add an explicitly triggered, read-only Supervisor collection endpoint and
  derive the next SOC-KP from the first future forecast sunset.

- Add a deterministic, non-actuating 72-hour regulator planner with explicit
  multi-day reserve, uncertainty derating, export budget, SOC feasibility,
  prefix-safe just-in-time import scheduling and latest-safe import start.
- Add `POST /api/regulator/plan`. It consumes an explicitly timestamped site
  snapshot and Nord Pool intervals, uses the current immutable forecast and
  always returns `mode: observe_only` and `actuation_authorized: false`.
- Fingerprint every planner input and produce a stable `decision_id` so plans
  can be reproduced and audited.
- Append every accepted decision and its exact input bundle to immutable
  `regulator_plans` storage. Add idempotent inserts, collision rejection,
  append-only SQLite triggers, history listing and deterministic replay API.
- Extend copied-database migration rehearsal so the new regulator table is
  reported as an additive schema change while all pre-existing historical
  table fingerprints and rollback data must remain unchanged.

## 0.5.0

- Add an observation-only `energy_regulator_vnext` placeholder contract for
  multi-day reserve planning, dynamic solar season, export budgets,
  latest-safe import scheduling, SOC feasibility, stability and status data.
  It explicitly grants no actuator authorization.
- Rename the dashboard column `Horizon h` to
  `Hours ahead / Timer frem` so forecast lead time cannot be confused with
  the solar horizon.
- Add an on-page language selector for Norwegian, English, Portuguese,
  Spanish, Ukrainian and German. The browser remembers the selection; JSON
  API keys and machine contracts are unchanged.

## 0.4.2

- Add an explicit per-site `auto_quarantine_existing` gate. When enabled, it
  adds reversible exclusion markers for preserved daily rows below the minimum
  or above the physical specific-yield limit. Raw observations remain
  immutable and rerunning the gate is idempotent.

## 0.4.1

- Fix successful hourly Recorder ingestion after measurement-source
  fingerprinting was introduced. The production acceptance test now exercises
  the full response-to-append path rather than only request construction.

## 0.4.0

- Reject incomplete, negative and near-zero Recorder observations before they
  can become empirical or calibration input.
- Require all configured PV energy entities for completed daily observations.
- Add a common HF39/BB86 fleet registry, canonical site profiles and explicit
  deployment/learning acceptance gates.
- Add reversible observation quarantine: suspect empirical rows remain in the
  append-only database but are excluded from comparisons and calibration.
- Add a source-preserving migration/rollback rehearsal tool with table
  fingerprints, SQLite integrity checks and idempotent legacy-import checks.
- Upgrade autonomous calibration to `bounded-temperature-residual-2`: training
  requires trusted completed days, quality-valid daylight hours per day and an
  explicit rolling window. Current or untrusted days cannot activate learning.
- Add fail-closed fleet/profile validation for PV capacity, panel counts,
  duplicate empirical entities, data-quality gates, safe calibration settings,
  version alignment, access/backup metadata and accidental embedded secrets.
- Calibration is disabled unless a site profile explicitly enables it.
- Enforce append-only history at the SQLite layer: forecast runs, daily/hourly
  forecasts, raw weather payloads, daily/hourly observations and calibration
  records reject both `UPDATE` and `DELETE`.
- Reject daily empirical totals above a configurable physical specific-yield
  bound; the generic audit/quarantine tool can apply the same capacity-based
  rule to preserved legacy data.
- Normalize Recorder statistics by device class (`energy: kWh`,
  `temperature: °C`) before aggregation, so mixed native kWh/MWh entities
  cannot silently create scale errors.
- Mark empirical health successful only when the latest completed local day is
  present and complete; older valid history alone can no longer create a false
  green status.
- Attach a stable SHA-256 measurement-source fingerprint to every Recorder
  daily/hourly observation. Entity swaps, aggregation rules and normalized
  units are therefore traceable without rewriting historical rows.
- Require both daily and hourly calibration rows to match the currently
  configured measurement-source fingerprint. Preserved observations from an
  earlier entity regime can no longer be mixed into a new learned model.

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
