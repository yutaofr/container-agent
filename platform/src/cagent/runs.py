"""RunService: the one place that changes runs. The web layer calls it; it never touches
LangGraph or the Runner directly.

Every command is accepted in one short transaction (Tx1) that records the command, consumes
what it answers, and marks the run running, all or nothing. The graph then runs in a
background task; its events are committed one by one; its result is recorded in a second
transaction (Tx2) only if the run still belongs to that execution. A command and its
execution are therefore never duplicated, but a Runner operation and its recorded result are
not atomic: when an execution ends without a result, the run goes to reconcile.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from cagent.catalog import NAME_PATTERN
from cagent.dispatch import MUTATING, Execution, current
from cagent.hub import RunnerHub
from cagent.orchestrator import EventSink, Orchestrator, Status
from cagent.spec import ClusterSpec
from cagent.store import ACTIVE_STATES, Store

log = logging.getLogger(__name__)

TERMINAL = {"healthy": "healthy", "rejected": "rejected", "escalated": "escalated"}

# The fields each kind of pending item shows; everything else in the interrupt stays inside.
PAYLOAD_FIELDS = {
    "question": ("gap", "question", "options", "default", "error"),
    "save_descriptors": ("service", "workdir", "files", "note", "error"),
    "approval": ("compose", "findings", "allow", "fixes"),
    "patch_approval": ("signature", "tier", "root_cause", "patch", "reasons"),
    "escalation": (
        "reason",
        "step",
        "stderr",
        "problems",
        "problem",
        "service",
        "script",
        "log",
        "report",
        "snapshot",
        "fix",
        "root_cause",
        "tier",
        "detail",
        "result",
        "source",
        "fixes",
    ),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Strict):
    """Every command on an existing run."""

    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = None
    action: Literal["answer", "refresh", "review_recovery", "abandon"]
    pending_id: str | None = None
    decision: dict[str, Any] | None = None


class _Answer(_Strict):
    answer: Any  # required; null takes the question's default


class _Abort(_Strict):
    action: Literal["abort"]


class _Saved(_Strict):
    saved: Literal[True]  # asks for a read-back; it does not mean the file is valid


class _Approval(_Strict):
    approve: StrictBool
    allow: list[str] = []


class _PatchApproval(_Strict):
    approve: StrictBool


class _Escalation(_Strict):
    action: Literal["retry", "abort"]


class _Recovery(_Strict):
    approve: StrictBool
    acknowledged: list[str] = []


def check_decision(kind: str, payload: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Validate a decision for a pending item strictly; raise a 422 RunError otherwise."""
    models: dict[str, type[_Strict]] = {
        "question": _Abort if "action" in decision else _Answer,
        "save_descriptors": _Abort if "action" in decision else _Saved,
        "approval": _Approval,
        "patch_approval": _PatchApproval,
        "escalation": _Escalation,
        "recovery": _Recovery,
    }
    try:
        d = models[kind].model_validate(decision).model_dump()
    except ValidationError as e:
        raise RunError(422, "invalid_decision", str(e)) from None
    if kind == "approval":
        findings = {f"{f['service']}:{f['rule']}" for f in payload["findings"]}
        if not d["approve"] and d["allow"]:
            raise RunError(422, "invalid_decision", "a rejection allows nothing")
        if len(set(d["allow"])) != len(d["allow"]) or not set(d["allow"]) <= findings:
            raise RunError(422, "invalid_decision", "allow names only this candidate's findings")
    if kind == "recovery":
        acks = d["acknowledged"]
        if d["approve"] and (len(set(acks)) != len(acks) or set(acks) != set(payload["required"])):
            raise RunError(422, "invalid_decision", "acknowledge exactly the required risks")
        if not d["approve"] and acks:
            raise RunError(422, "invalid_decision", "a rejection acknowledges nothing")
    return d


