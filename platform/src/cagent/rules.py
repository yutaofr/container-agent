"""T0 rules: deterministic fixes for well-known failures, no LLM (design 5.3).

A rule sees one Problem and the current compose document and returns a Fix (a JSON Patch with
its reason) or None when it cannot help. Problems come from the Runner's validate report and
snapshots, keyed by the error signatures M1 defined.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MEM_CAP_MB = 4096  # never raise a memory limit beyond this automatically
START_PERIOD_CAP_S = 600


@dataclass(frozen=True)
class Problem:
    kind: str  # e.g. "oom:killed", "port:conflict", "health:unhealthy"
    service: str
    detail: str = ""

    @property
    def signature(self) -> str:
        return f"{self.kind}@{self.service}"


@dataclass(frozen=True)
class Fix:
    rule: str
    signature: str
    patch: list[dict[str, Any]]
    reason: str


def problems_from_validate(report: dict[str, Any]) -> list[Problem]:
    return [
        Problem("port:conflict", c["service"], str(c["port"]))
        for c in report.get("port_conflicts") or []
    ]


def problems_from_snapshot(snapshot: dict[str, Any]) -> list[Problem]:
    """Every signature the Runner reported, plus unhealthy services that logged no error."""
    out: list[Problem] = []
    for s in snapshot.get("services") or []:
        for sig in s.get("signatures") or []:
            kind, _, service = sig.rpartition("@")
            out.append(Problem(kind, service))
        if s.get("health") == "unhealthy" and not s.get("signatures"):
            out.append(Problem("health:unhealthy", s["service"], s.get("health_log", "")))
    return out


def upstream_first(problems: list[Problem], compose: dict[str, Any]) -> list[Problem]:
    """Drop problems of services whose dependencies also have problems: those are symptoms."""
    broken = {p.service for p in problems}
    services = compose.get("services", {})
    return [
        p
        for p in problems
        if not broken & set(services.get(p.service, {}).get("depends_on", {}) or {})
    ]


# --- helpers --------------------------------------------------------------------------------

_SIZE = re.compile(r"^(\d+)\s*([kmg]?)b?$", re.IGNORECASE)


def to_mb(size: str) -> int | None:
    m = _SIZE.match(str(size).strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return {"k": n // 1024, "m": n, "g": n * 1024, "": n // (1024 * 1024)}[unit]


def _duration_s(value: str) -> int | None:
    m = re.fullmatch(r"(\d+)(s|m)", str(value))
    if not m:
        return None
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def _set(path: str, present: bool, value: Any) -> dict[str, Any]:
    return {"op": "replace" if present else "add", "path": path, "value": value}


# --- rules ----------------------------------------------------------------------------------


def _free_port(port: int, used: set[int]) -> int | None:
    """The nearest port 1000 apart, upward first, then downward, that the document does not
    use and that stays within 1024–65535."""
    for step in (1000, -1000):
        candidate = port + step
        while 1024 <= candidate <= 65535:
            if candidate not in used:
                return candidate
            candidate += step
    return None


def port_conflict(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    """Move a clashing published host port by 1000 (up, or down near the top) to a port unused
    in the document."""
    if problem.kind not in ("port:conflict", "bind:address_in_use"):
        return None
    svc = compose["services"].get(problem.service, {})
    ports = svc.get("ports") or []
    used = {
        int(p.split(":")[0])
        for s in compose["services"].values()
        for p in s.get("ports") or []
        if ":" in str(p)
    }
    patch, moved = [], []
    for i, entry in enumerate(ports):
        host, sep, container = str(entry).partition(":")
        if not sep or (problem.kind == "port:conflict" and host != problem.detail):
            continue
        new = _free_port(int(host), used)
        if new is None:
            return None
        used.add(new)
        patch.append(
            {
                "op": "replace",
                "path": f"/services/{problem.service}/ports/{i}",
                "value": f"{new}:{container}",
            }
        )
        moved.append(f"{host}→{new}")
    if not patch:
        return None
    return Fix(
        "port_conflict", problem.signature, patch, f"host port in use; moved {', '.join(moved)}"
    )


def oom_killed(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    """Double the container memory limit, up to MEM_CAP_MB."""
    if problem.kind != "oom:killed":
        return None
    svc = compose["services"][problem.service]
    current = to_mb(svc["mem_limit"]) if "mem_limit" in svc else 256
    if current is None or current >= MEM_CAP_MB:
        return None
    new = min(current * 2, MEM_CAP_MB)
    return Fix(
        "oom_killed",
        problem.signature,
        [_set(f"/services/{problem.service}/mem_limit", "mem_limit" in svc, f"{new}m")],
        f"killed for exceeding {current}m; raised mem_limit to {new}m",
    )


_XMX = re.compile(r"-Xmx(\d+)([mMgG])")


def java_heap(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    """Double -Xmx in JAVA_OPTS and keep mem_limit at least twice the heap."""
    if problem.kind != "oom:app":
        return None
    svc = compose["services"][problem.service]
    opts = (svc.get("environment") or {}).get("JAVA_OPTS", "")
    m = _XMX.search(opts)
    if not m:
        return None
    heap = int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    new_heap = heap * 2
    if new_heap * 2 > MEM_CAP_MB:
        return None
    patch = [
        {
            "op": "replace",
            "path": f"/services/{problem.service}/environment/JAVA_OPTS",
            "value": _XMX.sub(f"-Xmx{new_heap}m", opts, count=1),
        }
    ]
    limit = to_mb(svc["mem_limit"]) if "mem_limit" in svc else None
    if limit is None or limit < new_heap * 2:
        patch.append(
            _set(f"/services/{problem.service}/mem_limit", "mem_limit" in svc, f"{new_heap * 2}m")
        )
    return Fix(
        "java_heap", problem.signature, patch, f"Java heap exhausted; raised -Xmx to {new_heap}m"
    )


def arch_mismatch(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    """Run an amd64-only image under emulation."""
    if problem.kind != "image:arch_mismatch":
        return None
    if "platform" in compose["services"][problem.service]:
        return None
    return Fix(
        "arch_mismatch",
        problem.signature,
        [{"op": "add", "path": f"/services/{problem.service}/platform", "value": "linux/amd64"}],
        "image has no build for this CPU; run it as linux/amd64",
    )


def slow_start(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    """Give a service that turns unhealthy without logging errors more time to start."""
    if problem.kind != "health:unhealthy":
        return None
    hc = compose["services"][problem.service].get("healthcheck")
    if not hc:
        return None
    period = _duration_s(hc.get("start_period", "0s")) or 0
    new = max(period * 2, 30)
    if period >= START_PERIOD_CAP_S:
        return None
    new = min(new, START_PERIOD_CAP_S)
    base = f"/services/{problem.service}/healthcheck"
    patch = [_set(f"{base}/start_period", "start_period" in hc, f"{new}s")]
    if "retries" in hc:
        patch.append({"op": "replace", "path": f"{base}/retries", "value": hc["retries"] * 2})
    return Fix(
        "slow_start",
        problem.signature,
        patch,
        f"unhealthy with no errors logged; start_period raised to {new}s",
    )


RULES: list[Callable[[Problem, dict[str, Any]], Fix | None]] = [
    port_conflict,
    oom_killed,
    java_heap,
    arch_mismatch,
    slow_start,
]


def first_fix(problem: Problem, compose: dict[str, Any]) -> Fix | None:
    for rule in RULES:
        if (fix := rule(problem, compose)) is not None:
            return fix
    return None
