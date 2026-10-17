"""Deterministic orchestrator: the design's state machine (section 4) as a LangGraph graph.

    generate → validate → approve ⏸ → build → up → watch → stabilize → done
                  ↑                              │        │
                  └────────── diagnose ←─────────┴────────┘
                                 │ no rule / budget / oscillation
                                 ▼
                             escalate ⏸

Only `approve` and `escalate` wait for a person, via LangGraph interrupts; they do nothing
else, because a node that called interrupt() runs again from the top when resumed. All
cluster work goes through the Runner's tools, so the orchestrator never touches Docker.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from cagent.catalog import Catalog
from cagent.generate import GenerationError, generate, to_yaml
from cagent.hub import ToolResult
from cagent.patches import apply, fingerprint, within_environment
from cagent.rules import (
    Problem,
    first_fix,
    problems_from_snapshot,
    problems_from_validate,
    upstream_first,
)
from cagent.spec import ClusterSpec


class ToolCaller(Protocol):
    """What the orchestrator needs from the hub: RunnerHub, or a fake in tests."""

    async def call(
        self, runner_id: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> ToolResult: ...


@dataclass(frozen=True)
class Settings:
    """Budgets and timings. Defaults follow design section 5.5."""

    max_attempts_per_signature: int = 3
    max_rounds: int = 10
    deadline_s: float = 1800
    startup_timeout_s: float = 300
    poll_interval_s: float = 3
    stability_window_s: float = 120


class State(TypedDict, total=False):
    cluster: str
    runner: str
    spec: dict[str, Any]
    compose: dict[str, Any] | None
    allow: list[str]
    approved: bool
    built: bool
    findings: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    restarts: dict[str, int]
    attempts: dict[str, int]
    rounds: int
    seen: list[str]
    fixes: list[dict[str, Any]]
    report: dict[str, Any] | None
    escalation: dict[str, Any] | None
    deadline: float
    outcome: str | None


@dataclass(frozen=True)
class Status:
    """Where one cluster run stands."""

    outcome: str | None  # healthy, rejected, escalated; None while running or waiting
    waiting: dict[str, Any] | None  # the approval or escalation a person must answer
    compose: dict[str, Any] | None
    fixes: list[dict[str, Any]]


_BIND = re.compile(r"Bind for [0-9.:\[\]]*?:(\d+) failed|0\.0\.0\.0:(\d+).*address already in use")


class Orchestrator:
    def __init__(
        self,
        hub: ToolCaller,
        catalog: Catalog,
        checkpointer: Any,
        settings: Settings | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.hub, self.catalog, self.settings = hub, catalog, settings or Settings()
        self.sleep, self.clock = sleep, clock
        self.graph = self._graph().compile(checkpointer=checkpointer)

    # --- public API -------------------------------------------------------------------------

    async def start(self, thread_id: str, spec: ClusterSpec, runner: str) -> Status:
        initial: State = {
            "cluster": spec.name,
            "runner": runner,
            "spec": spec.model_dump(),
            "compose": None,
            "allow": [],
            "approved": False,
            "built": False,
            "findings": [],
            "problems": [],
            "restarts": {},
            "attempts": {},
            "rounds": 0,
            "seen": [],
            "fixes": [],
            "report": None,
            "escalation": None,
            "outcome": None,
            "deadline": self.clock() + self.settings.deadline_s,
        }
        await self.graph.ainvoke(initial, self._config(thread_id))
        return await self.status(thread_id)

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Status:
        """Answer the pending approval ({"approve": bool, "allow": [...]}) or escalation
        ({"action": "retry" | "abort"})."""
        await self.graph.ainvoke(Command(resume=decision), self._config(thread_id))
        return await self.status(thread_id)

    async def status(self, thread_id: str) -> Status:
        snap = await self.graph.aget_state(self._config(thread_id))
        values = snap.values
        waiting = snap.interrupts[0].value if snap.interrupts else None
        return Status(
            values.get("outcome"), waiting, values.get("compose"), values.get("fixes", [])
        )

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    # --- graph ------------------------------------------------------------------------------

    def _graph(self) -> StateGraph:
        g = StateGraph(State)
        for name in (
            "generate",
            "validate",
            "approve",
            "build",
            "up",
            "watch",
            "stabilize",
            "diagnose",
            "escalate",
            "done",
        ):
            g.add_node(name, getattr(self, f"_step_{name}"))
        g.add_edge(START, "generate")
        g.add_edge("done", END)
        return g

    async def _call(self, state: State, method: str, **params: Any) -> ToolResult:
        return await self.hub.call(state["runner"], method, {"name": state["cluster"], **params})

    @staticmethod
    def _escalation(kind: str, **detail: Any) -> Command:
        return Command(update={"escalation": {"reason": kind, **detail}}, goto="escalate")

    async def _step_generate(self, state: State) -> Command:
        try:
            compose = generate(ClusterSpec.model_validate(state["spec"]), self.catalog)
        except GenerationError as e:
            return self._escalation("clarify", problems=e.problems)
        return Command(update={"compose": compose, "seen": [fingerprint(compose)]}, goto="validate")

    async def _step_validate(self, state: State) -> Command:
        res = await self._call(
            state, "validate", compose=to_yaml(state["compose"]), allow=state["allow"]
        )
        if res.exit_code not in (0, 1) or not isinstance(res.output, dict):
            return self._escalation("runner_error", step="validate", stderr=res.stderr)
        if problems := problems_from_validate(res.output):
            return Command(update={"problems": [asdict(p) for p in problems]}, goto="diagnose")
        if res.output.get("findings"):
            return Command(
                update={"findings": res.output["findings"], "approved": False}, goto="approve"
            )
        if not state["approved"]:
            return Command(update={"findings": []}, goto="approve")
        return Command(goto="up" if state["built"] else "build")

    async def _step_approve(self, state: State) -> Command:
        decision = interrupt(
            {
                "kind": "approval",
                "compose": to_yaml(state["compose"]),
                "findings": state["findings"],
                "fixes": state["fixes"],
            }
        )
        if not decision.get("approve"):
            return Command(update={"outcome": "rejected"}, goto=END)
        return Command(
            update={"approved": True, "allow": decision.get("allow", state["allow"])},
            goto="validate",
        )

    async def _step_build(self, state: State) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        for ref in spec.services:
            desc = self.catalog.services[ref.name]
            params: dict[str, Any] = {
                "service": ref.name,
                "stack": desc.stack,
                "tag": state["compose"]["services"][ref.name]["image"],
            }
            kind = ref.source.kind
            if kind in ("release", "snapshot"):
                pattern = getattr(desc.sources, kind).nexus
                params["maven"] = pattern.replace(
                    "{version}", ref.source.ref.removesuffix("-SNAPSHOT")
                )
            elif kind == "workdir":
                params["workdir"] = ref.source.ref
            else:
                return self._escalation("unsupported_source", service=ref.name, source=kind)
            res = await self._call(state, "build", **params)
            if not res.ok:
                return self._escalation(
                    "build_failed", service=ref.name, result=res.output, stderr=res.stderr
                )
        return Command(update={"built": True}, goto="up")

    async def _step_up(self, state: State) -> Command:
        res = await self._call(state, "up", compose=to_yaml(state["compose"]), allow=state["allow"])
        if res.ok:
            return Command(goto="watch")
        if problems := self._bind_problems(state["compose"], res.stderr):
            return Command(update={"problems": [asdict(p) for p in problems]}, goto="diagnose")
        return self._escalation("up_failed", stderr=res.stderr)

    @staticmethod
    def _bind_problems(compose: dict[str, Any], stderr: str) -> list[Problem]:
        """Map docker's 'port is already allocated' errors to the services publishing it."""
        ports = {int(a or b) for a, b in _BIND.findall(stderr)}
        return [
            Problem("bind:address_in_use", name)
            for name, svc in compose["services"].items()
            if any(
                ":" in str(p) and int(str(p).split(":")[0]) in ports for p in svc.get("ports") or []
            )
        ]

    def _probes(self, state: State) -> list[dict[str, Any]]:
        """L3 probes from each app service's descriptor."""
        probes = []
        for ref in ClusterSpec.model_validate(state["spec"]).services:
            desc = self.catalog.services[ref.name]
            ports = {p.name: p.container for p in desc.runtime.ports}
            for pr in desc.probes:
                port = ports[pr.http.port] if pr.http.port else next(iter(ports.values()))
                probes.append(
                    {
                        "service": ref.name,
                        "http": {"port": port, "path": pr.http.path, "expect": pr.http.expect},
                    }
                )
        return probes

    async def _snapshot(self, state: State) -> dict[str, Any]:
        return (await self._call(state, "snapshot")).output or {"services": []}

    async def _step_watch(self, state: State) -> Command:
        """Poll until every service runs and is healthy, then verify; diagnose on failure."""
        deadline = self.clock() + self.settings.startup_timeout_s
        while True:
            snap = await self._snapshot(state)
            if problems := problems_from_snapshot(snap):
                return Command(update={"problems": [asdict(p) for p in problems]}, goto="diagnose")
            services = snap.get("services", [])
            settled = services and all(
                s["state"] == "running" and s.get("health") != "starting" for s in services
            )
            if settled:
                rep = (await self._call(state, "verify", probes=self._probes(state))).output
                if rep and rep.get("passed"):
                    restarts = {s["service"]: s.get("restart_count", 0) for s in services}
                    return Command(update={"report": rep, "restarts": restarts}, goto="stabilize")
                if self.clock() > deadline:
                    return self._escalation("verify_failed", report=rep)
            elif self.clock() > deadline:
                return self._escalation("startup_timeout", snapshot=snap)
            await self.sleep(self.settings.poll_interval_s)

    async def _step_stabilize(self, state: State) -> Command:
        """Healthy must stay healthy: no restarts and still passing after the window."""
        await self.sleep(self.settings.stability_window_s)
        snap = await self._snapshot(state)
        problems = problems_from_snapshot(snap)
        for s in snap.get("services", []):
            if s.get("restart_count", 0) != state["restarts"].get(s["service"], 0):
                problems.append(Problem("restart:unstable", s["service"]))
        if problems:
            return Command(update={"problems": [asdict(p) for p in problems]}, goto="diagnose")
        rep = (await self._call(state, "verify", probes=self._probes(state))).output
        if not (rep and rep.get("passed")):
            return self._escalation("verify_failed", report=rep)
        return Command(update={"report": rep}, goto="done")

    async def _step_diagnose(self, state: State) -> Command:
        """Apply the first T0 fix the budgets allow; escalate when none applies."""
        compose = state["compose"]
        problems = upstream_first([Problem(**p) for p in state["problems"]], compose)
        if state["rounds"] >= self.settings.max_rounds or self.clock() > state["deadline"]:
            return self._escalation("budget", problems=[p.signature for p in problems])
        exhausted = []
        for p in problems:
            if state["attempts"].get(p.signature, 0) >= self.settings.max_attempts_per_signature:
                exhausted.append(p.signature)
                continue
            fix = first_fix(p, compose)
            if fix is None or not within_environment(fix.patch):
                continue
            patched = apply(compose, fix.patch)
            if fingerprint(patched) in state["seen"]:
                return self._escalation("oscillation", fix=asdict(fix))
            return Command(
                update={
                    "compose": patched,
                    "seen": [*state["seen"], fingerprint(patched)],
                    "attempts": {
                        **state["attempts"],
                        p.signature: state["attempts"].get(p.signature, 0) + 1,
                    },
                    "rounds": state["rounds"] + 1,
                    "fixes": [*state["fixes"], asdict(fix)],
                },
                goto="validate",
            )
        reason = "budget" if exhausted else "no_rule"
        return self._escalation(reason, problems=[p.signature for p in problems])

    async def _step_escalate(self, state: State) -> Command:
        decision = interrupt(
            {"kind": "escalation", **(state["escalation"] or {}), "fixes": state["fixes"]}
        )
        if decision.get("action") != "retry":
            return Command(update={"outcome": "escalated"}, goto=END)
        return Command(
            update={
                "attempts": {},
                "rounds": 0,
                "escalation": None,
                "deadline": self.clock() + self.settings.deadline_s,
            },
            goto="validate" if state["compose"] else "generate",
        )

    async def _step_done(self, state: State) -> dict[str, Any]:
        return {"outcome": "healthy"}
