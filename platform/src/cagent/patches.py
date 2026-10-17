"""Structured compose patches: apply them, keep them inside the environment, fingerprint them.

Every automatic fix is an RFC 6902 JSON Patch against the compose document (design 5.4).
"""

import copy
import hashlib
import json
import re
from typing import Any

import jsonpatch

# Paths an automatic fix may touch: per-service environment settings, never images, builds,
# commands, volumes, or anything security-relevant. A patch outside these needs approval.
_ENV_PATH = re.compile(
    r"^/services/[^/]+/"
    r"(ports(/\d+|/-)?|mem_limit|cpus|environment(/[^/]+)?|platform|depends_on(/.*)?|healthcheck(/.*)?)$"
)


def within_environment(patch: list[dict[str, Any]]) -> bool:
    """True when every operation only changes environment-level service settings."""
    return all(_ENV_PATH.match(op.get("path", "")) for op in patch)


def apply(compose: dict[str, Any], patch: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a patched copy; the input is never modified."""
    return jsonpatch.apply_patch(copy.deepcopy(compose), patch)


def fingerprint(compose: dict[str, Any]) -> str:
    """Stable hash of a compose document, used to detect a fix loop returning to a past state."""
    canonical = json.dumps(compose, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
