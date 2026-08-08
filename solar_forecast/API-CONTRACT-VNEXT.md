# LSF API contract vNext — empirical observations

## Site discovery contract placeholder

The next implementation phase exposes an append-only discovery run with:

- `discovery_run_id`, `started_at`, `completed_at` and schema version;
- candidate entity/source mappings with confidence and supporting evidence;
- unit, device-class, freshness, monotonicity and energy-balance results;
- unresolved required inputs and the resulting fail-closed capability gates;
- the previous and proposed measurement-source fingerprints;
- explicit approval identity/time before any control mapping changes.

A rerun never overwrites an earlier run or historical observation. Hardware,
integration, entity or unit changes create a new source regime after approval.
Discovery and rediscovery are non-actuating.

## Historical reconstruction contract placeholder

`lsf-history-reconstruction/1` preserves every existing forecast run, daily
and hourly forecast and raw provider payload. It appends reconstructed empirical
observations from source regimes in an explicit site-defined priority order.
Every accepted observation carries its source fingerprint, observation and
availability times, confidence and—at hourly resolution—authoritative interval
boundaries.

Missing periods are emitted as gaps, never fabricated as zero production.
Conflicting candidates are retained in reconstruction evidence with rejection
reasons. Invalid legacy observations are preserved and reversibly excluded.
The result reports daily/hourly coverage and whether each target period is safe
for forecast-versus-actual comparison or model training.

Status: owner decision, not yet implemented. This document does not alter the
0.4.2 runtime contract.

## Energy-regulator planning placeholder

The next planning contract will remain observation-only until separately
authorized per site. It must not imply permission to call a Home Assistant
service or change `work_limit`.

LSF will expose a rolling plan with four deliberately different authority
levels: detailed dispatch for 0–24 hours, operational reserve for 24–72 hours,
daily reserve planning through day 5, and a risk-only outlook through day 10.
Long-range weather may increase reserve or block aggressive export, but must
not directly select an actuator value.

The planner will simulate at least: no voluntary export, minimum unavoidable
export, price-weighted export, and early price-aware import. Every candidate is
evaluated using preserved hourly PV, base-load, price, battery and conversion-
loss inputs. Plans that breach minimum SOC, the absolute grid-import limit, or
cannot receive the required energy before the first deficit are rejected.

The reserve consists of base load until the next useful PV interval, forecast
deficit through the next sufficient solar period, unavoidable export, battery
loss and an explicit uncertainty margin. "Useful PV" means the first forecast
interval where PV covers expected base load; it is not merely astronomical
sunrise. In solar season, normal base load is not a curtailment target.

The 95-percent SOC checkpoint is a soft optimization target with explicit
`achievable`, `at_risk`, or `infeasible_without_import_or_flexible_load`
status. Hard grid and battery constraints take precedence. When future cloud
makes the target or minimum reserve infeasible, LSF calculates the energy
shortfall and latest safe import start. Required energy is placed in the
cheapest feasible intervals first, then earlier intervals as necessary so a
late high-power import cannot breach the absolute grid limit.

The status object and hourly-plan fields are enumerated under
`energy_regulator_vnext.status_contract` in `site-template.yaml`. Proposed
`work_limit` is quantized to an option actually present in the configured
Home Assistant `input_select`. Hysteresis, a deadband and step-rate limiting
are mandatory; safety-limit risks may trigger an immediate replan.

## Ownership

Local Solar Forecast owns forecast snapshots, empirical PV observations,
measurement-source regimes, quality/exclusion state and forecast-versus-actual
comparison semantics. EnergyPilot consumes these records and must not infer
missing interval or provenance fields from display-oriented values.

## Hourly observations

The next natural API revision will expose an explicit observation object for
each actual value in `/api/hourly-comparison`:

- `observation_id`: immutable LSF observation identifier;
- `observation_type`: `pv_energy_interval`;
- `interval_start`: inclusive UTC timestamp from the Recorder period start;
- `interval_end`: exclusive UTC timestamp;
- `observed_at`: time LSF appended the immutable observation;
- `available_at`: earliest time the observation became available to a
  downstream consumer; initially equal to `observed_at`;
- `source`: transport/provider, for example
  `home_assistant_recorder_hourly_statistics`;
- `measurement_source_fingerprint`: stable SHA-256 over sorted entity IDs,
  aggregation and normalized unit;
- `measurement_regime_id`: explicit regime identifier. Until separately
  versioned, this is identical to `measurement_source_fingerprint`;
- `quality_valid`, `excluded` and exclusion metadata;
- `actual_pv_kwh` and optional `actual_temperature_c`.

`target_time` remains a compatibility alias for `interval_start`. Consumers
must use the explicit half-open interval `[interval_start, interval_end)` once
the vNext fields are available. For Recorder hourly statistics, the interval
is one elapsed hour in UTC, including across local daylight-saving changes.

## Daily observations

Daily PV empirics will be a separate append-only domain type,
`pv_energy_daily_observation`. They will not be forced into EnergyPilot's
generic hourly interval table.

Each daily observation will expose:

- immutable `observation_id` and `target_date`;
- `interval_start` and `interval_end` as the local civil-day boundaries,
  converted to UTC (therefore 23, 24 or 25 elapsed hours around DST);
- `observed_at` and `available_at`;
- source, measurement fingerprint/regime and normalized unit `kWh`;
- quality and reversible exclusion state;
- `actual_kwh_total`.

Multiple immutable observations for the same `target_date` may exist. LSF owns
the authoritative selection rule and will expose the selected observation
without deleting superseded raw observations.

## Compatibility and rollout

- Existing 0.4.2 endpoints remain readable while vNext fields are added.
- New fields must first be covered by storage/API tests and a copied-database
  migration rehearsal.
- EnergyPilot may map forecast 0.4.2 now, but should defer empirical ingestion
  until these provenance and interval fields are present in a deployed LSF
  contract version.
- No production or battery-control authorization follows from this document.
