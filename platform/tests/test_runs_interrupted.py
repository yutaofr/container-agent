"""Executions that end without a result: reconcile, cluster marks, shutdown, runner status."""

import asyncio

import pytest

from cagent.hub import RunnerUnavailable
from cagent.orchestrator import Status
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def cluster(store, name="demo"):
    async with store.read() as t:
        return await t.get_cluster("laptop-1", name)


async def approve_without_settling(h, run_id):
    snap = await h.service.snapshot("alice", run_id)
    body = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    return await h.service.submit("alice", run_id, body)


async def test_a_lost_runner_sends_the_run_to_reconcile_and_marks_the_cluster(store, catalog):
    class Lost(SimCluster):
        def _up(self, p):
            raise RunnerUnavailable("laptop-1 disconnected")

    h = Harness(store, Lost(), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile" and snap["pending"] is None
    assert snap["reconcile"]["reason"] == "RunnerUnavailable: laptop-1 disconnected"
    assert snap["reconcile"]["last_phase"] == "up"
    cmd = await h.service.command("alice", run_id, "a1")
    assert (cmd["status"], cmd["effect"]) == ("interrupted", "unknown")
    [mark] = (await cluster(store))["marks"]
    assert mark["reason"] == "up sent without a recorded result"
    with pytest.raises(RunError) as err:  # reconcile keeps the cluster's execution lock
        await h.create(command_id="c2")
    assert err.value.code == "cluster_busy"


async def test_a_call_refused_before_sending_marks_nothing(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    h.hub.connected_instance = "i-2"  # the runner restarted before the execution's first call
    await h.settle()
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"].startswith("InstanceChanged")
    assert (await cluster(store))["marks"] == []
    assert h.sim.calls == ["validate"]  # only the first execution's call


async def test_stopping_the_platform_interrupts_executions(store, catalog):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(SimCluster):
        async def call(self, runner_id, method, params, timeout=None):
            if method == "up":
                entered.set()
                await release.wait()
            return await super().call(runner_id, method, params, timeout)

    h = Harness(store, Slow(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    async with asyncio.timeout(5):
        await entered.wait()
    await h.service.close()
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"] == "cancelled: the platform stopped"
    assert (await cluster(store))["marks"][0]["reason"] == "up sent without a recorded result"
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2", cluster="other")
    assert (err.value.status, err.value.code) == (503, "shutting_down")


async def test_a_late_result_never_overwrites_later_state(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    before = await h.service.snapshot("alice", run_id)
    await h.service._finished(
        run_id, "an-old-execution", ("alice", "c1"), Status("healthy", None, None, [])
    )
    await h.service._interrupted(run_id, "an-old-execution", ("alice", "c1"), "late")
    assert await h.service.snapshot("alice", run_id) == before


async def test_runner_changes_are_recorded_and_told_to_active_runs(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    h.service.runner_changed("laptop-1", True, "i-1")
    h.service.runner_changed("laptop-1", False, None)
    await asyncio.gather(*h.service._background)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["runner_status"] == {"online": False, "instance_id": None, "changed_at": 1000.0}
    told = [e["payload"] for e in await h.events(run_id) if e["type"] == "runner"]
    assert told == [{"online": True, "instance_id": "i-1"}, {"online": False, "instance_id": None}]
