"""Fakes for the evaluation harness: a station over SimCluster, a host, and quick tools.

EvalSim treats a published port as taken when the host really holds it, because the harness
binds held ports for real; it also answers `down`. SimStation can stay busy (the Runner never
goes idle) and can fail to kill its process group, to drive every cleanup branch.
"""

import socket
from pathlib import Path
from typing import Any

import yaml

from cagent.eval.drive import Limits, Tools, tier_config
from cagent.eval.host import LogCapture
from cagent.eval.observe import EvidenceCompleter
from cagent.hub import ToolResult
from cagent.llm import Router
from cagent.orchestrator import Settings

from .fakellm import CONFIG
from .simcluster import SimCluster

QUICK = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)


def _held(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("", port))
    except OSError:
        return True
    finally:
        s.close()
    return False


class EvalSim(SimCluster):
    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.base_occupied = set(self.occupied)
        self.downs = 0

    def _validate(self, p: dict[str, Any]) -> ToolResult:
        doc = yaml.safe_load(p["compose"])
        ports = {port for svc in doc["services"].values() for port in self._host_ports(svc)}
        self.occupied = self.base_occupied | {port for port in ports if _held(port)}
        return super()._validate(p)

    def _down(self, p: dict[str, Any]) -> ToolResult:
        self.downs += 1
        return ToolResult(0, {"project": f"cagent-{p['name']}"})


class SimStation:
    runner_id = "eval"

    def __init__(
        self, sim: SimCluster, busy: bool = False, group_gone: bool = True, instance: str = "sim-1"
    ):
        self.sim, self.busy, self.group_gone = sim, busy, group_gone
        self.instance, self.pid = instance, None
        self.target = self
        self.stopped = self.killed = False

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        return await self.sim.call(runner_id, method, params, timeout)

    async def status(self) -> dict[str, Any]:
        active = {"method": "up"} if self.busy else None
        return {"instance_id": self.instance, "active": active, "queued": {}}

    async def stop(self) -> None:
        self.stopped = True

    async def kill(self) -> bool:
        self.killed = True
        return self.group_gone


class FakeHost:
    def __init__(
        self,
        residue: dict[str, list[str]] | None = None,
        events: list[str] | None = None,
        logs: list[LogCapture] | None = None,
    ):
        self.residue = residue or {"containers": [], "networks": [], "volumes": []}
        self.events = events or []
        self.captures = logs or []
        self.forced: list[str] = []
        self.digests = {"small": "sha256:s1", "large": "sha256:l1"}

    async def platform(self) -> str:
        return "linux/arm64"

    async def versions(self) -> dict[str, str]:
        return {"client": "test", "server": "test"}

    async def image_ids(self, images: list[str]) -> dict[str, str | None]:
        return {i: "sha256:test" for i in images}

    async def logs(self, project: str) -> list[LogCapture]:
        return list(self.captures)

    async def resources(self, project: str) -> dict[str, list[str]]:
        return self.residue

    async def created_since(self, project: str, since: float) -> list[str]:
        return list(self.events)

    async def force_down(self, project: str) -> None:
        self.forced.append(project)

    async def model_digests(self) -> dict[str, str]:
        return dict(self.digests)


def tools(
    station: SimStation | Any,
    completer: Any = None,
    host: FakeHost | None = None,
    settings: Settings = QUICK,
    idle_s: float = 0.05,
    attempt_s: float = 30,
) -> Tools:
    """Tools whose every attempt uses station (or station(dir) when it is callable)."""

    async def make(attempt_dir: Path):
        return station(attempt_dir) if callable(station) else station

    def router(mode: str, log):
        tiers = tier_config(CONFIG, mode)
        return None if tiers is None else Router(tiers, EvidenceCompleter(completer, log))

    return Tools(
        settings,
        host or FakeHost(),
        b"k" * 32,
        make,
        router,
        Limits(attempt_s=attempt_s, idle_s=idle_s, poll_s=0.01),
    )


def write_scenario(root: Path, doc: dict[str, Any], *templates: dict[str, Any]) -> Path:
    """A scenario directory, with its templates in the shared fixture catalog."""
    d = root / doc["id"]
    d.mkdir(parents=True)
    (d / "scenario.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    shared = root / "fixtures" / "catalog" / "middleware"
    shared.mkdir(parents=True, exist_ok=True)
    for t in templates:
        (shared / f"{t['name']}.yaml").write_text(yaml.safe_dump(t, sort_keys=False))
    return d


def template(name: str, env: dict[str, str] | None = None, **over: Any) -> dict[str, Any]:
    """A busybox web template; SimCluster decides how it behaves."""
    return {
        "name": name,
        "kind": "middleware",
        "provides": {"capability": name.split("-")[0]},
        "image": "busybox:{version}",
        "versions": ["1.37"],
        "default_version": "1.37",
        "port": 8080,
        "env": env or {},
        "healthcheck": {"cmd": ["CMD", "true"]},
        "presets": {
            "tiny": {"mem_limit": "64m", "cpus": 0.25},
            "roomy": {"mem_limit": "512m", "cpus": 0.5},
        },
        **over,
    }
