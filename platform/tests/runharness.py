"""A RunService over SimCluster, with a hub-shaped wrapper, for RunService tests."""

import asyncio
import itertools
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from cagent.dispatch import RecordingCaller
from cagent.hub import InstanceChanged, RunnerUnavailable, ToolResult
from cagent.orchestrator import Orchestrator, Settings
from cagent.runs import RunService
from cagent.store import Store

from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
USERS = {"alice": {"laptop-1"}, "bob": {"laptop-1"}, "carol": {"laptop-2"}}
REDIS = {"purpose": "a cache", "services": [], "middleware": [{"template": "redis"}]}


async def no_sleep(_seconds: float) -> None:
    return None


class SimHub:
    """The hub's interface over a SimCluster: one runner, an instance id, and status."""

    def __init__(self, sim: SimCluster, instance: str | None = "i-1"):
        self.sim, self.connected_instance = sim, instance
        self.active: dict[str, Any] | None = None
        self.queued: dict[str, int] = {}

    def instance(self, runner_id: str) -> str | None:
        return self.connected_instance

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        if self.connected_instance is None:
            raise RunnerUnavailable(f"{runner_id} is not connected")
        if instance is not None and instance != self.connected_instance:
            raise InstanceChanged("another process")
        if method == "status":
            report = {
                "instance_id": self.connected_instance,
                "active": self.active,
                "queued": self.queued,
            }
            return ToolResult(0, report)
        return await self.sim.call(runner_id, method, params, timeout)


class Harness:
    def __init__(self, store: Store, sim: SimCluster, catalog, saver=None, router=None):
        self.store, self.sim, self.catalog = store, sim, catalog
        self.hub = SimHub(sim)
        self.saver = saver or InMemorySaver()
        self.router = router
        self.ids = itertools.count(1)
        self.service = self.new_service()

    def new_service(self) -> RunService:
        """A RunService as a restarted platform builds it, over the same records."""
        orch = Orchestrator(
            RecordingCaller(self.hub, self.store),
            self.catalog,
            self.saver,
            FAST,
            sleep=no_sleep,
            clock=lambda: 1000.0,
            router=self.router,
        )
        return RunService(
            self.store,
            orch,
            self.hub,
            USERS,
            clock=lambda: 1000.0,
            new_id=lambda: f"id{next(self.ids)}",
        )

    async def settle(self) -> None:
        while self.service.tasks:
            await asyncio.gather(*self.service.tasks.values(), return_exceptions=True)

    async def create(self, user="alice", command_id="c1", cluster="demo", spec=REDIS, **kw):
        acc = await self.service.create_run(
            user, command_id, kw.pop("runner", "laptop-1"), cluster, spec=spec, **kw
        )
        await self.settle()
        return acc

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.store.events_after(run_id, 0, 10_000, 10_000_000)
