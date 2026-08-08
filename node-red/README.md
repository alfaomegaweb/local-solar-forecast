# LSF Node-RED dry-run

Import `lsf-work-limit-mqtt-dry-run-flow.json`. It creates one Node-RED tab,
`Ekstern maskinhelse og LSF`, with three independent lines: machine-health
reporting, work-limit dry-run and an optional Deye/ESPHome setpoint-plan
dry-run. Select the site's existing MQTT broker in the MQTT input node. The
BB86 pilot listens on:

`alfaomega/lsf/bb86/work_limit/proposal`

The flow validates `site_id`, `decision_id`, `issued_at`/`decided_at`,
`valid_until` and `proposed_work_limit_kw`. It rounds the proposal to a known
`input_select.deye_work_limit` option and displays the result in Debug.

Version 0.5.4 is strictly dry-run: there is no Home Assistant Action node and
the flow cannot write to `input_select.deye_work_limit`. MQTT credentials and
broker configuration are deliberately not included in the repository.

An optional proposal may select `actuator_profile: deye_esphome_http_v1` and
provide `setpoints` for `timezone1_soc`, `timezone1_voltage`,
`timezone1_power` and `battery_max_charge`. Version 0.5.4 validates these and
shows the ordered request plan only. It contains no HTTP request nodes. A later
active adapter must use a local `DEYE_ESPHOME_BASE_URL`, local Digest
credentials, read-back verification and rollback.

Set `HF39_HEALTH_WEBHOOK_URL` in the local Node-RED environment before enabling
the health line. The webhook secret is deliberately absent from the flow.
