"""Answering pending items: versions, idempotency, strict decisions, approval records."""

import pytest

from cagent.recovery import compose_digest
from cagent.runs import RunError, check_decision
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster

ORDERS_WITHOUT_ENV = {
    "purpose": "orders",
    "services": [{"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}],
    "middleware": [{"template": "mysql"}, {"template": "kafka"}],
}
FINDING = {"service": "redis", "rule": "cap-add", "detail": "x"}


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def answer(h, run_id, decision, command_id="a1", user="alice", **overrides):
    snap = await h.service.snapshot(user, run_id)
    body = {
        "command_id": command_id,
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": decision,
        **overrides,
    }
    acc = await h.service.submit(user, run_id, body)
    await h.settle()
    return acc, body


async def test_an_approved_run_becomes_healthy_and_frees_its_cluster(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    acc, _ = await answer(h, run_id, {"approve": True})
    assert acc.body["accepted_revision"] == 3 and acc.body["execution_id"]
    snap = await h.service.snapshot("alice", run_id)
    assert (snap["state"], snap["verified_at"], snap["pending"]) == ("healthy", 1000.0, None)
    assert snap["last_observation"]["services"][0]["service"] == "redis"
    async with store.read() as t:
        [record] = await t.all("SELECT * FROM approvals")
    assert record["decision"] == {"approve": True, "allow": []}
    assert record["candidate_digest"] == compose_digest(h.sim.compose)
    await h.create(command_id="c2")  # the healthy run released the cluster


async def test_a_repeated_answer_runs_once(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    first, body = await answer(h, run_id, {"approve": True})
    again = await h.service.submit("alice", run_id, body)
    assert again.replay and again.body == first.body
    assert h.sim.calls.count("up") == 1
    with pytest.raises(RunError) as err:  # same pending item, a new command id
        await h.service.submit("alice", run_id, {**body, "command_id": "a2"})
    assert (err.value.status, err.value.code) == (409, "not_waiting")
    assert err.value.snapshot["state"] == "healthy"
    with pytest.raises(RunError) as err:  # same command id, another decision
        await h.service.submit("alice", run_id, {**body, "decision": {"approve": False}})
    assert err.value.code == "command_id_reused"


async def test_a_replay_still_needs_access(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    _, body = await answer(h, run_id, {"approve": True})
    h.service.users = {"alice": set()}
    with pytest.raises(RunError) as err:
        await h.service.submit("alice", run_id, body)
    assert err.value.status == 404


async def test_stale_and_malformed_commands_consume_nothing(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    for overrides, code in [
        ({"expected_revision": 1}, "stale"),
        ({"pending_id": "old"}, "stale"),
    ]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, {"approve": True}, **overrides)
        assert (err.value.status, err.value.code) == (409, code)
        assert err.value.snapshot["revision"] == 2
    for decision in [{"approve": "yes"}, {"approve": True, "extra": 1}, {}]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, decision)
        assert (err.value.status, err.value.code) == (422, "invalid_decision")
    with pytest.raises(RunError) as err:
        await h.service.submit("alice", run_id, {"command_id": "x", "action": "dance"})
    assert err.value.code == "invalid_command"
    assert (await h.service.snapshot("alice", run_id))["state"] == "waiting"
    await answer(h, run_id, {"approve": True})  # a valid answer still works
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_allow_names_only_this_candidates_findings(store, catalog):
    h = Harness(store, SimCluster(findings=[FINDING]), catalog)
    run_id = (await h.create()).body["run_id"]
    for decision in [
        {"approve": True, "allow": ["redis:privileged"]},
        {"approve": True, "allow": ["cap-add"]},
        {"approve": True, "allow": ["redis:cap-add", "redis:cap-add"]},
        {"approve": False, "allow": ["redis:cap-add"]},
    ]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, decision)
        assert err.value.status == 422
    await answer(h, run_id, {"approve": True}, command_id="a1")  # the finding stays uncovered
    snap = await h.service.snapshot("alice", run_id)
    previous = snap["pending"]["payload"]["previous_approved"]
    assert previous["compose"] == snap["pending"]["payload"]["compose"]
    await answer(h, run_id, {"approve": True, "allow": ["redis:cap-add"]}, command_id="a2")
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_questions_take_defaults_answers_or_an_abort(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create(spec=ORDERS_WITHOUT_ENV)).body["run_id"]
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    assert pending["kind"] == "question"
    assert pending["payload"]["gap"]["key"] == "SPRING_PROFILES_ACTIVE"
    with pytest.raises(RunError):
        await answer(h, run_id, {"answer": "dev", "action": "abort"})
    await answer(h, run_id, {"answer": "dev"})
    assert (await h.service.snapshot("alice", run_id))["pending"]["kind"] == "approval"

    run2 = (await h.create(command_id="c2", cluster="other", spec=ORDERS_WITHOUT_ENV)).body
    await answer(h, run2["run_id"], {"action": "abort"}, command_id="a9")
    assert (await h.service.snapshot("alice", run2["run_id"]))["state"] == "rejected"


async def test_an_escalation_ends_or_retries(store, catalog):
    h = Harness(store, SimCluster(crash={"redis": 3}), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    assert pending["kind"] == "escalation" and pending["payload"]["reason"] == "no_rule"
    await answer(h, run_id, {"action": "retry"}, command_id="a2")
    assert (await h.service.snapshot("alice", run_id))["pending"]["kind"] == "escalation"
    await answer(h, run_id, {"action": "abort"}, command_id="a3")
    assert (await h.service.snapshot("alice", run_id))["state"] == "escalated"
    await h.create(command_id="c2")  # an ended run released the cluster


def test_decisions_are_strict_for_every_kind():
    assert check_decision("question", {}, {"answer": None}) == {"answer": None}
    assert check_decision("save_descriptors", {}, {"saved": True}) == {"saved": True}
    assert check_decision("patch_approval", {}, {"approve": False}) == {"approve": False}
    plan = {"required": ["init_partial"]}
    assert check_decision("recovery", plan, {"approve": True, "acknowledged": ["init_partial"]})
    assert check_decision("recovery", plan, {"approve": False})
    for kind, payload, decision in [
        ("question", {}, {}),
        ("save_descriptors", {}, {"saved": False}),
        ("patch_approval", {}, {"approve": 1}),
        ("escalation", {}, {"action": "skip"}),
        ("recovery", plan, {"approve": True}),
        ("recovery", plan, {"approve": True, "acknowledged": ["init_partial", "x"]}),
        ("recovery", plan, {"approve": False, "acknowledged": ["init_partial"]}),
    ]:
        with pytest.raises(RunError) as err:
            check_decision(kind, payload, decision)
        assert err.value.status == 422, (kind, decision)
