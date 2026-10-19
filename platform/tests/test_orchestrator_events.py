"""Application events: whitelisted phase events and observations from the graph's streams."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.hub import RunnerUnavailable, ToolResult
from cagent.orchestrator import Orchestrator, Settings

from .conftest import spec_with
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
MIDDLEWARE_ONLY = {"services": [], "middleware": [{"template": "redis"}]}
PHASE_KEYS = {"phase", "pending_kind", "reason"}


async def no_sleep(_seconds: float) -> None:
    return None


def orchestrator(sim, catalog) -> Orchestrator:
    return Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep, clock=lambda: 42.0)


class Recorder:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)

    def phases(self) -> list[tuple[str, str]]:
        return [(e["type"], e["payload"]["phase"]) for e in self.events if "phase" in e["payload"]]


async def test_a_run_reports_phases_up_to_its_approval(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    assert rec.phases() == [
        ("phase_started", "generate"),
        ("phase_completed", "generate"),
        ("phase_started", "validate"),
        ("phase_completed", "validate"),
        ("phase_started", "approve"),
        ("phase_interrupted", "approve"),
    ]
    assert rec.events[-1]["payload"]["pending_kind"] == "approval"
    snap = await o.graph.aget_state(o._config("t1"))
    assert status.interrupt_id == snap.interrupts[0].id


async def test_events_carry_no_raw_task_data(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    await o.resume("t1", {"approve": True}, on_event=rec)
    for e in rec.events:
        assert set(e) == {"type", "task_id", "payload"}
        if e["type"].startswith("phase_"):
            assert set(e["payload"]) <= PHASE_KEYS
        else:
            assert set(e["payload"]) in ({"received_at", "services"}, {"received_at", "reason"})


async def test_watching_reports_each_snapshot_as_an_observation(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(starting_snapshots=1), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    status = await o.resume("t1", {"approve": True}, on_event=rec)
    assert status.outcome == "healthy"
    observations = [e["payload"] for e in rec.events if e["type"] == "observation"]
    assert len(observations) == 3  # starting, settled, after the stability window
    assert observations[0]["received_at"] == 42.0
    assert observations[0]["services"][0] == {
        "service": "redis",
        "role": None,
        "state": "running",
        "health": "starting",
        "restart_count": 0,
        "exit_code": 0,
        "errors": [],
        "tail": [],
    }
    assert ("phase_completed", "done") in rec.phases()


async def test_a_failed_snapshot_is_reported_not_shown_as_empty(catalog):
    class Flaky(SimCluster):
        failed = False

        def _snapshot(self, p):
            if not self.failed:
                self.failed = True
                return ToolResult(2, None, "docker: cannot connect")
            return super()._snapshot(p)

    rec = Recorder()
    o = orchestrator(Flaky(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    await o.resume("t1", {"approve": True}, on_event=rec)
    first = next(e for e in rec.events if e["type"].startswith("observation"))
    assert first["type"] == "observation_failed"
    assert first["payload"]["reason"] == "snapshot exited 2: docker: cannot connect"


async def test_a_node_error_reports_the_phase_failed_and_propagates(catalog):
    class Gone(SimCluster):
        def _up(self, p):
            raise RunnerUnavailable("laptop-1 disconnected")

    rec = Recorder()
    o = orchestrator(Gone(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    with pytest.raises(RunnerUnavailable):
        await o.resume("t1", {"approve": True}, on_event=rec)
    assert rec.events[-1] == {
        "type": "phase_failed",
        "task_id": rec.events[-1]["task_id"],
        "payload": {"phase": "up", "reason": "RunnerUnavailable"},
    }


async def test_a_resumed_node_starts_again_with_the_same_task_id(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    interrupted, n = rec.events[-1], len(rec.events)
    await o.resume("t1", {"approve": True}, on_event=rec)
    restarted = rec.events[n]
    assert restarted["type"] == "phase_started" and restarted["payload"]["phase"] == "approve"
    assert restarted["task_id"] == interrupted["task_id"]
