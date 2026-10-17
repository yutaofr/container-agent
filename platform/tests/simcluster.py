"""A fake Runner and Docker for orchestrator tests.

It answers the Runner's tools from a tiny model of a cluster: which host ports are taken, how
much memory each service needs, which services crash, and how long healthchecks stay in
"starting". That is enough to drive every branch of the state machine deterministically.
"""

from typing import Any

import yaml

from cagent.hub import ToolResult
from cagent.rules import to_mb


class SimCluster:
    def __init__(
        self,
        *,
        occupied_ports: set[int] | None = None,
        needs_mb: dict[str, int] | None = None,
        crash: dict[str, int] | None = None,
        findings: list[dict[str, str]] | None = None,
        starting_snapshots: int = 0,
    ):
        self.occupied = occupied_ports or set()
        self.needs_mb = needs_mb or {}  # below this mem_limit the service is OOM-killed
        self.crash = crash or {}  # service -> exit code, a failure no T0 rule fixes
        self.findings = findings or []  # lint findings until the user allows them
        self.starting_snapshots = starting_snapshots
        self.compose: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.builds: list[dict[str, Any]] = []

    async def call(self, runner_id: str, method: str, params: dict[str, Any], timeout=None):
        self.calls.append(method)
        return getattr(self, f"_{method}")(params)

    @staticmethod
    def _host_ports(svc: dict[str, Any]) -> list[int]:
        return [int(str(p).split(":")[0]) for p in svc.get("ports") or [] if ":" in str(p)]

    def _validate(self, p: dict[str, Any]) -> ToolResult:
        doc = yaml.safe_load(p["compose"])
        conflicts = [
            {"service": name, "port": port, "reason": "in-use"}
            for name, svc in doc["services"].items()
            for port in self._host_ports(svc)
            if port in self.occupied
        ]
        allowed = set(p.get("allow") or [])
        findings = [f for f in self.findings if f"{f['service']}:{f['rule']}" not in allowed]
        out = {"project": p["name"], "findings": findings, "port_conflicts": conflicts}
        return ToolResult(1 if findings or conflicts else 0, out)

    def _build(self, p: dict[str, Any]) -> ToolResult:
        self.builds.append(p)
        return ToolResult(0, {"image": p["tag"], "cached": False})

    def _up(self, p: dict[str, Any]) -> ToolResult:
        self.compose = yaml.safe_load(p["compose"])
        self._starting = self.starting_snapshots
        return ToolResult(0, {"project": f"cagent-{p['name']}"})

    def _service(self, name: str, svc: dict[str, Any]) -> dict[str, Any]:
        s = {
            "service": name,
            "state": "running",
            "exit_code": 0,
            "oom_killed": False,
            "restart_count": 0,
            "error_lines": [],
            "tail": [],
            "signatures": [],
        }
        need, limit = self.needs_mb.get(name), to_mb(svc.get("mem_limit", "0"))
        if need and (limit is None or limit < need):
            s.update(
                state="exited", exit_code=137, oom_killed=True, signatures=[f"oom:killed@{name}"]
            )
        elif name in self.crash:
            code = self.crash[name]
            s.update(state="exited", exit_code=code, signatures=[f"exit:{code}@{name}"])
        elif "healthcheck" in svc:
            s["health"] = "starting" if self._starting > 0 else "healthy"
        return s

    def _snapshot(self, p: dict[str, Any]) -> ToolResult:
        services = [self._service(n, s) for n, s in sorted(self.compose["services"].items())]
        self._starting = max(0, self._starting - 1)
        return ToolResult(0, {"project": f"cagent-{p['name']}", "services": services})

    def _verify(self, p: dict[str, Any]) -> ToolResult:
        services = [self._service(n, s) for n, s in sorted(self.compose["services"].items())]
        ok = all(s["state"] == "running" and s.get("health") in (None, "healthy") for s in services)
        reached = ("L3" if p.get("probes") else "L2") if ok else ""
        return ToolResult(0 if ok else 1, {"passed": ok, "reached": reached, "results": []})
