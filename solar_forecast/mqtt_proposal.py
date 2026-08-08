"""Fail-closed MQTT proposal publication through Home Assistant Core."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
DEFAULT_TOPIC = "lsf/bb86/work_limit/proposal/"


class ProposalPublishError(ValueError):
    """Raised when a proposal cannot be constructed or published safely."""


def build_proposal(
    plan,
    *,
    topic=DEFAULT_TOPIC,
    valid_for_seconds=900,
    now=None,
    mode="dry_run",
    actuation_authorized=False,
    authorization_ref=None,
):
    """Create the narrow MQTT contract consumed by the BB86 local actuator.

    Generic callers remain dry-run by default. Active publication is accepted
    only for BB86 and only with an explicit, non-empty authorization reference.
    """
    if not isinstance(plan, dict):
        raise ProposalPublishError("plan must be an object")
    if not isinstance(topic, str) or not topic.endswith("/"):
        raise ProposalPublishError("proposal topic must end with '/'")
    if plan.get("site_id") != "bb86":
        raise ProposalPublishError("only the bb86 proposal contract is enabled")
    decision_id = plan.get("decision_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ProposalPublishError("plan is missing decision_id")
    proposed = plan.get("proposed_work_limit_kw")
    if not isinstance(proposed, (int, float)):
        raise ProposalPublishError("plan is missing proposed_work_limit_kw")
    if plan.get("mode") != "observe_only" or plan.get("actuation_authorized") is not False:
        raise ProposalPublishError("planner output must remain observation-only")
    if mode not in {"dry_run", "active"}:
        raise ProposalPublishError("proposal mode must be dry_run or active")
    if mode == "active":
        if actuation_authorized is not True:
            raise ProposalPublishError("active proposal requires actuation authorization")
        if not isinstance(authorization_ref, str) or not authorization_ref.strip():
            raise ProposalPublishError("active proposal requires authorization_ref")
    elif actuation_authorized is not False:
        raise ProposalPublishError("dry-run proposal cannot authorize actuation")
    issued = now or datetime.now(UTC)
    validity = int(valid_for_seconds)
    if validity < 60 or validity > 3600:
        raise ProposalPublishError("valid_for_seconds must be between 60 and 3600")
    payload = {
        "schema_version": "lsf-work-limit-proposal/1",
        "site_id": "bb86",
        "decision_id": decision_id.strip(),
        "proposed_work_limit_kw": float(proposed),
        "issued_at": _iso(issued),
        "valid_until": _iso(issued + timedelta(seconds=validity)),
        "reason": str(plan.get("reason") or "Ingen begrunnelse mottatt"),
        "mode": mode,
        "dry_run": mode == "dry_run",
        "actuation_authorized": mode == "active",
        "authorization_ref": (
            authorization_ref.strip() if mode == "active" else None
        ),
    }
    return {"topic": topic, "payload": payload}


def publish_via_home_assistant(
    proposal,
    supervisor_token,
    *,
    opener=urllib.request.urlopen,
    endpoint="http://supervisor/core/api/services/mqtt/publish",
):
    """Publish without storing broker credentials in LSF."""
    if not supervisor_token:
        raise ProposalPublishError("SUPERVISOR_TOKEN is unavailable")
    body = json.dumps(
        {
            "topic": proposal["topic"],
            "payload": json.dumps(proposal["payload"], separators=(",", ":")),
            "qos": 1,
            "retain": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(request, timeout=10) as response:
            if response.status < 200 or response.status >= 300:
                raise ProposalPublishError(
                    f"Home Assistant MQTT publish returned HTTP {response.status}"
                )
    except ProposalPublishError:
        raise
    except Exception as exc:
        raise ProposalPublishError(
            f"Home Assistant MQTT publish failed: {type(exc).__name__}"
        ) from exc
    return {
        "published": True,
        "topic": proposal["topic"],
        "qos": 1,
        "retain": False,
        "decision_id": proposal["payload"]["decision_id"],
    }


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
