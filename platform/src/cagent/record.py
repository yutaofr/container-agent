"""The run record: what each pending item showed and what was decided on it, merged with
the run's kept events in one sequence.

The record keeps its own copy of pending items, so it must not become a second, permanent
home for secrets. One masking rule covers published content and decisions alike: every
value of a missing_env question (the answer, the default, options, error text, nested
copies) is masked, as is any question whose key looks like a secret. Variable names and what
the user did ("answered", "took the default", "aborted") stay visible.
"""

import json
import re
from typing import Any

MASK = {"masked": True}
SECRET = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|KEY|CREDENTIAL", re.IGNORECASE)


def sensitive(kind: str, payload: dict[str, Any]) -> bool:
    if kind != "question":
        return False
    gap = payload.get("gap") or {}
    return gap.get("kind") == "missing_env" or bool(SECRET.search(gap.get("key") or ""))


def _mask_values(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    if out.get("default") is not None:
        out["default"] = MASK
    if out.get("options"):
        out["options"] = [MASK for _ in out["options"]]
    if out.get("error"):
        out["error"] = MASK
    return out


def mask_published(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A pending item's payload as the record keeps it."""
    if not sensitive(kind, payload):
        return payload
    masked = _mask_values(payload)
    if isinstance(payload.get("gap"), dict):
        masked["gap"] = _mask_values(payload["gap"])
    return masked


def decision_record(kind: str, payload: dict[str, Any], decision: dict[str, Any]) -> dict:
    """A validated decision as the record keeps it: the action, and values unless masked."""
    if decision.get("action") == "abort":
        return {"action": "abort"}
    if kind == "question":
        if decision.get("answer") is None:
            return {"action": "default"}
        answer = MASK if sensitive(kind, payload) else decision["answer"]
        return {"action": "answer", "answer": answer}
    if kind == "save_descriptors":
        return {"action": "saved"}  # a claim the read-back checks
    return dict(decision)


def size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry))


def page(
    pendings: list[dict[str, Any]],
    events: list[dict[str, Any]],
    limit: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Merge rows by seq into one page; True if more rows follow. One entry over max_bytes
    is a page of its own with its content replaced by a marker, so the cursor always moves."""
    merged = sorted([*pendings, *events], key=lambda e: e["seq"])
    out: list[dict[str, Any]] = []
    total = 0
    for entry in merged[:limit]:
        n = size(entry)
        if not out and n > max_bytes:
            body = "decision" if "decision" in entry else "payload"
            out.append({**entry, body: {"truncated": True, "bytes": n}, "truncated": True})
            break
        if out and total + n > max_bytes:
            break
        out.append(entry)
        total += n
    return out, len(merged) > len(out)
