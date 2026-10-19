"""Recovery plans for runs whose last execution ended with an unknown result.

A plan is what the user approves before anything runs again: the compose that will be
validated, whether images are rebuilt, and the risks nobody can rule out. It is built by a
pure function from records (the interrupted thread's last checkpoint, the last approval, the
last observation and Runner check, the dispatch log, the cluster's marks), so building or
showing it never touches the cluster. Its facts say what it relies on: a later check that
changes them invalidates the plan.

A recovery runs on a new LangGraph thread whose state is built from a whitelist, never copied:
the old thread's pending node, approval, unchecked patch, report, and deadline must not carry
over into a run the user approved separately.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import yaml

from cagent.generate import to_yaml
from cagent.patches import fingerprint, init_services

RESUME = "resume_execution"  # the run has a compose: validate it again, then continue
CONTINUE = "continue_cluster"  # a new run on a marked cluster: start it normally

# Risks the user must acknowledge one by one; the platform cannot verify them.
INIT_PARTIAL = "init_partial"
INSTANCE_CHANGED = "instance_changed"
PREVIOUS_UNCONFIRMED = "previous_unconfirmed"


@dataclass(frozen=True)
class Plan:
    mode: str
    content: dict[str, Any]  # shown to the user and bound to the approval
    facts: dict[str, Any]  # what the plan relies on
    required: list[str]  # risk ids the approval must acknowledge, exactly
    digest: str


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def compose_digest(compose: dict[str, Any]) -> str:
    return hashlib.sha256(to_yaml(compose).encode()).hexdigest()


def facts_of(check: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
    """The facts of a Runner check and observation that a plan relies on."""
    services = sorted(
        (s["service"], s["state"], s.get("exit_code"))
        for s in (observation or {}).get("services", [])
    )
    return {
        "instance_id": check.get("instance_id"),
        "same_instance": check.get("same_instance"),
        "services": [list(s) for s in services],
    }


def _risk(risk_id: str, text: str, requires_ack: bool) -> dict[str, Any]:
    return {"id": risk_id, "text": text, "requires_ack": requires_ack}


def build_plan(
    *,
    values: dict[str, Any] | None,
    last_approved: dict[str, Any] | None,
    check: dict[str, Any],
    observation: dict[str, Any] | None,
    dispatched: list[str],
    marks: list[dict[str, Any]],
    catalog_digest: str,
) -> Plan:
    """values: the interrupted thread's checkpoint values, None for a new run on a marked
    cluster. last_approved: the latest approval record, if any. check: the latest Runner check
    {instance_id, idle, same_instance}. observation: the latest observation's payload.
    dispatched: methods the interrupted execution sent. marks: the cluster's open marks."""
    risks = []
    if check.get("same_instance") is False:
        risks.append(
            _risk(
                INSTANCE_CHANGED,
                "The Runner restarted since the interrupted execution. You accept a risk the "
                "platform cannot verify: processes the old Runner started may still be running.",
                True,
            )
        )
    for m in marks:
        risks.append(_risk(f"mark:{m['id']}", f"Unconfirmed operation: {m['reason']}", False))
    base = {
        "marks": [m["id"] for m in marks],
        "observation": observation,
        "check": {k: check.get(k) for k in ("at", "instance_id", "idle", "same_instance")},
    }
    facts = {**facts_of(check, observation), "catalog": catalog_digest}

    if values is None or not values.get("compose"):
        risks.append(
            _risk(
                PREVIOUS_UNCONFIRMED,
                "An earlier run's last operation on this cluster is unconfirmed: containers, "
                "volumes, and data it may have changed are left as they are.",
                True,
            )
        )
        content = {**base, "mode": CONTINUE, "steps": ["start"], "risks": risks}
        return _plan(CONTINUE, content, facts, risks)

    compose = values["compose"]
    candidate = compose_digest(compose)
    services = [s["name"] for s in (values.get("spec") or {}).get("services", [])]
    workdir = [
        s["name"]
        for s in (values.get("spec") or {}).get("services", [])
        if s["source"]["kind"] == "workdir"
    ]
    skip = bool(values.get("built")) and "build" not in dispatched
    build = {
        "decision": "skip_all" if skip else "rebuild_all",
        "services": [] if skip else services,
        "reason": (
            "images were built and the interrupted execution sent no build"
            if skip
            else "no proof that every image is complete"
        ),
    }
    if not skip and workdir:
        risks.append(
            _risk(
                "rebuild_workdir",
                "These services run their project's build command again: " + ", ".join(workdir),
                False,
            )
        )
    if inits := sorted(init_services(compose)):
        risks.append(
            _risk(
                INIT_PARTIAL,
                "Init scripts may have run partly; check the data before continuing, because a "
                "script without a marker runs again: " + ", ".join(inits),
                True,
            )
        )
    if last_approved is None or last_approved["candidate_digest"] != candidate:
        risks.append(
            _risk(
                "compose_changed",
                "The compose differs from the last one a person approved "
                "(automatic fixes ran in between).",
                False,
            )
        )
    steps = ["validate", "build" if not skip else "build (skipped)", "up", "watch", "stabilize"]
    content = {
        **base,
        "mode": RESUME,
        "compose": to_yaml(compose),
        "candidate_digest": candidate,
        "previous_approved": (
            {
                "id": last_approved["id"],
                "at": last_approved["at"],
                "compose": last_approved["content"].get("compose"),
            }
            if last_approved
            else None
        ),
        "build": build,
        "steps": steps,
        "risks": risks,
    }
    return _plan(RESUME, content, {**facts, "candidate": candidate}, risks)


def _plan(mode: str, content: dict[str, Any], facts: dict[str, Any], risks: list) -> Plan:
    required = [r["id"] for r in risks if r["requires_ack"]]
    return Plan(mode, content, facts, required, _digest({"content": content, "facts": facts}))


def make_recovery_initial(
    content: dict[str, Any], values: dict[str, Any], deadline: float, source: dict[str, Any]
) -> dict[str, Any]:
    """The new thread's state for a resume_execution plan, built from a whitelist."""
    compose = yaml.safe_load(content["compose"])
    return {
        # kept: what the run is and what was approved to run
        "cluster": values["cluster"],
        "runner": values["runner"],
        "purpose": values.get("purpose"),
        "spec": values["spec"],
        "descriptors": dict(values.get("descriptors") or {}),
        "compose": compose,
        "fixes": list(values.get("fixes") or []),  # history, shown in later approvals
        # from this authorization only
        "approved": True,
        "allow": [],  # validate sends any finding back to a normal approval
        "built": content["build"]["decision"] == "skip_all",
        # set fresh
        "question": None,
        "asked": 0,
        "drafting": None,
        "findings": [],
        "problems": [],
        "snapshot": {"services": []},
        "proposal": None,
        "rejected": [],
        "unchecked": None,
        "restarts": {},
        "attempts": {},
        "rounds": 0,
        "seen": [fingerprint(compose)],
        "report": None,
        "escalation": None,
        "outcome": None,
        "deadline": deadline,
        "recovered": True,
        "source": source,
    }
