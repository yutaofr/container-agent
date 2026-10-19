"""RunService: creating runs, executing them in the background, and reading them back."""

import pytest

from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster


@pytest.fixture
async def h(tmp_path, catalog):
    store = await Store.open(str(tmp_path / "cagent.db"))
    yield Harness(store, SimCluster(), catalog)
    await store.close()


async def test_a_new_run_executes_until_its_first_approval(h):
    acc = await h.create()
    run_id = acc.body["run_id"]
    assert acc.body == {
        "command_id": "c1",
        "run_id": run_id,
        "accepted_revision": 1,
        "execution_id": acc.body["execution_id"],
    }
    snap = await h.service.snapshot("alice", run_id)
    assert (snap["state"], snap["revision"], snap["phase"]) == ("waiting", 2, "approve")
    pending = snap["pending"]
    assert pending["kind"] == "approval" and pending["revision"] == 2
    assert set(pending["payload"]) == {"compose", "findings", "allow", "fixes", "previous_approved"}
    assert pending["payload"]["previous_approved"] is None
    assert snap["watching"] is False and snap["runner_status"]["online"] is False
    states = [e["payload"]["state"] for e in await h.events(run_id) if e["type"] == "run_state"]
    assert states == ["running", "waiting"]
    assert (await h.service.command("alice", run_id, "c1"))["status"] == "done"


async def test_creating_twice_with_one_command_id_creates_one_run(h):
    first = await h.create()
    again = await h.create()
    assert again.replay and again.body == first.body
    assert [r["id"] for r in await h.service.list_runs("alice")] == [first.body["run_id"]]
    with pytest.raises(RunError) as err:
        await h.create(cluster="other")
    assert (err.value.status, err.value.code) == (409, "command_id_reused")


async def test_runs_are_visible_only_to_their_owner_with_the_runner(h):
    run_id = (await h.create()).body["run_id"]
    for user in ("bob", "carol", "nobody"):
        with pytest.raises(RunError) as err:
            await h.service.snapshot(user, run_id)
        assert err.value.status == 404
    assert await h.service.list_runs("bob") == []
    with pytest.raises(RunError) as err:
        await h.create(user="carol", runner="laptop-1")
    assert err.value.status == 404
    h.service.users = {"alice": set()}  # the configuration took the runner away
    with pytest.raises(RunError):
        await h.service.snapshot("alice", run_id)


async def test_a_cluster_has_one_owner_and_one_active_run(h):
    await h.create()
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2")
    assert (err.value.status, err.value.code) == (409, "cluster_busy")
    with pytest.raises(RunError) as err:
        await h.create(user="bob", command_id="c3")
    assert (err.value.status, err.value.code) == (409, "cluster_name_taken")
    assert "alice" not in err.value.detail
    await h.create(command_id="c4", cluster="second")  # another name is free


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"cluster": "Bad Name"}, "invalid_cluster"),
        ({"purpose": "a cache"}, "invalid_input"),  # both a purpose and a spec
        ({"spec": None, "purpose": "a cache"}, "invalid_input"),  # no model configured
        ({"spec": None, "purpose": " "}, "invalid_input"),
        ({"spec": {"purpose": "x", "services": "no"}}, "invalid_spec"),
    ],
)
async def test_invalid_input_is_refused(h, kw, code):
    with pytest.raises(RunError) as err:
        await h.create(**kw)
    assert (err.value.status, err.value.code) == (422, code)
    assert await h.service.list_runs("alice") == []
