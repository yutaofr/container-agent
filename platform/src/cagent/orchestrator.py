"""Deterministic orchestrator: the design's state machine (section 4) as a LangGraph graph.

    extract ─→ generate ──gap──→ ask ⏸ ──answer──→ generate
                  │ └──unregistered workdir──→ inspect → save ⏸ → readback → generate
                  ▼
    generate → validate → approve ⏸ → build → up → watch → stabilize → done
                  ↑                              │        │
                  └────────── diagnose ←─────────┴────────┘
                     (T0 rules, then model tiers)  │ undeclared env key / JVM agent
                                 │                 ▼
                                 │          approve_patch ⏸
                                 │ no rule / no fix / budget / oscillation
                                 ▼
                             escalate ⏸

Only `ask`, `save`, `approve`, `approve_patch`, and `escalate` wait for a person, via
LangGraph interrupts;
they do nothing before interrupting, because a node that called interrupt() runs again from
the top when resumed. All cluster work goes through the Runner's tools, so the orchestrator
never touches Docker.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

import yaml
from jsonpatch import JsonPatchException
from jsonpointer import JsonPointerException
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from cagent.catalog import Catalog, ServiceDescriptor
from cagent.clarify import AnswerError, apply_answer, extract, question
from cagent.diagnose import declared_env, propose
from cagent.draft import DraftError, draft, read_back
from cagent.gaps import ANSWERABLE, Gap
from cagent.generate import GenerationError, generate, to_yaml
from cagent.hub import ToolResult
from cagent.llm import ModelUnavailable, Router
from cagent.patches import apply, fingerprint, init_services, within_environment
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
    max_questions: int = 20


class State(TypedDict, total=False):
    cluster: str
    runner: str
    purpose: str | None  # set when the run starts from a sentence instead of a spec
    spec: dict[str, Any] | None
    question: dict[str, Any] | None  # the gap being asked about, and the last answer's error
    asked: int
    drafting: dict[str, Any] | None  # a descriptor draft waiting to be saved
    descriptors: dict[str, str]  # service -> descriptor read back from the user's project
    compose: dict[str, Any] | None
    allow: list[str]
    approved: bool
    built: bool
    findings: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    snapshot: dict[str, Any]  # evidence for the model tiers
    proposal: dict[str, Any] | None  # a model fix waiting for approval
    rejected: list[dict[str, Any]]  # model fixes that could not be used
    unchecked: dict[str, Any] | None  # a model fix applied but not yet validated
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
# docker compose's error when a service_completed_successfully dependency exits non-zero
_NOT_COMPLETED = re.compile(r"service \"([A-Za-z0-9._-]+)\" didn't complete successfully")
# the generated init loop's last words: what it was running when it failed
_INIT_FAILED = re.compile(r"cagent init failed: (.+)")


def _completed(s: dict[str, Any]) -> bool:
    return s.get("role") == "init" and s["state"] == "exited" and s.get("exit_code") == 0


def _failed_init(snap: dict[str, Any]) -> dict[str, Any] | None:
    for s in snap.get("services") or []:
        if s.get("role") == "init" and s["state"] == "exited" and s.get("exit_code") != 0:
            return s
    return None


class Orchestrator:
    def __init__(
        self,
        hub: ToolCaller,
        catalog: Catalog,
        checkpointer: Any,
        settings: Settings | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        router: Router | None = None,
    ):
        """Without a router the orchestrator stays deterministic: T0 rules only."""
        self.hub, self.catalog, self.settings = hub, catalog, settings or Settings()
        self.sleep, self.clock, self.router = sleep, clock, router
        self.graph = self._graph().compile(checkpointer=checkpointer)

    # --- public API -------------------------------------------------------------------------

    async def start(self, thread_id: str, spec: ClusterSpec, runner: str) -> Status:
        await self.graph.ainvoke(self._initial(runner, spec=spec), self._config(thread_id))
        return await self.status(thread_id)

    async def start_from_purpose(
        self, thread_id: str, purpose: str, runner: str, cluster: str | None = None
    ) -> Status:
        """Start from a sentence: a model drafts the spec, then questions close its gaps.
        With cluster, the spec takes that name instead of the one the model picks."""
        if self.router is None:
            raise ValueError("starting from a purpose needs a model router")
        initial = self._initial(runner, purpose=purpose, cluster=cluster)
        await self.graph.ainvoke(initial, self._config(thread_id))
        return await self.status(thread_id)

    def _initial(
        self,
        runner: str,
        spec: ClusterSpec | None = None,
        purpose: str | None = None,
        cluster: str | None = None,
    ) -> State:
        return {
            "cluster": cluster or (spec.name if spec else ""),
            "runner": runner,
            "purpose": purpose,
            "spec": spec.model_dump() if spec else None,
            "question": None,
            "asked": 0,
            "drafting": None,
            "descriptors": {},
            "compose": None,
            "allow": [],
            "approved": False,
            "built": False,
            "findings": [],
            "problems": [],
            "snapshot": {"services": []},
            "proposal": None,
            "rejected": [],
            "unchecked": None,
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

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Status:
        """Answer what is pending: a question ({"answer": value | None}), a save
        ({"saved": True}), an approval ({"approve": bool, "allow": [...]}), or an escalation
        ({"action": "retry"}). {"action": "abort"} ends a question, save, or escalation."""
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
            "extract",
            "ask",
            "inspect",
            "save",
            "readback",
            "generate",
            "validate",
            "approve",
            "build",
            "up",
            "watch",
            "stabilize",
            "diagnose",
            "approve_patch",
            "escalate",
            "done",
        ):
            g.add_node(name, getattr(self, f"_step_{name}"))
        g.add_conditional_edges(
            START, lambda s: "generate" if s["spec"] else "extract", ["generate", "extract"]
        )
        g.add_edge("done", END)
        return g

    async def _call(self, state: State, method: str, **params: Any) -> ToolResult:
        return await self.hub.call(state["runner"], method, {"name": state["cluster"], **params})

    def _init_failed(self, s: dict[str, Any]) -> Command:
        """An init script failed: that is the project's code or data, not the environment, so
        no rule or model tries a fix. The user fixes it and retries; build stages it again."""
        lines = s.get("tail") or []
        script = next(
            (m.group(1) for ln in reversed(lines) if (m := _INIT_FAILED.search(ln))), None
        )
        return self._escalation("init_failed", service=s["service"], script=script, log=lines)

    @staticmethod
    def _escalation(kind: str, **detail: Any) -> Command:
        return Command(update={"escalation": {"reason": kind, **detail}}, goto="escalate")

    def _catalog(self, state: State) -> Catalog:
        """The shared catalog plus the descriptors read back from this run's projects."""
        saved = state.get("descriptors") or {}
        if not saved:
            return self.catalog
        services = {
            name: ServiceDescriptor.model_validate(yaml.safe_load(text))
            for name, text in saved.items()
        }
        return Catalog({**self.catalog.services, **services}, self.catalog.middleware)

    async def _step_extract(self, state: State) -> Command:
        try:
            spec = await extract(self.router, state["purpose"], self.catalog)
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", step="extract", detail=str(e))
        if state["cluster"]:  # the user named the cluster; State and spec must agree
            spec = ClusterSpec.model_validate({**spec.model_dump(), "name": state["cluster"]})
        return Command(update={"spec": spec.model_dump(), "cluster": spec.name}, goto="generate")

    async def _step_generate(self, state: State) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        try:
            compose = generate(spec, self._catalog(state))
        except GenerationError as e:
            return self._close_gap(state, spec, e)
        return Command(update={"compose": compose, "seen": [fingerprint(compose)]}, goto="validate")

    def _close_gap(self, state: State, spec: ClusterSpec, e: GenerationError) -> Command:
        """Ask about the first gap, inspect an unregistered project, or escalate."""
        gap = e.gaps[0]
        if gap.kind == "unregistered":
            ref = next(s for s in spec.services if s.name == gap.target)
            if ref.source.kind == "workdir":
                drafting = {"service": ref.name, "workdir": ref.source.ref, "error": None}
                return Command(update={"drafting": drafting}, goto="inspect")
        answerable = gap.kind in ANSWERABLE and (gap.options or gap.kind == "missing_env")
        if answerable and state["asked"] < self.settings.max_questions:
            return Command(update={"question": {"gap": gap.to_dict(), "error": None}}, goto="ask")
        return self._escalation("clarify", problems=e.problems)

    async def _step_ask(self, state: State) -> Command:
        q = state["question"]
        gap = Gap.from_dict(q["gap"])
        decision = interrupt(
            {
                "kind": "question",
                "gap": q["gap"],
                "question": question(gap),
                "options": q["gap"]["options"],
                "default": gap.default,
                "error": q["error"],
            }
        )
        if decision.get("action") == "abort":
            return Command(update={"outcome": "rejected"}, goto=END)
        asked = state["asked"] + 1
        try:
            spec = apply_answer(
                ClusterSpec.model_validate(state["spec"]), gap, decision.get("answer")
            )
        except AnswerError as err:
            return Command(
                update={"asked": asked, "question": {**q, "error": str(err)}}, goto="ask"
            )
        return Command(
            update={"spec": spec.model_dump(), "asked": asked, "question": None}, goto="generate"
        )

    async def _inspect(self, state: State) -> ToolResult:
        workdir = state["drafting"]["workdir"]
        return await self.hub.call(state["runner"], "inspect", {"workdir": workdir})

    async def _step_inspect(self, state: State) -> Command:
        """Use a descriptor the project already has; otherwise draft one for the user."""
        d = state["drafting"]
        res = await self._inspect(state)
        if not res.ok or not isinstance(res.output, dict):
            return self._escalation("runner_error", step="inspect", stderr=res.stderr)
        try:
            read_back(d["service"], res.output)
        except DraftError:
            pass
        else:
            return self._saved(state, res.output)
        if self.router is None:
            return self._escalation(
                "clarify",
                problems=[f"{d['service']}: not registered, and no model to draft a descriptor"],
            )
        try:
            drafted = await draft(self.router, d["service"], res.output, self.catalog)
        except DraftError as e:
            return self._escalation("clarify", problems=[str(e)])
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", step="draft", detail=str(e))
        return Command(
            update={
                "drafting": {
                    **d,
                    "files": {drafted.path: drafted.text},
                    "note": drafted.note,
                }
            },
            goto="save",
        )

    async def _step_save(self, state: State) -> Command:
        d = state["drafting"]
        decision = interrupt(
            {
                "kind": "save_descriptors",
                "service": d["service"],
                "workdir": d["workdir"],
                "files": d["files"],
                "note": d["note"],
                "error": d["error"],
            }
        )
        if decision.get("action") == "abort":
            return Command(update={"outcome": "rejected"}, goto=END)
        return Command(goto="readback")

    async def _step_readback(self, state: State) -> Command:
        """Validate the descriptor the user saved, as the Runner will read it."""
        res = await self._inspect(state)
        if not res.ok or not isinstance(res.output, dict):
            return self._escalation("runner_error", step="inspect", stderr=res.stderr)
        try:
            read_back(state["drafting"]["service"], res.output)
        except DraftError as e:
            return Command(update={"drafting": {**state["drafting"], "error": str(e)}}, goto="save")
        return self._saved(state, res.output)

    @staticmethod
    def _saved(state: State, inspection: dict[str, Any]) -> Command:
        service = state["drafting"]["service"]
        files = inspection["files"]
        path = f".cagent/services/{service}.yaml"
        text = files.get(path, files.get(".cagent/service.yaml"))
        return Command(
            update={"descriptors": {**state["descriptors"], service: text}, "drafting": None},
            goto="generate",
        )

    async def _step_validate(self, state: State) -> Command:
        # Validate without approvals, so every finding is known; the user's allow is the full
        # set of exceptions for this candidate, and only uncovered findings need approval.
        res = await self._call(state, "validate", compose=to_yaml(state["compose"]), allow=[])
        if res.exit_code not in (0, 1) or not isinstance(res.output, dict):
            if unchecked := state.get("unchecked"):
                # A model patch passed the gate but broke the document: undo it and let the
                # next attempt start a tier higher, instead of escalating.
                return Command(
                    update={
                        "compose": unchecked["previous"],
                        "fixes": state["fixes"][:-1],
                        "unchecked": None,
                        "rejected": [
                            *state["rejected"],
                            {**unchecked["fix"], "why": f"invalid compose: {res.stderr[-300:]}"},
                        ],
                    },
                    goto="diagnose",
                )
            return self._escalation("runner_error", step="validate", stderr=res.stderr)
        checked = {"unchecked": None}
        if problems := problems_from_validate(res.output):
            return Command(
                update={**checked, "problems": [asdict(p) for p in problems]}, goto="diagnose"
            )
        findings = res.output.get("findings") or []
        allowed = set(state["allow"])
        if any(f"{f['service']}:{f['rule']}" not in allowed for f in findings):
            return Command(
                update={**checked, "findings": findings, "approved": False}, goto="approve"
            )
        if not state["approved"]:
            return Command(update={**checked, "findings": findings}, goto="approve")
        return Command(update=checked, goto="up" if state["built"] else "build")

    async def _step_approve(self, state: State) -> Command:
        decision = interrupt(
            {
                "kind": "approval",
                "compose": to_yaml(state["compose"]),
                "findings": state["findings"],
                "allow": state["allow"],
                "fixes": state["fixes"],
            }
        )
        if not decision.get("approve"):
            return Command(update={"outcome": "rejected"}, goto=END)
        # The decision's allow replaces the old one: it is the complete set for this candidate.
        return Command(
            update={"approved": True, "allow": list(decision.get("allow") or [])},
            goto="validate",
        )

    async def _step_build(self, state: State) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        catalog = self._catalog(state)
        for ref in spec.services:
            desc = catalog.services[ref.name]
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
                scripts = [
                    {"capability": r.capability, "path": path}
                    for r in desc.requires
                    if r.init
                    for path in r.init.scripts
                ]
                if scripts:
                    params["init"] = scripts
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
        m = _NOT_COMPLETED.search(res.stderr or "")
        if m and m.group(1) in init_services(state["compose"]):
            snap = await self._snapshot(state)
            failed = next((s for s in snap["services"] if s["service"] == m.group(1)), None)
            return self._init_failed(failed or {"service": m.group(1), "tail": []})
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
        catalog = self._catalog(state)
        for ref in ClusterSpec.model_validate(state["spec"]).services:
            desc = catalog.services[ref.name]
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
            if failed := _failed_init(snap):
                return self._init_failed(failed)
            if problems := problems_from_snapshot(snap):
                return Command(
                    update={"problems": [asdict(p) for p in problems], "snapshot": snap},
                    goto="diagnose",
                )
            services = snap.get("services", [])
            settled = services and all(
                (s["state"] == "running" and s.get("health") != "starting") or _completed(s)
                for s in services
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
            return Command(
                update={"problems": [asdict(p) for p in problems], "snapshot": snap},
                goto="diagnose",
            )
        rep = (await self._call(state, "verify", probes=self._probes(state))).output
        if not (rep and rep.get("passed")):
            return self._escalation("verify_failed", report=rep)
        return Command(update={"report": rep}, goto="done")

    async def _step_diagnose(self, state: State) -> Command:
        """Apply the first T0 fix the budgets allow; for problems no rule covers, ask the
        model tiers when a router is configured; escalate when neither helps."""
        compose = state["compose"]
        problems = upstream_first([Problem(**p) for p in state["problems"]], compose)
        if state["rounds"] >= self.settings.max_rounds or self.clock() > state["deadline"]:
            return self._escalation("budget", problems=[p.signature for p in problems])
        exhausted, unruled = [], []
        for p in problems:
            if state["attempts"].get(p.signature, 0) >= self.settings.max_attempts_per_signature:
                exhausted.append(p.signature)
                continue
            fix = first_fix(p, compose)
            if fix is None or not within_environment(fix.patch, init_services(compose)):
                unruled.append(p)
                continue
            return self._apply_fix(state, p.signature, asdict(fix), fix.patch)
        if unruled and self.router is not None:
            return await self._ask_model(state, unruled[0])
        reason = "budget" if exhausted else "no_rule"
        return self._escalation(reason, problems=[p.signature for p in problems])

    def _apply_fix(
        self, state: State, signature: str, fix: dict[str, Any], patch: list[dict[str, Any]]
    ) -> Command:
        try:
            patched = apply(state["compose"], patch)
        except (JsonPatchException, JsonPointerException) as e:
            return self._failed_attempt(state, signature, fix, f"patch does not apply: {e}")
        if fingerprint(patched) in state["seen"]:
            if fix["rule"].startswith("llm:"):
                # A model repeating a state already tried is stuck, not oscillating: let the
                # next attempt start a tier higher, within the per-signature budget.
                return self._failed_attempt(state, signature, fix, "repeats a state already tried")
            return self._escalation("oscillation", fix=fix)
        return Command(
            update={
                "compose": patched,
                "seen": [*state["seen"], fingerprint(patched)],
                "attempts": {
                    **state["attempts"],
                    signature: state["attempts"].get(signature, 0) + 1,
                },
                "rounds": state["rounds"] + 1,
                "fixes": [*state["fixes"], fix],
                "proposal": None,
                "unchecked": (
                    {"signature": signature, "fix": fix, "previous": state["compose"]}
                    if fix["rule"].startswith("llm:")
                    else None
                ),
            },
            goto="validate",
        )

    def _failed_attempt(
        self, state: State, signature: str, fix: dict[str, Any], why: str
    ) -> Command:
        """Count a model fix that could not be used; the next try starts a tier higher."""
        return Command(
            update={
                "attempts": {
                    **state["attempts"],
                    signature: state["attempts"].get(signature, 0) + 1,
                },
                "rounds": state["rounds"] + 1,
                "rejected": [*state["rejected"], {**fix, "why": why}],
            },
            goto="diagnose",
        )

    async def _ask_model(self, state: State, problem: Problem) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        try:
            proposal = await propose(
                self.router,
                problem,
                state["snapshot"],
                state["compose"],
                declared_env(spec, self._catalog(state)),
                state["attempts"].get(problem.signature, 0),
            )
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", problem=problem.signature, detail=str(e))
        fix = {
            "rule": f"llm:{proposal.tier}",
            "signature": proposal.signature,
            "patch": proposal.patch,
            "reason": proposal.root_cause,
        }
        if proposal.verdict == "give_up":
            return self._escalation(
                "no_fix",
                problem=problem.signature,
                root_cause=proposal.root_cause,
                tier=proposal.tier,
            )
        if proposal.verdict == "reject":
            return self._failed_attempt(
                state, problem.signature, fix, f"{proposal.tier}: {'; '.join(proposal.reasons)}"
            )
        if proposal.verdict == "approve":
            return Command(
                update={"proposal": {**asdict(proposal), "fix": fix}}, goto="approve_patch"
            )
        return self._apply_fix(state, problem.signature, fix, proposal.patch)

    async def _step_approve_patch(self, state: State) -> Command:
        p = state["proposal"]
        decision = interrupt(
            {
                "kind": "patch_approval",
                "signature": p["signature"],
                "tier": p["tier"],
                "root_cause": p["root_cause"],
                "patch": p["patch"],
                "reasons": p["reasons"],
            }
        )
        if not decision.get("approve"):
            return self._escalation(
                "patch_rejected", problem=p["signature"], root_cause=p["root_cause"]
            )
        return self._apply_fix(state, p["signature"], p["fix"], p["patch"])

    async def _step_escalate(self, state: State) -> Command:
        decision = interrupt(
            {"kind": "escalation", **(state["escalation"] or {}), "fixes": state["fixes"]}
        )
        if decision.get("action") != "retry":
            return Command(update={"outcome": "escalated"}, goto=END)
        reason = (state["escalation"] or {}).get("reason")
        return Command(
            update={
                "attempts": {},
                "rounds": 0,
                "escalation": None,
                "deadline": self.clock() + self.settings.deadline_s,
                # a fixed init script reaches the cluster only through build's staging
                "built": state["built"] and reason != "init_failed",
            },
            goto="validate" if state["compose"] else "generate",
        )

    async def _step_done(self, state: State) -> dict[str, Any]:
        return {"outcome": "healthy"}
