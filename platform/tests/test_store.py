"""The store: transactions, the bounded event log, and wake-ups after commits."""

import asyncio

import pytest

from cagent.store import Store, bounded


async def new_run(store: Store, run_id: str = "r1") -> None:
    async with store.tx() as t:
        await t.insert(
            "runs",
            id=run_id,
            owner="alice",
            runner="laptop-1",
            cluster="demo",
            input={"purpose": "x"},
            created_at=0.0,
            state="running",
        )


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"), keep_events=3)
    yield s
    await s.close()


async def test_a_run_survives_reopening(tmp_path):
    path = str(tmp_path / "cagent.db")
    s = await Store.open(path)
    await new_run(s)
    await s.close()
    s = await Store.open(path)
    async with s.read() as t:
        run = await t.get_run("r1")
    await s.close()
    assert run["input"] == {"purpose": "x"} and run["event_seq"] == 0
    assert run["min_event_seq"] == 1


async def test_a_failed_transaction_leaves_nothing(store):
    await new_run(store)
    with pytest.raises(RuntimeError):
        async with store.tx() as t:
            await t.update_run("r1", state="waiting")
            await t.append_event("r1", "run_state", {"state": "waiting"})
            raise RuntimeError("crash before commit")
    async with store.read() as t:
        run = await t.get_run("r1")
    assert run["state"] == "running" and run["event_seq"] == 0
    assert await store.events_after("r1", 0, 10, 10_000) == []


async def test_events_are_numbered_by_a_counter_and_trimmed(store):
    await new_run(store)
    for i in range(5):
        async with store.tx() as t:
            assert await t.append_event("r1", "phase_started", {"phase": f"p{i}"}) == i + 1
    async with store.read() as t:
        run = await t.get_run("r1")
    assert (run["event_seq"], run["min_event_seq"]) == (5, 3)
    page = await store.events_after("r1", 2, 10, 10_000)
    assert [e["seq"] for e in page] == [3, 4, 5]
    assert page[0]["payload"] == {"phase": "p2"}


async def test_a_cursor_outside_the_kept_events_must_resync(store):
    await new_run(store)
    for _ in range(5):
        async with store.tx() as t:
            await t.append_event("r1", "phase_started", {"phase": "p"})
    assert await store.events_after("r1", 1, 10, 10_000) is None  # 2 was trimmed
    assert await store.events_after("r1", 6, 10, 10_000) is None  # past the last event
    assert await store.events_after("r1", 5, 10, 10_000) == []
    assert await store.events_after("missing", 0, 10, 10_000) is None


async def test_a_page_stops_at_its_byte_limit(store):
    await new_run(store)
    for _ in range(3):
        async with store.tx() as t:
            await t.append_event("r1", "observation", {"line": "x" * 100})
    page = await store.events_after("r1", 0, 10, 150)
    assert [e["seq"] for e in page] == [1]  # one event always fits


def test_payloads_are_bounded_and_marked():
    long = bounded({"tail": ["y" * 5000]}, 64 * 1024)
    assert long["tail"][0].endswith("…[truncated]") and len(long["tail"][0]) < 2100
    assert bounded({"lines": ["z" * 1000] * 100}, 1000) == {"truncated": True, "bytes": 100_411}


async def test_subscribers_wake_only_after_the_commit(store):
    await new_run(store)
    seen = store.notifier.generation("r1")
    waiter = asyncio.create_task(store.notifier.wait("r1", seen, timeout=5))
    async with store.tx() as t:
        await t.append_event("r1", "run_state", {"state": "waiting"})
        await asyncio.sleep(0.05)
        assert not waiter.done()  # not before the commit
    assert await waiter


async def test_a_wake_up_between_read_and_wait_is_not_missed(store):
    await new_run(store)
    seen = store.notifier.generation("r1")  # taken before reading
    async with store.tx() as t:
        await t.append_event("r1", "run_state", {"state": "waiting"})
    assert await store.notifier.wait("r1", seen, timeout=0.01)  # returns at once
    assert not await store.notifier.wait("r1", store.notifier.generation("r1"), timeout=0.01)
