"""Patch previews: each operation's effect in order, as the person approving it sees it."""

import pytest

from cagent.orchestrator import Status
from cagent.patches import preview
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer

COMPOSE = {
    "services": {
        "app": {
            "image": "app:1",
            "environment": {"MODE": "dev", "EMPTY": None},
            "ports": ["8080:8080", "9090:9090"],
        }
    }
}


def step(op, path, before, after, insert=False):
    return {"op": op, "path": path, "before": before, "after": after, "insert": insert}


def present(v):
    return {"present": True, "value": v}


ABSENT = {"present": False}


def test_operations_on_one_path_are_shown_in_order():
    patch = [
        {"op": "replace", "path": "/services/app/environment/MODE", "value": "test"},
        {"op": "replace", "path": "/services/app/environment/MODE", "value": "prod"},
    ]
    assert preview(COMPOSE, patch) == [
        step("replace", "/services/app/environment/MODE", present("dev"), present("test")),
        step("replace", "/services/app/environment/MODE", present("test"), present("prod")),
    ]


def test_absent_differs_from_null():
    patch = [
        {"op": "add", "path": "/services/app/environment/NEW", "value": "x"},
        {"op": "replace", "path": "/services/app/environment/EMPTY", "value": "y"},
    ]
    first, second = preview(COMPOSE, patch)
    assert first["before"] == ABSENT and not first["insert"]
    assert second["before"] == present(None)


def test_array_inserts_and_removals_are_not_mistaken_for_replacements():
    patch = [
        {"op": "add", "path": "/services/app/ports/0", "value": "7070:7070"},
        {"op": "add", "path": "/services/app/ports/-", "value": "6060:6060"},
        {"op": "remove", "path": "/services/app/ports/1"},
    ]
    assert preview(COMPOSE, patch) == [
        step("add", "/services/app/ports/0", ABSENT, present("7070:7070"), insert=True),
        step("add", "/services/app/ports/-", ABSENT, present("6060:6060"), insert=True),
        step("remove", "/services/app/ports/1", present("8080:8080"), ABSENT),
    ]


def test_a_test_operation_is_a_check_and_a_bad_patch_has_no_preview():
    check = {"op": "test", "path": "/services/app/image", "value": "app:1"}
    assert preview(COMPOSE, [check]) == [
        step("test", "/services/app/image", present("app:1"), present("app:1"))
    ]
    assert preview(COMPOSE, [{**check, "value": "app:2"}]) is None
    assert preview(COMPOSE, [{"op": "remove", "path": "/services/nope"}]) is None
    assert COMPOSE["services"]["app"]["ports"] == ["8080:8080", "9090:9090"]  # untouched


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def patch_waiting(h, patch):
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})  # healthy, with an observation
    async with h.store.tx() as t:
        await t.update_run(run_id, state="running", pending=None, execution_id="x")
    waiting = {
        "kind": "patch_approval",
        "signature": "s",
        "tier": "large",
        "root_cause": "r",
        "patch": patch,
        "reasons": ["undeclared key"],
    }
    compose = {"services": {"redis": {"image": "redis:7.4"}}}
    await h.service._finished(run_id, "x", ("alice", "a1"), Status(None, waiting, compose, []))
    return run_id


async def test_the_preview_and_the_evidence_are_fixed_when_the_item_is_published(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    patch = [{"op": "replace", "path": "/services/redis/image", "value": "redis:7.2"}]
    run_id = await patch_waiting(h, patch)
    payload = (await h.service.snapshot("alice", run_id))["pending"]["payload"]
    assert payload["preview"] == [
        step("replace", "/services/redis/image", present("redis:7.4"), present("redis:7.2"))
    ]
    evidence = payload["evidence"]
    assert evidence["services"][0]["service"] == "redis" and evidence["seq"] > 0
    rec = await h.service.record("alice", run_id, 0, limit=1000)
    published = [e for e in rec["entries"] if e.get("kind") == "patch_approval"][0]
    assert published["payload"]["evidence"] == evidence  # the same, not today's


async def test_a_patch_without_a_preview_cannot_be_approved(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = await patch_waiting(h, [{"op": "remove", "path": "/services/nope"}])
    assert (await h.service.snapshot("alice", run_id))["pending"]["payload"]["preview"] is None
    with pytest.raises(RunError) as err:
        await answer(h, run_id, {"approve": True}, command_id="p1")
    assert err.value.status == 422
    await answer(h, run_id, {"approve": False}, command_id="p2")  # rejecting still works
