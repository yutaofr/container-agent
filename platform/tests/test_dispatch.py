"""The dispatch log: written before a call is sent, completed after, pinned to one Runner."""

import asyncio

import pytest

from cagent.dispatch import Execution, RecordingCaller, current, unconfirmed
from cagent.hub import InstanceChanged, RunnerUnavailable, ToolResult
from cagent.store import Store

from .test_store import new_run


class FakeHub:
    """Answers calls; reports which instance is connected; can hang or refuse."""

    def __init__(self, store: Store, instance: str | None = "i-1"):
        self.store, self.connected = store, instance
        self.hang = False
        self.sent: list[tuple[str, str | None]] = []
        self.rows_at_send: list[dict] = []

    def instance(self, runner_id: str) -> str | None:
        return self.connected

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        if instance is not None and instance != self.connected:
            raise InstanceChanged("other process")
        async with self.store.read() as t:
            self.rows_at_send = await t.all("SELECT * FROM dispatches")
        self.sent.append((method, instance))
        if self.hang:
            await asyncio.sleep(3600)
        return ToolResult(0, {"ok": True})


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    await new_run(s)
    async with s.tx() as t:
        await t.update_run("r1", execution_id="e1")
    yield s
    await s.close()


def within(ex: Execution):
    current.set(ex)


async def test_a_call_is_logged_before_it_is_sent_and_completed_after(store):
    hub = FakeHub(store)
    within(Execution("r1", "e1", "i-1"))
    res = await RecordingCaller(hub, store).call("laptop-1", "up", {"name": "demo"})
    assert res.ok and hub.sent == [("up", "i-1")]
    [at_send] = hub.rows_at_send
    assert at_send["method"] == "up" and at_send["result_at"] is None
    assert await unconfirmed(store, "r1") == []


async def test_a_call_without_an_answer_stays_unconfirmed(store):
    hub = FakeHub(store)
    hub.hang = True
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await RecordingCaller(hub, store).call("laptop-1", "build", {})
    assert [d["method"] for d in await unconfirmed(store, "r1")] == ["build"]


async def test_reads_never_count_as_unconfirmed(store):
    hub = FakeHub(store)
    hub.hang = True
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await RecordingCaller(hub, store).call("laptop-1", "snapshot", {})
    assert await unconfirmed(store, "r1") == []


async def test_a_call_pinned_to_another_instance_is_refused_and_not_logged(store):
    hub = FakeHub(store, instance="i-2")
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(InstanceChanged):
        await RecordingCaller(hub, store).call("laptop-1", "up", {})
    assert hub.sent == []
    async with store.read() as t:
        assert await t.all("SELECT * FROM dispatches") == []


async def test_an_unpinned_execution_pins_the_first_instance(store):
    hub = FakeHub(store, instance="i-7")
    ex = Execution("r1", "e1", None)
    within(ex)
    await RecordingCaller(hub, store).call("laptop-1", "validate", {})
    assert ex.instance == "i-7" and hub.sent == [("validate", "i-7")]
    async with store.read() as t:
        assert (await t.get_run("r1"))["execution_instance"] == "i-7"


async def test_an_unpinned_execution_needs_a_connected_runner(store):
    within(Execution("r1", "e1", None))
    with pytest.raises(RunnerUnavailable):
        await RecordingCaller(FakeHub(store, instance=None), store).call("laptop-1", "up", {})


async def test_calls_outside_an_execution_pass_straight_through(store):
    hub = FakeHub(store)
    current.set(None)
    assert (await RecordingCaller(hub, store).call("laptop-1", "status", {})).ok
    assert hub.sent == [("status", None)]
    async with store.read() as t:
        assert await t.all("SELECT * FROM dispatches") == []
