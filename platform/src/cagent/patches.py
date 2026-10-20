"""Structured compose patches: apply them, keep them inside the environment, fingerprint them.

Every automatic fix is an RFC 6902 JSON Patch against the compose document (design 5.4).
"""

import copy
import hashlib
import json
import re
from typing import Any

import jsonpatch
from jsonpointer import JsonPointer, JsonPointerException

# The gate every automatic fix passes (design 5.4). It is deliberately strict: today only the
# in-repo T0 rules produce patches, but M7's model-written patches must clear the same check.
#
# Operations: only the four that name a single pointer. `move` and `copy` carry a second
# pointer (`from`) that can read from anywhere in the document, so they are refused, and so is
# any operation that carries a `from` at all.
_ALLOWED_OPS = frozenset({"add", "replace", "remove", "test"})

# Paths an automatic fix may touch: per-service environment settings, never images, builds,
# commands, volumes, or anything security-relevant. A patch outside these needs approval.
# Matched in full (a trailing newline or extra segment fails). Healthcheck is limited to its
# timing fields: `test` would let a patch change what the container runs.
_SERVICE = r"[A-Za-z0-9._-]+"
_ENV_PATH = re.compile(
    rf"/services/{_SERVICE}/"
    r"(ports(/\d+|/-)?|mem_limit|cpus|environment(/[A-Za-z0-9_.~-]+)?|platform|depends_on(/.*)?"
    r"|healthcheck/(start_period|interval|timeout|retries))"
)
_ENV_KEY = re.compile(rf"/services/{_SERVICE}/environment/([A-Za-z0-9_.~-]+)")
_ENV_ROOT = re.compile(rf"/services/{_SERVICE}/environment")

# Variables that make a process load attacker-chosen code; no automatic fix may set them.
_DENIED_ENV_PREFIXES = ("LD_", "DYLD_")


def _unescape(segment: str) -> str | None:
    """Decode a JSON Pointer segment (RFC 6901); None when it holds an invalid escape."""
    if re.search(r"~(?![01])", segment) or segment.endswith("~"):
        return None
    return segment.replace("~1", "/").replace("~0", "~")


def _denied_env(name: object) -> bool:
    return not isinstance(name, str) or name.startswith(_DENIED_ENV_PREFIXES)


def _env_entry_denied(entry: object) -> bool:
    """A list-form environment entry is "KEY=value" or a bare "KEY"."""
    return _denied_env(entry.partition("=")[0] if isinstance(entry, str) else entry)


def _env_value_denied(value: object) -> bool:
    """Whole-environment values: a mapping of keys, or a list of "KEY=value" entries."""
    if isinstance(value, dict):
        return any(_denied_env(k) for k in value)
    if isinstance(value, list):
        return any(_env_entry_denied(e) for e in value)
    return True  # anything else is not a valid environment


def _op_allowed(op: object) -> bool:
    if not isinstance(op, dict) or op.get("op") not in _ALLOWED_OPS or "from" in op:
        return False
    path = op.get("path")
    if not isinstance(path, str) or not _ENV_PATH.fullmatch(path):
        return False
    if m := _ENV_KEY.fullmatch(path):
        key = m.group(1)
        if key == "-" or key.isdigit():  # list-form environment: the value is "KEY=value"
            return "value" not in op or not _env_entry_denied(op["value"])
        name = _unescape(key)
        return name is not None and not _denied_env(name)
    if _ENV_ROOT.fullmatch(path) and "value" in op:
        return not _env_value_denied(op["value"])
    return True


_SERVICE_OF = re.compile(rf"/services/({_SERVICE})(/.*)?")
_DEPENDS_ON = re.compile(rf"/services/{_SERVICE}/depends_on(/.*)?")


def init_services(compose: dict[str, Any]) -> frozenset[str]:
    """Services the generator labelled as one-shot init services (M7c)."""
    return frozenset(
        name
        for name, svc in (compose.get("services") or {}).items()
        if (svc.get("labels") or {}).get("cagent.role") == "init"
    )


def _touches(op: dict[str, Any], protected: frozenset[str]) -> bool:
    """An op on a protected service, on a dependency on one, or on a whole depends_on, which
    could drop a wait for init."""
    path = op.get("path", "")
    if (m := _SERVICE_OF.fullmatch(path)) and m.group(1) in protected:
        return True
    if m := _DEPENDS_ON.fullmatch(path):
        rest = (m.group(1) or "").lstrip("/").split("/", 1)[0]
        return rest == "" or _unescape(rest) in protected
    return False


def within_environment(
    patch: list[dict[str, Any]], protected: frozenset[str] = frozenset()
) -> bool:
    """True when every operation only changes environment-level service settings. Init
    services, and other services' waits for them, are never changed automatically: an init
    failure is the user's to fix."""
    return all(_op_allowed(op) and not (protected and _touches(op, protected)) for op in patch)


def apply(compose: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a patched copy; the input is never modified."""
    return jsonpatch.apply_patch(copy.deepcopy(compose), patch)


def fingerprint(compose: dict[str, Any]) -> str:
    """Stable hash of a compose document, used to detect a fix loop returning to a past state."""
    canonical = json.dumps(compose, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _at(doc: Any, path: str) -> dict[str, Any]:
    """What a JSON Pointer names: {"present": False} when nothing is there, which differs
    from a present null."""
    try:
        value = JsonPointer(path).resolve(doc)
    except JsonPointerException:
        return {"present": False}
    return {"present": True, "value": copy.deepcopy(value)}


def _parent_is_list(doc: Any, path: str) -> bool:
    parent = path.rsplit("/", 1)[0]
    return isinstance(_at(doc, parent).get("value"), list) if parent else False


def preview(compose: dict[str, Any], patch: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Each operation's effect, applied in order to a copy of compose, as a person approving
    the patch should see it; None when the patch does not apply, so nothing is guessed.

    before/after are {"present": bool, "value": ...}. An add into an array inserts (nothing
    was there before); a remove leaves nothing at that path (later elements move up, they
    are not the "after"); a test changes nothing and is shown as a check."""
    doc = copy.deepcopy(compose)
    steps = []
    for op in patch:
        path = op.get("path", "")
        inserts = op.get("op") == "add" and _parent_is_list(doc, path)
        before = {"present": False} if inserts else _at(doc, path)
        try:
            doc = apply(doc, [op])
        except (jsonpatch.JsonPatchException, JsonPointerException):
            return None
        if op["op"] == "remove":
            after = {"present": False}
        elif op["op"] == "test":
            after = before
        elif inserts and path.endswith("/-"):
            after = {"present": True, "value": copy.deepcopy(op.get("value"))}
        else:
            after = _at(doc, path)
        steps.append(
            {"op": op["op"], "path": path, "before": before, "after": after, "insert": inserts}
        )
    return steps
