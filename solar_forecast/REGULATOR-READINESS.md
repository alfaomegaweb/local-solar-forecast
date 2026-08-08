# LSF 0.6 regulator readiness

Status: development source only. Not deployed as common fleet version.

## Implemented and verified locally

- Pure read-only HA state normalizer with timestamps, units, live work-limit
  options, native Nord Pool intervals and a source-regime fingerprint.
- Fail-closed coverage for stale/unavailable states, entity replacement,
  quarter-hour prices, interval gaps and missing tomorrow data.
- Explicit Supervisor `GET /api/states` collection and dynamic SOC-KP derived
  from the first future forecast sunset; the endpoint remains disabled by each
  site's acceptance gate and has no service-call path.

- Pure deterministic planner in `regulator.py`; no Home Assistant client or
  service-call code exists in the module.
- Versioned output: `lsf-regulator-plan/1` and
  `0.6.0-observation-draft-1`.
- Stable SHA-256 input fingerprint and decision ID.
- Explicit timestamps for observation, forecast issue, interval start/end,
  checkpoint and latest safe import start.
- 72-hour hourly simulation using safe solar, base load, conversion loss,
  usable battery capacity and minimum SOC.
- Cloud/staleness uncertainty derating, reserve, export budget, checkpoint
  feasibility, proposed work limit and quantization to actual select options.
- Prefix-safe just-in-time import: every hourly prefix is checked against
  minimum SOC and available import power below the absolute grid limit.
- Fail-closed response when required input or timezone-bearing timestamps are
  missing.
- `POST /api/regulator/plan` uses the current forecast and caller-supplied,
  timestamped site snapshot and price intervals. Every response states
  `mode=observe_only` and `actuation_authorized=false`.
- Tests cover deterministic replay, sunny export budget, prolonged-cloud
  import, impossible import squeeze, missing timestamps and the HTTP endpoint.
- Every accepted plan preserves the exact site configuration, forecast,
  timestamped snapshot, prices and output in append-only SQLite storage.
  Identical input is idempotent; decision-ID collisions, update and delete are
  rejected. History and deterministic replay endpoints are implemented.
- Additive migration rehearsal recognizes `regulator_plans` as a new table
  without treating it as historical-row drift; pre-existing forecast and
  empirical table fingerprints remain unchanged and rollback matches source.
- Evidence refreshed on 2026-08-08: the complete suite, including the HTTP
  loopback/API test, passed: 44 tests run, one optional dependency test skipped.

## Required before 0.6.0 release

- Verify each site's complete entity map, battery inventory and BMS limits,
  then activate the already-wired Supervisor collector site by site.

1. Implement the dynamic solar-season classifier from geodata and preserved
   useful-PV history. Calendar months may remain advisory only.
2. Extend the planner from 72 hourly intervals to the contracted hierarchy:
   0–24 h native 15/60-minute dispatch, 24–72 h operational reserve, days 3–5
   reserve planning and days 6–10 risk-only outlook.
3. Add price-aware early import shifting. It must prove through chronological
   replay that moved energy is not lost against a full-SOC clamp before the
   later deficit; until then the draft deliberately imports just in time.
4. Derive load uncertainty from the preserved `very_stable`,
   `moderately_stable` or `low_stability` empirical model rather than a caller
   scalar alone.
5. Extend stored decisions with rejected candidate alternatives once the
   price-shifting optimiser is implemented. The accepted plan, inputs,
   calculated value, selected option and reason are already immutable and
   replayable.
6. Replay several complete HF39 and BB86 historical periods, including cloudy
   sequences, high load, full battery, entity-regime changes and 23/25-hour DST
   days. Compare projected and actual SOC/energy per interval.
7. Add battery-offline and forecast-gap integration tests. Stale SOC,
   stale/unavailable entities, missing tomorrow prices and price-interval gaps
   are already covered by the pure collector tests.
8. Keep generic installations observation-only by default. A separate local
   actuator contract and owner authorization are required per site. BB86's
   owner-approved active pilot does not authorize HF39 or future sites.
9. Rehearse package upgrade and rollback on a copied database, then deploy to
    BB86 first. Promote the common fleet version only after health, forecast,
    daily/hourly empirics and history counts remain correct.

## Current fleet truth

- Common source/package version remains `0.5.0`.
- BB86 is the approved active `work_limit` pilot, currently independent of the
  generic 0.6 planner actuator path.
- The 0.6 planner is not yet an actuator and cannot change `work_limit`.
- HF39 and other registered sites receive no regulator authority from this
  development work.
