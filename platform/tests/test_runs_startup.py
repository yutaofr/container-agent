"""Startup reconciliation: what a restarted platform does with the runs it finds."""

import asyncio

import pytest

from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer
from .test_runs_interrupted import approve_without_settling, cluster


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def restart(h: Harness) -> None:
    """A new process over the same database and checkpoints; the old one simply stops."""
    h.service = h.new_service()
    await h.service.recover_on_startup()


async def test_a_run_waiting_for_input_waits_again_after_a_restart(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    before = await h.service.snapshot("alice", run_id)
    await restart(h)
    after = await h.service.snapshot("alice", run_id)
    assert after["state"] == "waiting" and after["pending"] == before["pending"]
    await answer(h, run_id, {"approve": True})
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_an_execution_cut_off_by_a_crash_goes_to_reconcile(store, catalog):
    entered, never = asyncio.Event(), asyncio.Event()

    class Stuck(SimCluster):
        async def call(self, runner_id, method, params, timeout=None):
            if method == "up":
                entered.set()
                await never.wait()  # the process dies here
            return await super().call(runner_id, method, params, timeout)

    h = Harness(store, Stuck(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    async with asyncio.timeout(5):
        await entered.wait()
    dead = h.service
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"] == {
        "reason": "platform restarted",
        "last_phase": "up",
        "since": 1000.0,
        "execution_id": snap["execution_id"],
    }
    cmd = await h.service.command("alice", run_id, "a1")
    assert (cmd["status"], cmd["effect"]) == ("interrupted", "unknown")
    assert (await cluster(store))["marks"][0]["reason"] == "up sent without a recorded result"
    assert h.sim.calls == ["validate", "validate"]  # the stuck up was never sent again
    # The dead process's execution, if it ever ended, could not touch the reconciled run.
    await dead.close()
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"


async def test_a_result_the_graph_reached_but_never_recorded_goes_to_reconcile(store, catalog):
    h = Harness(store, SimCluster(), catalog)

    async def crash_before_tx2(*_args):
        return None  # the graph finished, the process died before recording it

    run_id = (await h.create()).body["run_id"]
    h.service._finished = crash_before_tx2
    await answer(h, run_id, {"approve": True})
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"  # even though the checkpoint says healthy
    assert (await cluster(store))["marks"] == []  # every call got its result


async def test_a_pending_item_that_no_longer_matches_the_checkpoint_goes_to_reconcile(
    store, catalog
):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    async with store.tx() as t:
        run = await t.get_run(run_id)
        await t.update_run(run_id, pending={**run["pending"], "interrupt_id": "another"})
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"] == "pending item no longer matches the checkpoint"


async def test_an_unfinished_read_changes_no_run_and_runners_start_offline(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    h.service.runner_changed("laptop-1", True, "i-1")
    await asyncio.gather(*h.service._background)
    async with store.tx() as t:
        await t.insert(
            "commands",
            user="alice",
            command_id="r1",
            run_id=run_id,
            kind="refresh",
            **{"class": "read_only"},
            digest="d",
            response={},
            status="accepted",
            at=1.0,
        )
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "waiting" and snap["runner_status"]["online"] is False
    assert (await h.service.command("alice", run_id, "r1"))["status"] == "interrupted"
