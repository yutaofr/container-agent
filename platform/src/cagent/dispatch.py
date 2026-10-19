"""Every tool call an execution makes is written down before it is sent.

After a crash the platform cannot ask a half-finished execution what it did. The dispatch
log answers instead: a mutating call that was sent but has no recorded result may have
changed the cluster, so the cluster needs reconciling; a cluster with no such call does not.
The log also pins each execution to one Runner process, so a call is never sent to a Runner
that restarted in between.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from cagent.hub import InstanceChanged, RunnerHub, RunnerUnavailable, ToolResult
from cagent.store import Store

# Calls that may change the user's machine. inspect, snapshot, verify, and status only read.
MUTATING = frozenset({"validate", "build", "up", "down"})


@dataclass
class Execution:
    """The execution the current task belongs to."""

    run_id: str
    execution_id: str
    instance: str | None  # the Runner process its calls go to; None pins the first one seen


current: ContextVar[Execution | None] = ContextVar("execution", default=None)


class RecordingCaller:
    """The orchestrator's ToolCaller: the hub, with a dispatch log and instance pinning."""

    def __init__(self, hub: RunnerHub, store: Store):
        self.hub, self.store = hub, store

    async def call(
        self, runner_id: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> ToolResult:
        ex = current.get()
        if ex is None:
            return await self.hub.call(runner_id, method, params, timeout)
        if ex.instance is None:
            ex.instance = self.hub.instance(runner_id)
            if ex.instance is None:
                raise RunnerUnavailable(f"{runner_id} is not connected")
            async with self.store.tx() as t:
                await t.update(
                    "runs",
                    {"id": ex.run_id, "execution_id": ex.execution_id},
                    execution_instance=ex.instance,
                )
        async with self.store.tx() as t:
            row = await t.one(
                "INSERT INTO dispatches (run_id, execution_id, method, instance, sent_at)"
                " VALUES (?, ?, ?, ?, ?) RETURNING call_id",
                ex.run_id,
                ex.execution_id,
                method,
                ex.instance,
                self.store.clock(),
            )
        call_id = row["call_id"]
        try:
            res = await self.hub.call(runner_id, method, params, timeout, instance=ex.instance)
        except InstanceChanged:
            # The hub refuses before sending anything, so nothing was dispatched.
            async with self.store.tx() as t:
                await t.run("DELETE FROM dispatches WHERE call_id = ?", call_id)
            raise
        async with self.store.tx() as t:
            await t.update(
                "dispatches",
                {"call_id": call_id},
                result_at=self.store.clock(),
                exit_code=res.exit_code,
            )
        return res


async def unconfirmed(store: Store, run_id: str) -> list[dict[str, Any]]:
    """Mutating calls of run_id that were sent and never got a recorded result."""
    marks = ", ".join("?" for _ in MUTATING)
    async with store.read() as t:
        return await t.all(
            "SELECT * FROM dispatches WHERE run_id = ? AND result_at IS NULL"
            f" AND method IN ({marks}) ORDER BY call_id",
            run_id,
            *sorted(MUTATING),
        )
