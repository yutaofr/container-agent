"""Recovery: refresh, review a plan, approve or decline it, abandon; and clearing marks."""

import pytest

from cagent.hub import RunnerUnavailable
from cagent.recovery import INSTANCE_CHANGED, PREVIOUS_UNCONFIRMED
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer
from .test_runs_interrupted import cluster


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


class LosesUpOnce(SimCluster):
    """The first up starts the cluster, then the answer is lost with the connection."""

    lost = False

    def _up(self, p):
        res = super()._up(p)
        if not self.lost:
            self.lost = True
            raise RunnerUnavailable("laptop-1 disconnected")
        return res


async def interrupted_run(store, catalog, sim=None):
    """A run whose up was sent and never answered: reconcile, cluster marked."""
    h = Harness(store, sim or LosesUpOnce(), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    return h, run_id


async def command(h, run_id, action, command_id, **extra):
    snap = await h.service.snapshot("alice", run_id)
    body = {"command_id": command_id, "expected_revision": snap["revision"], "action": action}
    acc = await h.service.submit("alice", run_id, {**body, **extra})
    await h.settle()
    return acc


async def test_refresh_review_and_approve_recover_the_run_and_clear_its_mark(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    before = await h.service.snapshot("alice", run_id)
    await command(h, run_id, "refresh", "f1")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["revision"] == before["revision"]  # a read changes no version
    assert snap["last_check"] == {
        "at": 1000.0,
        "instance_id": "i-1",
        "idle": True,
        "same_instance": True,
    }
    assert snap["last_observation"]["services"][0]["service"] == "redis"

    await command(h, run_id, "review_recovery", "v1")
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    plan = pending["payload"]
    assert pending["kind"] == "recovery" and plan["mode"] == "resume_execution"
    assert plan["build"]["decision"] == "skip_all" and plan["required"] == []
    assert plan["steps"] == ["validate", "build (skipped)", "up", "watch", "stabilize"]
    assert [r["id"] for r in plan["risks"]] == [f"mark:{plan['marks'][0]}"]

    await answer(h, run_id, {"approve": True}, command_id="r1")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "healthy"
    assert (await cluster(store))["marks"] == []
    assert h.sim.calls.count("up") == 2 and "build" not in h.sim.calls
    async with store.read() as t:
        kinds = [a["kind"] for a in await t.all("SELECT kind FROM approvals ORDER BY at")]
    assert kinds == ["approval", "recovery"]


async def test_a_busy_or_absent_runner_blocks_a_plan(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    h.hub.active = {"call_id": 9, "method": "up", "cluster": "demo", "started_at": "t"}
    await command(h, run_id, "refresh", "f1")
    assert (await h.service.snapshot("alice", run_id))["last_check"]["idle"] is False
    with pytest.raises(RunError) as err:
        await command(h, run_id, "review_recovery", "v1")
    assert err.value.code == "check_needed"
    h.hub.connected_instance = None
    with pytest.raises(RunError) as err:
        await command(h, run_id, "refresh", "f2")
    assert err.value.code == "runner_offline"


async def test_a_restarted_runner_needs_acknowledging_and_pins_the_new_instance(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    h.hub.connected_instance = "i-2"
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    plan = (await h.service.snapshot("alice", run_id))["pending"]["payload"]
    assert plan["required"] == [INSTANCE_CHANGED]
    with pytest.raises(RunError) as err:
        await answer(h, run_id, {"approve": True}, command_id="r1")
    assert err.value.status == 422
    await answer(h, run_id, {"approve": True, "acknowledged": [INSTANCE_CHANGED]}, command_id="r2")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "healthy"
    async with store.read() as t:
        assert (await t.get_run(run_id))["execution_instance"] == "i-2"


async def test_a_plan_is_withdrawn_when_its_facts_change(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    stale = await h.service.snapshot("alice", run_id)
    h.hub.connected_instance = "i-2"
    await command(h, run_id, "refresh", "f2")  # finds another instance
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile" and snap["revision"] == stale["revision"] + 1
    with pytest.raises(RunError) as err:
        body = {
            "command_id": "r1",
            "expected_revision": stale["revision"],
            "action": "answer",
            "pending_id": stale["pending"]["pending_id"],
            "decision": {"approve": True},
        }
        await h.service.submit("alice", run_id, body)
    assert err.value.code == "not_waiting"


async def test_an_approval_rechecks_the_runner_first(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    h.hub.connected_instance = "i-2"  # restarted after the plan, before the approval
    with pytest.raises(RunError) as err:
        await answer(h, run_id, {"approve": True}, command_id="r1")
    assert (err.value.status, err.value.code) == (409, "plan_stale")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    assert h.sim.calls.count("up") == 1


async def test_declining_needs_no_runner_and_keeps_the_lock(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    h.hub.connected_instance = None
    await answer(h, run_id, {"approve": False}, command_id="r1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2")
    assert err.value.code == "cluster_busy"


async def test_abandoning_keeps_the_mark_for_the_next_run(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "abandon", "x1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "abandoned"
    [mark] = (await cluster(store))["marks"]

    # A new run on the marked cluster inherits the mark and starts in reconcile.
    new = (await h.create(command_id="c2")).body["run_id"]
    snap = await h.service.snapshot("alice", new)
    assert snap["state"] == "reconcile" and snap["reconcile"]["reason"] == "cluster_marked"
    await command(h, new, "refresh", "f1")
    await command(h, new, "review_recovery", "v1")
    plan = (await h.service.snapshot("alice", new))["pending"]["payload"]
    assert plan["mode"] == "continue_cluster" and plan["required"] == [PREVIOUS_UNCONFIRMED]
    await answer(h, new, {"approve": True, "acknowledged": [PREVIOUS_UNCONFIRMED]}, command_id="r1")

    # It still needs the normal approval, and waiting there clears nothing.
    assert (await h.service.snapshot("alice", new))["pending"]["kind"] == "approval"
    assert (await cluster(store))["marks"] == [mark]
    await answer(h, new, {"approve": True}, command_id="r2")
    assert (await h.service.snapshot("alice", new))["state"] == "healthy"
    assert (await cluster(store))["marks"] == []


async def test_a_recovery_that_fails_again_keeps_the_marks(store, catalog):
    class AlwaysLost(SimCluster):
        def _up(self, p):
            super()._up(p)
            raise RunnerUnavailable("gone again")

    h, run_id = await interrupted_run(store, catalog, AlwaysLost())
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    await answer(h, run_id, {"approve": True}, command_id="r1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    assert len((await cluster(store))["marks"]) == 2  # the old one and the new one


async def test_a_run_without_a_compose_cannot_be_resumed(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")

    async def no_compose(_thread):
        return {"compose": None}, "cp"

    h.service.orch.checkpoint = no_compose
    with pytest.raises(RunError) as err:
        await command(h, run_id, "review_recovery", "v1")
    assert err.value.code == "no_compose"
