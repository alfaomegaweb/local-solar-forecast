# LSF service operations contract

Status: accepted product requirement for the next implementation phase. This
contract does not enable remote access, telemetry, billing or actuation in the
current runtime.

The customer-facing terms are maintained in
`../AVTALE-LSF-SERVICE-NO.md`. Runtime behaviour must not claim that a service
is active unless the requirements below have been verified.

## Roles and ownership

- The customer owns the Home Assistant installation and the site's data.
- Alfa Omega is the technical facilitator only after explicit customer
  acceptance.
- LSF owns local forecast, empirical and health semantics.
- Maplite is an Alfa Omega-owned service endpoint loaned to the customer while
  the service agreement is active.
- The local actuator remains independently authorized. A service agreement
  does not by itself authorize changes to `work_limit`.

## Installation tracks

The same LSF package supports two onboarding tracks:

1. `existing_ha`: preserve the customer's configuration, back up before any
   change, discover read-only candidates and require confirmation before a
   source or control mapping changes.
2. `alfa_prepared_ha`: start from the versioned Alfa Omega base package, then
   discover and confirm site-specific hardware, geodata and entities.

Both tracks require a supported 64-bit architecture (`amd64` or `aarch64`) and
SSD, eMMC or equivalent robust permanent storage. A conventional SD/microSD
card is not accepted as the permanent system disk.

## Remote-ready gate

Before Alfa Omega leaves the installation site, an immutable onboarding result
must record pass/fail evidence for:

- Home Assistant local availability and stable LAN connectivity;
- HACS authorization bound to GitHub user `alfaomegaweb` and service mailbox
  `hacs@alfaomegaweb.no`, with no OAuth credential copied into evidence;
- verified geodata and timezone;
- suggested and customer-confirmed Nord Pool bidding zone;
- inverter, battery, PV, grid and load visibility where applicable;
- a verified AMS/HAN source; Aidon with Tibber Pulse is the standard Norwegian
  package, while an existing compatible AMS bridge may be retained after the
  same data-quality and transport checks;
- fresh import, export and instantaneous power measurements from the AMS path;
- local MQTT availability and site-scoped credentials/ACL;
- a verified, site-scoped MQTT mapping from the approved AMS entities to LSF
  and Node-RED, without storing Wi-Fi or MQTT secrets in the onboarding result;
- Maplite powered, connected to LAN and bound to the correct site ID;
- the mandatory Maplite `iot_ap` base profile present and tested; when enabled,
  a unique site SSID, isolated subnet, DHCP/NAT and least-privilege HA/MQTT
  access are verified;
- explicitly accepted web access and WireGuard/SSH access;
- successful external-path web and SSH tests outside the customer LAN;
- current backup identifier and a documented rollback route;
- `observe_only` as the initial regulator state.

No remote-ready result may infer success from configuration alone. Failed or
unresolved checks keep the relevant capability closed.

HACS is an optional/custom-integration manager authenticated to the dedicated
Alfa Omega GitHub identity. It is not the distribution channel for the LSF
Home Assistant app. LSF is installed from
`https://github.com/alfaomegaweb/local-solar-forecast` in the Home Assistant
app store. These two checks remain distinct in onboarding evidence.

Tibber Pulse Wi-Fi commissioning is normally completed at the customer site so
that SSID, credentials, radio coverage, HAN activation and live measurements
can be verified together. It may be pre-staged by Alfa Omega only when the
customer has provided the required network details through an approved secret
channel. The credentials are never written to the service repository or
onboarding evidence.

## Consent record

Consent is versioned and auditable. It records at least:

- `consent_id`, site ID, agreement version and acceptance timestamp;
- customer identity/reference without exposing it in public health data;
- accepted health telemetry fields;
- accepted Maplite placement on power and LAN;
- accepted web and WireGuard/SSH scopes;
- service term (`included_trial`, `monthly`, `annual` or `none`);
- whether active energy control is separately authorized;
- withdrawal timestamp when applicable.

Telemetry and remote access fail closed when the necessary consent is missing
or withdrawn. Consent to monitoring is not consent to actuation.

## Service lifecycle

The machine-readable service state is one of:

- `included_trial`: the approved installation-month remainder plus three
  complete calendar months;
- `active_monthly`: active monthly service, priced from the separately recorded
  connection profile;
- `active_annual`: active annual service, priced from the separately recorded
  connection profile;
- `suspended_connectivity`: Maplite or required connectivity is unavailable;
- `terminated_pending_maplite_return`: agreement ended; return is outstanding;
- `closed`: monitoring and remote service ended and return settled.

Before the included trial ends, the customer must explicitly choose monthly,
annual or no continuation. No paid continuation may be inferred from silence.
Billing belongs to the commercial system, not to LSF or Home Assistant.

The connection profile is one of:

- `customer_mikrotik`: NOK 49 including VAT per month or NOK 490 per year;
- `maplite`: NOK 99 including VAT per month or NOK 990 per year;
- `ha_pipe`: NOK 149 including VAT per month or NOK 1,490 per year after the
  mandatory test period.

Exactly one connection profile is selected for an active service. A customer
MikroTik qualifies only after verified RouterOS support, a site-bound
WireGuard peer, least-privilege route/ACL rules, configuration backup, rollback
and an external-path connectivity test. Customer acceptance is recorded before
activation. Removal, reset, replacement or security-gate failure suspends the
remote service until a qualifying connection is restored.

