"""Read-only Home Assistant state normalisation for the LSF regulator.

This module deliberately has no HTTP client and no Home Assistant service-call
code.  A caller supplies the result of ``GET /api/states`` (or equivalent
fixtures); the collector validates and normalises that immutable observation.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
COLLECTOR_SCHEMA = "lsf-ha-observation/1"
PRICE_SCHEMA = "lsf-nord-pool-intervals/1"


class HACollectorError(ValueError):
    """The HA observation is not safe enough to feed the regulator."""


def next_soc_checkpoint(forecast, observed_at, minutes_before_sunset=60):
    """Select the first future SOC-KP from forecasted local sunsets."""
    observed = _instant(observed_at, "observed_at")
    try:
        offset = int(minutes_before_sunset)
    except (TypeError, ValueError) as exc:
        raise HACollectorError("minutes_before_sunset must be an integer") from exc
    if offset < 0 or offset > 360:
        raise HACollectorError("minutes_before_sunset must be between 0 and 360")
    candidates = []
    for row in forecast.get("daily_forecast") or []:
        sunset = row.get("sunset")
        if not sunset:
            continue
        checkpoint = _instant(sunset, "forecast sunset") - timedelta(minutes=offset)
        if checkpoint > observed:
            candidates.append(checkpoint)
    if not candidates:
        raise HACollectorError("forecast has no future sunset for SOC-KP")
    return _iso(min(candidates))


def collect_regulator_inputs(site_config, states, collected_at):
    """Return a regulator snapshot and native Nord Pool price intervals.

    ``states`` may be either the list returned by HA ``GET /api/states`` or a
    mapping keyed by entity_id.  No missing value is inferred from another
    entity.  Entity changes intentionally change ``source_fingerprint``.
    """
    observed = _instant(collected_at, "collected_at")
    collector = ((site_config.get("energy_regulator_vnext") or {}).get("collector") or {})
    if not collector.get("enabled", False):
        raise HACollectorError("energy_regulator_vnext.collector is not enabled")
    entities = collector.get("entities") or {}
    indexed = _index_states(states)
    maximum_age = int(collector.get("maximum_state_age_seconds", 900))
    if maximum_age <= 0:
        raise HACollectorError("maximum_state_age_seconds must be positive")

    evidence = []
    soc = _number_state(
        indexed, entities, "battery_soc", observed, maximum_age, evidence,
        allowed_units={"%"}, minimum=0, maximum=100,
    )
    work_limit, work_state = _number_state(
        indexed, entities, "work_limit", observed, maximum_age, evidence,
        allowed_units={"kW", ""}, return_state=True,
    )
    absolute_limit = _number_state(
        indexed, entities, "absolute_import_limit", observed, maximum_age,
        evidence, allowed_units={"kW", ""}, minimum=0.000001,
    )

    base_load_kw, load_source = _base_load(
        site_config, indexed, entities, observed, maximum_age, evidence
    )
    options = _numeric_options(work_state, "work_limit")
    if not any(abs(option - work_limit) < 1e-9 for option in options):
        raise HACollectorError("observed work_limit is not present in its HA options")

    optional = {}
    for role, output_name, units in (
        ("grid_power", "grid_power_kw", {"W", "kW"}),
        ("battery_charge_power", "battery_charge_power_kw", {"W", "kW"}),
        ("battery_discharge_power", "battery_discharge_power_kw", {"W", "kW"}),
    ):
        if entities.get(role):
            optional[output_name] = _number_state(
                indexed, entities, role, observed, maximum_age, evidence,
                allowed_units=units, convert_power_to_kw=True,
            )

    settings = collector.get("static") or {}
    usable_battery = _positive(settings.get("usable_battery_kwh"), "usable_battery_kwh")
    minimum_soc = _range(settings.get("minimum_soc_percent"), "minimum_soc_percent", 0, 100)
    checkpoint_at = settings.get("checkpoint_at")
    if checkpoint_at is None:
        raise HACollectorError("collector.static.checkpoint_at is required")
    checkpoint = _instant(checkpoint_at, "checkpoint_at")
    if checkpoint <= observed:
        raise HACollectorError("checkpoint_at must be later than collected_at")

    source_descriptor = {
        "schema": COLLECTOR_SCHEMA,
        "site_id": str((site_config.get("site") or {}).get("id") or ""),
        "roles": [
            {
                "role": item["role"],
                "entity_id": item["entity_id"],
                "native_unit": item["native_unit"],
            }
            for item in sorted(evidence, key=lambda row: row["role"])
        ],
        "sign_convention": collector.get(
            "sign_convention", "positive_import_negative_export"
        ),
    }
    source_fingerprint = _fingerprint(source_descriptor)
    maximum_export = settings.get("maximum_export_kw")
    if maximum_export is None:
        maximum_export = absolute_limit
    snapshot = {
        "schema": COLLECTOR_SCHEMA,
        "site_id": source_descriptor["site_id"],
        "observed_at": _iso(observed),
        "available_at": _iso(observed),
        "soc_percent": soc,
        "usable_battery_kwh": usable_battery,
        "minimum_soc_percent": minimum_soc,
        "target_checkpoint_soc_percent": _range(
            settings.get("target_checkpoint_soc_percent", 95),
            "target_checkpoint_soc_percent", minimum_soc, 100,
        ),
        "checkpoint_at": _iso(checkpoint),
        "base_load_kw": base_load_kw,
        "base_load_source": load_source,
        "absolute_import_limit_kw": absolute_limit,
        "observed_work_limit_kw": work_limit,
        "work_limit_options": options,
        "grid_margin_kw": _nonnegative(settings.get("grid_margin_kw", 0.5), "grid_margin_kw"),
        "minimum_active_export_kw": _positive(
            settings.get("minimum_active_export_kw", 0.25),
            "minimum_active_export_kw",
        ),
        "maximum_export_kw": _positive(maximum_export, "maximum_export_kw"),
        "source_fingerprint": source_fingerprint,
        "source_regime": source_descriptor,
        "source_observations": evidence,
        "data_quality": {
            "status": "valid",
            "maximum_state_age_seconds": maximum_age,
            "all_required_entities_available": True,
            "actuation_authorized": False,
        },
        **optional,
    }
    price_config = collector.get("nord_pool") or {}
    price_maximum_age = int(
        price_config.get("maximum_state_age_seconds", 108000)
    )
    prices, price_quality = _collect_prices(
        indexed, price_config, observed, price_maximum_age
    )
    snapshot["data_quality"]["nord_pool"] = price_quality
    snapshot["data_quality"]["safe_for_aggressive_export"] = bool(
        price_quality["complete_configured_sources"]
    )
    return {"snapshot": snapshot, "prices": prices, "price_quality": price_quality}


def _index_states(states):
    if isinstance(states, dict):
        iterable = states.values()
    elif isinstance(states, list):
        iterable = states
    else:
        raise HACollectorError("states must be a HA state list or mapping")
    indexed = {}
    for state in iterable:
        if not isinstance(state, dict) or not state.get("entity_id"):
            continue
        indexed[str(state["entity_id"])] = state
    return indexed


def _number_state(indexed, entities, role, now, maximum_age, evidence,
                  allowed_units, minimum=None, maximum=None,
                  convert_power_to_kw=False, return_state=False):
    entity_id = entities.get(role)
    if not entity_id:
        raise HACollectorError(f"collector entity role {role!r} is not configured")
    state = indexed.get(str(entity_id))
    if state is None:
        raise HACollectorError(f"required HA entity for {role} is missing")
    raw = str(state.get("state", "")).strip()
    if raw.lower() in {"", "unknown", "unavailable", "none", "nan", "inf", "-inf"}:
        raise HACollectorError(f"required HA entity for {role} is unavailable")
    timestamp = _instant(state.get("last_updated") or state.get("last_changed"), f"{role}.last_updated")
    age = (now - timestamp).total_seconds()
    if age < -5:
        raise HACollectorError(f"required HA entity for {role} has a future timestamp")
    if age > maximum_age:
        raise HACollectorError(f"required HA entity for {role} is stale ({int(age)} seconds)")
    attributes = state.get("attributes") or {}
    unit = str(attributes.get("unit_of_measurement") or "")
    if unit not in allowed_units:
        raise HACollectorError(f"unexpected unit {unit!r} for {role}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise HACollectorError(f"non-numeric HA state for {role}") from exc
    if not math.isfinite(value):
        raise HACollectorError(f"non-finite HA state for {role}")
    normalized_unit = unit
    if convert_power_to_kw:
        if unit == "W":
            value /= 1000
        normalized_unit = "kW"
    if minimum is not None and value < minimum:
        raise HACollectorError(f"HA state for {role} is below minimum")
    if maximum is not None and value > maximum:
        raise HACollectorError(f"HA state for {role} is above maximum")
    evidence.append({
        "role": role,
        "entity_id": str(entity_id),
        "state": value,
        "native_unit": unit,
        "normalized_unit": normalized_unit,
        "observed_at": _iso(timestamp),
    })
    return (value, state) if return_state else value


def _base_load(site_config, indexed, entities, now, maximum_age, evidence):
    if entities.get("base_load_power"):
        value = _number_state(
            indexed, entities, "base_load_power", now, maximum_age, evidence,
            allowed_units={"W", "kW"}, minimum=0, convert_power_to_kw=True,
        )
        return value, "home_assistant_entity"
    fallback = (((site_config.get("load_model") or {}).get("daily_energy") or {}).get("fallback_kwh_per_day"))
    daily = _positive(fallback, "load_model.daily_energy.fallback_kwh_per_day")
    return daily / 24, "site_profile_daily_fallback"


def _numeric_options(state, role):
    values = (state.get("attributes") or {}).get("options")
    if not isinstance(values, list) or not values:
        raise HACollectorError(f"HA input_select options are missing for {role}")
    result = []
    for raw in values:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise HACollectorError(f"non-numeric HA input_select option for {role}") from exc
        if not math.isfinite(value):
            raise HACollectorError(f"non-finite HA input_select option for {role}")
        result.append(value)
    return sorted(set(result))


def _collect_prices(indexed, config, now, maximum_age):
    if not config.get("enabled", False):
        return [], {
            "status": "disabled",
            "complete_configured_sources": False,
            "missing_entities": [],
            "interval_count": 0,
        }
    entity_ids = config.get("entities") or []
    attribute = config.get("intervals_attribute", "intervals")
    if not entity_ids:
        raise HACollectorError("Nord Pool collector has no entities")
    rows = []
    sources = []
    missing_entities = []
    for entity_id in entity_ids:
        state = indexed.get(str(entity_id))
        if state is None:
            missing_entities.append(str(entity_id))
            continue
        updated = _instant(state.get("last_updated") or state.get("last_changed"), "Nord Pool last_updated")
        age = (now - updated).total_seconds()
        if age < -5 or age > maximum_age:
            raise HACollectorError("Nord Pool interval entity is stale or future-dated")
        raw_rows = (state.get("attributes") or {}).get(attribute)
        if isinstance(raw_rows, str):
            try:
                raw_rows = json.loads(raw_rows)
            except json.JSONDecodeError as exc:
                raise HACollectorError("Nord Pool intervals attribute is invalid JSON") from exc
        if not isinstance(raw_rows, list) or not raw_rows:
            raise HACollectorError("Nord Pool intervals attribute is empty")
        sources.append({"entity_id": str(entity_id), "available_at": _iso(updated)})
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise HACollectorError("Nord Pool interval must be an object")
            start = _instant(raw.get("start"), "Nord Pool interval start")
            end = _instant(raw.get("end"), "Nord Pool interval end")
            if end <= start:
                raise HACollectorError("Nord Pool interval end must follow start")
            minutes = (end - start).total_seconds() / 60
            if minutes not in {15.0, 60.0}:
                raise HACollectorError("Nord Pool interval must be 15 or 60 minutes")
            price = raw.get("price_nok_per_kwh", raw.get("price"))
            price = _finite(price, "Nord Pool price")
            rows.append({
                "schema": PRICE_SCHEMA,
                "start": _iso(start),
                "end": _iso(end),
                "interval_minutes": int(minutes),
                "price_nok_per_kwh": price,
                "available_at": _iso(updated),
                "source_entity": str(entity_id),
            })
    if not rows:
        raise HACollectorError("no Nord Pool intervals are available")
    rows.sort(key=lambda row: row["start"])
    seen = set()
    previous_end = None
    for row in rows:
        key = (row["start"], row["end"])
        if key in seen:
            raise HACollectorError("duplicate Nord Pool interval")
        if previous_end is not None and row["start"] != previous_end:
            raise HACollectorError("Nord Pool intervals contain a gap or overlap")
        seen.add(key)
        previous_end = row["end"]
    fingerprint = _fingerprint({
        "schema": PRICE_SCHEMA,
        "configured_entities": [str(item) for item in entity_ids],
        "sources": sources,
        "missing_entities": missing_entities,
    })
    for row in rows:
        row["source_fingerprint"] = fingerprint
    return rows, {
        "status": "valid" if not missing_entities else "incomplete",
        "complete_configured_sources": not missing_entities,
        "missing_entities": missing_entities,
        "interval_count": len(rows),
        "source_fingerprint": fingerprint,
        "safe_for_aggressive_export": not missing_entities,
    }


def _instant(value, label):
    if not value:
        raise HACollectorError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HACollectorError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HACollectorError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _finite(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HACollectorError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise HACollectorError(f"{label} must be finite")
    return result


def _positive(value, label):
    result = _finite(value, label)
    if result <= 0:
        raise HACollectorError(f"{label} must be positive")
    return result


def _nonnegative(value, label):
    result = _finite(value, label)
    if result < 0:
        raise HACollectorError(f"{label} must be non-negative")
    return result


def _range(value, label, minimum, maximum):
    result = _finite(value, label)
    if not minimum <= result <= maximum:
        raise HACollectorError(f"{label} must be between {minimum} and {maximum}")
    return result


def _fingerprint(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