class RunError(Exception):
    """A command or read the service refuses: maps to an HTTP status."""

    def __init__(self, status: int, code: str, detail: str = "", snapshot: Any = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status, self.code, self.detail, self.snapshot = status, code, detail, snapshot


def not_found() -> RunError:
    # The same answer for a missing run and one the user may not see.
    return RunError(404, "not_found")


@dataclass(frozen=True)
class Accepted:
    body: dict[str, Any]
    replay: bool = False


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# Runs the graph for one execution, reporting events to the sink; returns its Status.
Work = Callable[[EventSink], Awaitable[Status]]


class RunService:
    def __init__(
        self,
        store: Store,
        orchestrator: Orchestrator,
        hub: RunnerHub,
        users: Mapping[str, set[str]],
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        """users maps each user to the runners they may use, as configured now."""
        self.store, self.orch, self.hub, self.users = store, orchestrator, hub, users
        self.clock, self.new_id = clock, new_id
        self.accepting = True
        self.tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task] = set()
        self._runner_lock = asyncio.Lock()

    async def close(self) -> None:
        """Stop accepting commands, then stop the executions. Each records why it stopped;
        one that cannot is found unfinished at the next start."""
        self.accepting = False
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._background, return_exceptions=True)

    # --- runners ------------------------------------------------------------------------------

    def runner_changed(self, runner: str, online: bool, instance: str | None) -> None:
        """Hub listener: record the change and tell the runner's active runs."""
        task = asyncio.get_running_loop().create_task(self._record_runner(runner, online, instance))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _record_runner(self, runner: str, online: bool, instance: str | None) -> None:
        async with self._runner_lock, self.store.tx() as t:  # in the order the hub saw them
            await t.run(
                "INSERT INTO runner_status (runner, online, instance_id, changed_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (runner) DO UPDATE SET"
                " online = excluded.online, instance_id = excluded.instance_id,"
                " changed_at = excluded.changed_at",
                runner,
                int(online),
                instance,
                self.clock(),
            )
            marks = ", ".join("?" for _ in ACTIVE_STATES)
            runs = await t.all(
                f"SELECT id FROM runs WHERE runner = ? AND state IN ({marks})",
                runner,
                *ACTIVE_STATES,
            )
            for r in runs:
                await t.append_event(r["id"], "runner", {"online": online, "instance_id": instance})

    # --- access ------------------------------------------------------------------------------

    def _may_use(self, user: str, runner: str) -> bool:
        return runner in self.users.get(user, set())

    def _visible(self, user: str, run: dict[str, Any] | None) -> dict[str, Any]:
        if run is None or run["owner"] != user or not self._may_use(user, run["runner"]):
            raise not_found()
        return run

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _check_accepting(self) -> None:
        if not self.accepting:
            raise RunError(503, "shutting_down")

    async def _replay(self, t, user: str, command_id: str, dig: str) -> Accepted | None:
        """The original answer to a repeated command, or None for a new one."""
        old = await t.one(
            "SELECT * FROM commands WHERE user = ? AND command_id = ?", user, command_id
        )
        if old is None:
            return None
        if old["digest"] != dig:
            raise RunError(409, "command_id_reused")
        return Accepted(old["response"], replay=True)

    # --- reads -------------------------------------------------------------------------------

    async def snapshot(self, user: str, run_id: str) -> dict[str, Any]:
        """Everything a page shows about a run, read in one transaction."""
        async with self.store.read() as t:
            run = self._visible(user, await t.get_run(run_id))
            runner = await t.one("SELECT * FROM runner_status WHERE runner = ?", run["runner"])
        return self._view(run, runner)

    @staticmethod
    def _view(run: dict[str, Any], runner: dict[str, Any] | None) -> dict[str, Any]:
        pending = run["pending"]
        return {
            "id": run["id"],
            "runner": run["runner"],
            "cluster": run["cluster"],
            "input": run["input"],
            "state": run["state"],
            "revision": run["revision"],
            "phase": run["phase"],
            "execution_id": run["execution_id"],
            "pending": (
                {k: pending[k] for k in ("pending_id", "kind", "revision", "created_at", "payload")}
                if pending
                else None
            ),
            "reconcile": run["reconcile"],
            "last_check": run["last_check"],
            "runner_status": (
                {
                    "online": bool(runner["online"]),
                    "instance_id": runner["instance_id"],
                    "changed_at": runner["changed_at"],
                }
                if runner
                else {"online": False, "instance_id": None, "changed_at": None}
            ),
            "last_observation": run["last_observation"],
            "last_observation_attempt": run["last_observation_attempt"],
            "verified_at": run["verified_at"],
            "watching": False,  # nothing watches a cluster after it passed acceptance
            "event_seq": run["event_seq"],
            "min_event_seq": run["min_event_seq"],
        }

    async def list_runs(self, user: str) -> list[dict[str, Any]]:
        async with self.store.read() as t:
            rows = await t.all(
                "SELECT id, runner, cluster, state, revision, created_at FROM runs"
                " WHERE owner = ? ORDER BY created_at DESC",
                user,
            )
        return [r for r in rows if self._may_use(user, r["runner"])]

    async def command(self, user: str, run_id: str, command_id: str) -> dict[str, Any]:
        async with self.store.read() as t:
            self._visible(user, await t.get_run(run_id))
            row = await t.one(
                "SELECT * FROM commands WHERE user = ? AND command_id = ? AND run_id = ?",
                user,
                command_id,
                run_id,
            )
        if row is None:
            raise not_found()
        return {k: row[k] for k in ("command_id", "kind", "status", "effect", "reason", "response")}

    async def _snapshot_in(self, t, run: dict[str, Any]) -> dict[str, Any]:
        runner = await t.one("SELECT * FROM runner_status WHERE runner = ?", run["runner"])
        return self._view(run, runner)

    # --- commands on a run ------------------------------------------------------------------

    async def submit(self, user: str, run_id: str, body: dict[str, Any]) -> Accepted:
        """Accept one command on a run. Checks run in this order: access, the idempotency
        record, then (new commands only) revision and pending item, then the decision."""
        self._check_accepting()
        try:
            env = Envelope.model_validate(body)
        except ValidationError as e:
            raise RunError(422, "invalid_command", str(e)) from None
        async with self.store.read() as t:
            self._visible(user, await t.get_run(run_id))
        dig = digest(
            {
                "run_id": run_id,
                "action": env.action,
                "pending_id": env.pending_id,
                "decision": env.decision,
            }
        )
        async with self.store.read() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
        handler = getattr(self, f"_{env.action}")
        async with self._lock(run_id):
            return await handler(user, run_id, env, dig)

    async def _current(self, t, run_id: str, env: Envelope, state: str) -> dict[str, Any]:
        """The run, if it is in state at the revision the command expects; else a 409."""
        run = await t.get_run(run_id)
        if run["state"] != state:
            raise RunError(409, f"not_{state}", snapshot=await self._snapshot_in(t, run))
        if env.expected_revision != run["revision"]:
            raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
        return run

    async def _answer(self, user: str, run_id: str, env: Envelope, dig: str) -> Accepted:
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await self._current(t, run_id, env, "waiting")
            pending = run["pending"]
            if env.pending_id != pending["pending_id"]:
                raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
            decision = check_decision(pending["kind"], pending["payload"], env.decision or {})
            now = self.clock()
            if pending["kind"] in ("approval", "patch_approval"):
                await self._record_approval(t, user, run_id, pending, decision, now)
            execution_id = self.new_id()
            instance = self.hub.instance(run["runner"])
            body = await self._accept(
                t, user, run, env.command_id, dig, now, execution_id=execution_id
            )
            await t.update_run(
                run_id,
                pending=None,
                execution_id=execution_id,
                executions=run["executions"] + 1,
                execution_instance=instance,
            )
        thread = run["thread_id"]
        self._launch(
            run_id,
            execution_id,
            instance,
            (user, env.command_id),
            lambda sink: self.orch.resume(thread, decision, on_event=sink),
        )
        return Accepted(body)

    async def _accept(
        self,
        t,
        user: str,
        run: dict[str, Any],
        command_id: str,
        dig: str,
        now: float,
        execution_id: str | None,
        state: str = "running",
        status: str = "accepted",
        kind: str = "answer",
    ) -> dict[str, Any]:
        """Record an accepted command and move the run to state, in the caller's Tx1."""
        revision = await t.bump_revision(run["id"])
        body = {
            "command_id": command_id,
            "run_id": run["id"],
            "accepted_revision": revision,
            "execution_id": execution_id,
        }
        await t.insert(
            "commands",
            user=user,
            command_id=command_id,
            run_id=run["id"],
            kind=kind,
            **{"class": "advancing"},
            digest=dig,
            response=body,
            status=status,
            execution_id=execution_id,
            at=now,
        )
        await t.update_run(run["id"], state=state)
        await t.append_event(
            run["id"], "run_state", {"state": state, "revision": revision, "pending_id": None}
        )
        return body

    async def _record_approval(
        self,
        t,
        user: str,
        run_id: str,
        pending: dict[str, Any],
        decision: dict[str, Any],
        now: float,
    ) -> None:
        """An immutable record of what was shown and what the person decided."""
        payload, kind = pending["payload"], pending["kind"]
        if kind == "approval":  # the same digest recovery plans compare against
            shown = hashlib.sha256(payload["compose"].encode()).hexdigest()
        elif kind == "patch_approval":
            shown = digest(payload["patch"])
        else:
            shown = payload["digest"]
        await t.insert(
            "approvals",
            id=self.new_id(),
            run_id=run_id,
            pending_id=pending["pending_id"],
            kind=kind,
            candidate_digest=shown,
            content=payload,
            decision=decision,
            user=user,
            at=now,
        )

    # --- creating a run ----------------------------------------------------------------------

    async def create_run(
        self,
        user: str,
        command_id: str,
        runner: str,
        cluster: str,
        purpose: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> Accepted:
        """Create a run on (runner, cluster) and start it. The cluster's owner is whoever
        first used the name; a cluster with unconfirmed operations starts the run in
        reconcile instead (cagent.recovery)."""
        self._check_accepting()
        if not self._may_use(user, runner):
            raise RunError(404, "not_found", "unknown runner")
        dig = digest(
            {
                "kind": "create_run",
                "runner": runner,
                "cluster": cluster,
                "purpose": purpose,
                "spec": spec,
            }
        )
        async with self.store.read() as t:
            if replay := await self._replay(t, user, command_id, dig):
                return replay
        start = self._validate_start(cluster, purpose, spec)
        run_id, execution_id = self.new_id(), self.new_id()
        async with self._lock(f"cluster:{runner}:{cluster}"), self.store.tx() as t:
            if replay := await self._replay(t, user, command_id, dig):
                return replay
            row = await t.get_cluster(runner, cluster)
            if row and row["owner"] != user:
                raise RunError(409, "cluster_name_taken")
            if row and row["lock_run_id"]:
                holder = await t.get_run(row["lock_run_id"])
                if holder and holder["state"] in ACTIVE_STATES:
                    raise RunError(409, "cluster_busy")
            marks = row["marks"] if row else []
            now = self.clock()
            state = "reconcile" if marks else "running"
            await t.insert(
                "runs",
                id=run_id,
                owner=user,
                runner=runner,
                cluster=cluster,
                input={"purpose": purpose} if purpose is not None else {"spec": start},
                created_at=now,
                state=state,
                revision=1,
                thread_id=run_id,
                executions=0 if marks else 1,
                execution_id=None if marks else execution_id,
                execution_instance=None if marks else self.hub.instance(runner),
                reconcile=(
                    {"reason": "cluster_marked", "marks": [m["id"] for m in marks], "since": now}
                    if marks
                    else None
                ),
            )
            if row:
                await t.update("clusters", {"runner": runner, "name": cluster}, lock_run_id=run_id)
            else:
                await t.insert(
                    "clusters", runner=runner, name=cluster, owner=user, lock_run_id=run_id
                )
            body = {
                "command_id": command_id,
                "run_id": run_id,
                "accepted_revision": 1,
                "execution_id": None if marks else execution_id,
            }
            await t.insert(
                "commands",
                user=user,
                command_id=command_id,
                run_id=run_id,
                kind="create_run",
                **{"class": "advancing"},
                digest=dig,
                response=body,
                status="done" if marks else "accepted",
                at=now,
            )
            await t.append_event(
                run_id, "run_state", {"state": state, "revision": 1, "pending_id": None}
            )
        if not marks:
            self._launch(
                run_id,
                execution_id,
                self.hub.instance(runner),
                (user, command_id),
                self._start_work(run_id, runner, cluster, purpose, start),
            )
        return Accepted(body)

    def _validate_start(
        self, cluster: str, purpose: str | None, spec: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not re.fullmatch(NAME_PATTERN, cluster):
            raise RunError(422, "invalid_cluster", "use lowercase letters, digits, - and _")
        if (purpose is None) == (spec is None):
            raise RunError(422, "invalid_input", "give either a purpose or a spec")
        if purpose is not None:
            if not purpose.strip():
                raise RunError(422, "invalid_input", "the purpose is empty")
            if self.orch.router is None:
                raise RunError(422, "invalid_input", "no model is configured to read a purpose")
            return None
        try:
            return ClusterSpec.model_validate({**spec, "name": cluster}).model_dump()
        except ValidationError as e:
            raise RunError(422, "invalid_spec", str(e)) from None

    def _start_work(
        self,
        thread: str,
        runner: str,
        cluster: str,
        purpose: str | None,
        spec: dict[str, Any] | None,
    ) -> Work:
        if purpose is not None:
            return lambda sink: self.orch.start_from_purpose(
                thread, purpose, runner, cluster=cluster, on_event=sink
            )
        return lambda sink: self.orch.start(
            thread, ClusterSpec.model_validate(spec), runner, on_event=sink
        )

    # --- executions --------------------------------------------------------------------------

    def _launch(
        self,
        run_id: str,
        execution_id: str,
        instance: str | None,
        command: tuple[str, str],
        work: Work,
    ) -> None:
        task = asyncio.create_task(self._execute(run_id, execution_id, instance, command, work))
        self.tasks[execution_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(execution_id, None))

    async def _execute(
        self,
        run_id: str,
        execution_id: str,
        instance: str | None,
        command: tuple[str, str],
        work: Work,
    ) -> None:
        current.set(Execution(run_id, execution_id, instance))

        async def sink(event: dict[str, Any]) -> None:
            await self._record_event(run_id, execution_id, event)

        try:
            status = await work(sink)
        except asyncio.CancelledError:
            await self._interrupted(
                run_id, execution_id, command, "cancelled: the platform stopped"
            )
            raise
        except Exception as e:  # the Runner went away, a call timed out, an event failed to commit
            log.warning("execution %s of run %s failed: %r", execution_id, run_id, e)
            await self._interrupted(run_id, execution_id, command, f"{type(e).__name__}: {e}")
            return
        await self._finished(run_id, execution_id, command, status)

    async def _interrupted(
        self, run_id: str, execution_id: str, command: tuple[str, str], reason: str
    ) -> None:
        """Tx2': the execution ended without a result. What it did is unknown: the run goes
        to reconcile, and the cluster is marked if a mutating call has no recorded result."""
        async with self.store.tx() as t:
            run = await t.get_run(run_id)
            if run is None or run["execution_id"] != execution_id or run["state"] != "running":
                return
            now = self.clock()
            await self._mark_unconfirmed(t, run, execution_id, now)
            revision = await t.bump_revision(run_id)
            await t.update(
                "commands",
                {"user": command[0], "command_id": command[1]},
                status="interrupted",
                effect="unknown",
                reason=reason,
            )
            await t.update_run(
                run_id,
                state="reconcile",
                pending=None,
                reconcile={
                    "reason": reason,
                    "last_phase": run["phase"],
                    "since": now,
                    "execution_id": execution_id,
                },
            )
            await t.append_event(
                run_id,
                "run_state",
                {"state": "reconcile", "revision": revision, "pending_id": None},
                execution_id,
            )

    async def _mark_unconfirmed(
        self, t, run: dict[str, Any], execution_id: str | None, now: float
    ) -> None:
        """Mark the run's cluster for each mutating call execution_id sent without a result."""
        if execution_id is None:
            return
        marks = ", ".join("?" for _ in MUTATING)
        sent = await t.all(
            "SELECT method FROM dispatches WHERE execution_id = ? AND result_at IS NULL"
            f" AND method IN ({marks}) ORDER BY call_id",
            execution_id,
            *sorted(MUTATING),
        )
        if not sent:
            return
        cluster = await t.get_cluster(run["runner"], run["cluster"])
        mark = {
            "id": self.new_id(),
            "reason": ", ".join(d["method"] for d in sent) + " sent without a recorded result",
            "run_id": run["id"],
            "execution_id": execution_id,
            "at": now,
        }
        await t.update(
            "clusters",
            {"runner": run["runner"], "name": run["cluster"]},
            marks=[*cluster["marks"], mark],
        )

    async def _record_event(self, run_id: str, execution_id: str, event: dict[str, Any]) -> None:
        """Commit one application event with the projections it changes."""
        kind, payload = event["type"], event["payload"]
        async with self.store.tx() as t:
            seq = await t.append_event(run_id, kind, payload, execution_id, event["task_id"])
            if kind == "phase_started":
                await t.update_run(run_id, phase=payload["phase"])
            elif kind == "observation":
                at = payload["received_at"]
                await t.update_run(
                    run_id,
                    last_observation={**payload, "seq": seq},
                    last_observation_attempt={"at": at, "ok": True, "reason": None},
                )
            elif kind == "observation_failed":
                await t.update_run(
                    run_id,
                    last_observation_attempt={
                        "at": payload["received_at"],
                        "ok": False,
                        "reason": payload["reason"],
                    },
                )

    async def _finished(
        self, run_id: str, execution_id: str, command: tuple[str, str], status: Status
    ) -> None:
        """Tx2: record what the execution ended with, if the run is still its own."""
        async with self.store.tx() as t:
            run = await t.get_run(run_id)
            if run is None or run["execution_id"] != execution_id or run["state"] != "running":
                return  # a late result never overwrites what came after it
            revision = await t.bump_revision(run_id)
            now = self.clock()
            pending, values = None, {}
            if status.waiting is not None:
                kind = status.waiting["kind"]
                payload = {k: status.waiting.get(k) for k in PAYLOAD_FIELDS[kind]}
                if kind == "approval":
                    payload["previous_approved"] = await self._previous_approved(t, run_id)
                pending = {
                    "pending_id": self.new_id(),
                    "kind": kind,
                    "revision": revision,
                    "created_at": now,
                    "payload": payload,
                    "interrupt_id": status.interrupt_id,
                }
                state = "waiting"
            elif status.outcome in TERMINAL:
                state = TERMINAL[status.outcome]
                if state == "healthy":
                    values["verified_at"] = now
                await t.update(
                    "clusters",
                    {"runner": run["runner"], "name": run["cluster"], "lock_run_id": run_id},
                    lock_run_id=None,
                )
            else:
                state = "reconcile"
                values["reconcile"] = {
                    "reason": "no_result",
                    "last_phase": run["phase"],
                    "since": now,
                    "execution_id": execution_id,
                }
            await t.update_run(run_id, state=state, pending=pending, **values)
            await t.update(
                "commands", {"user": command[0], "command_id": command[1]}, status="done"
            )
            await t.append_event(
                run_id,
                "run_state",
                {
                    "state": state,
                    "revision": revision,
                    "pending_id": pending and pending["pending_id"],
                },
                execution_id,
            )

    @staticmethod
    async def _previous_approved(t, run_id: str) -> dict[str, Any] | None:
        """The last compose a person approved for this run, from the immutable records."""
        row = await t.one(
            "SELECT * FROM approvals WHERE run_id = ? AND kind = 'approval'"
            " AND json_extract(decision, '$.approve') = 1 ORDER BY at DESC LIMIT 1",
            run_id,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "at": row["at"],
            "compose": row["content"]["compose"],
            "digest": row["candidate_digest"],
        }