When Maplite is not deployed, an explicitly accepted customer MikroTik or HA
pipe may be registered. The record must identify its
technical type, access scope, owner, activation time and revocation procedure.
LSF does not infer acceptance from the pipe merely being reachable.

Exactly one qualifying connection is mandatory before rollout. Alfa Omega must
retain accepted access for the remainder of the approved installation month
and the next three complete calendar months. MikroTik and Maplite service are
included during those three complete test months. An HA pipe costs NOK 50
including VAT for each complete test month (NOK 150 total); the partial
installation month is not charged. Revocation or loss of required access keeps
rollout verification incomplete and suspends monitoring/service.

## Maplite reachability and separate application health

Maplite is a minimal outbound service bridge and does not publish MQTT health
or run LSF/HA application logic. Alfa Omega measures the bridge centrally and
independently through:

- WireGuard latest-handshake age;
- site-bound node reachability/ping; and
- optional restricted SSH diagnostics from an explicitly permitted hub source.

LSF and Home Assistant health are separate signals collected through the
established service path. They must not be folded into or inferred from
Maplite reachability. This separation distinguishes at least:

- `bridge_unreachable`;
- `bridge_reachable_ha_unreachable`;
- `ha_reachable_lsf_unhealthy`; and
- `bridge_and_application_healthy`.

The central monitor records a pseudonymous site binding, service-node identity,
observation time, handshake age, reachability result and separately timestamped
HA/LSF health. It stores no WireGuard private key, password or other secret in
health records or logs.

Loss of bridge reachability suspends remote monitoring but must not stop local
Home Assistant, LSF or existing local energy operation. MQTT remains part of
the site energy/telemetry package where needed, but is not a prerequisite for
proving that the Maplite bridge itself is alive.

The service peer uses least-privilege `AllowedIPs`: only the hub and explicit
service aliases/routes required for the approved site. A broad service subnet
such as `/24` is rejected by the activation gate unless a documented routing
need, ACL evidence and owner approval justify the exception.

## Customer notification policy

For one unresolved incident, send SMS and/or e-mail to the customer's verified
contact channel:

1. no more than one notification per rolling 24 hours for the first three
   notification days;
2. after that, one reminder each Saturday while the same incident remains
   unresolved.

Recovery closes the incident and stops reminders. A new incident receives a
new incident ID and starts a new schedule. Duplicate signals are coalesced.
Every attempt records channel, time, outcome and provider message reference,
but never message credentials. Notification delivery failure is visible to
Alfa Omega and does not count as customer acknowledgement.

## Degraded and disconnected behaviour

- Short power, Internet or LAN interruptions do not terminate the agreement.
- While Maplite is unavailable, remote monitoring and associated response time
  are suspended and the customer is notified by the policy above.
- Persistent absence after reasonable notice may move the agreement to
  `terminated_pending_maplite_return`; this is a commercial/admin decision,
  not an autonomous LSF action.
- Local operation remains independent of the fleet service.
- LSF must never fabricate healthy status while health evidence is stale.

## Termination and return

On termination, remote credentials are revoked and Maplite plus all supplied
cables are due for return within 30 days. The customer pays return postage,
uses tracked shipping and sends the tracking code to Alfa Omega. The service
system tracks requested, reminded, tracking-received, returned and settled
states. A NOK 500 including VAT non-return/loss charge may only be raised by
the commercial system after the contractually required written reminder. LSF
does not create invoices.

Alfa Omega may administratively end the service when responsible delivery is
no longer possible, including company cessation, permanent incapacity, death
or another serious unforeseen event. Where possible, the customer receives 30
days' notice, export/backup information and a remote-access shutdown path. An
emergency event may make advance notice impossible. Local HA/LSF operation is
not intentionally disabled by service termination. Business-continuity,
customer notification, credential revocation and any proportional refund are
commercial/legal responsibilities outside the LSF runtime.

## Updates

- New LSF versions are customer-optional.
- Default update policy is `notify_only`.
- An update states version, impact, migration need and rollback availability.
- No development build or control-authority change is installed by an
  automatic health-monitoring subscription.
- A working installed version continues locally when the customer declines an
  update, subject to separately communicated critical compatibility or
  security limitations.

## Privacy and data minimisation

Fleet health is separate from detailed energy history. The default outbound
payload contains health and freshness only. Detailed consumption, exact
location and raw entity names require a separately documented purpose and
authority. Secrets are never sent in telemetry or written to logs.

Retention, access, sub-processors, data-subject handling and deletion/return at
service end must be described in the privacy notice and, where applicable, a
data-processing agreement before production monitoring starts.

## Acceptance gates before production use

- unit and integration tests for lifecycle transitions and notification
  scheduling;
- replay-safe, site-isolated MQTT tests;
- consent withdrawal and credential revocation test;
- Maplite disconnect/reconnect and local-operation continuity test;
- external remote-ready test with backup and rollback evidence;
- SMS/e-mail sandbox test, deduplication and failed-delivery test;
- privacy review and completed customer-facing documents;
- copied-database/package upgrade rehearsal;
- pilot approval before fleet-wide promotion.
