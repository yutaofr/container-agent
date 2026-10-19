"""Event streams: paging from the store, resync on gaps, wake-ups, heartbeats, session ends.

Each race is arranged step by step with the generator itself, not with sleeps."""

import asyncio
import json

import pytest

from cagent.auth import Sessions
from cagent.events import BadCursor, StreamLimits, control, event_stream, parse_cursor
from cagent.store import Store

from .test_store import new_run

LIMITS = StreamLimits(page_events=2, heartbeat_s=30)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"), keep_events=4)
    await new_run(s)
    yield s
    await s.close()


@pytest.fixture
def auth():
    clock = Clock()
    sessions = Sessions({"alice": "a" * 32}, ttl_s=1000, idle_ttl_s=100, clock=clock)
    pre = sessions.new_pre("c")
    return sessions, sessions.login(pre.id, pre.csrf, "a" * 32, "c"), clock


async def append(store, n=1, type="phase_started"):
    for _ in range(n):
        async with store.tx() as t:
            await t.append_event("r1", type, {"phase": "p"})


def seq_of(chunk: str) -> int:
    return json.loads(chunk.split("data: ", 1)[1])["seq"]


async def next_chunk(stream, timeout=2.0) -> str:
    async with asyncio.timeout(timeout):
        return await anext(stream)


def stream(store, auth, cursor=0, limits=LIMITS):
    sessions, session, _ = auth
    return event_stream(store, sessions, session, "r1", cursor, limits)


def test_cursors_are_non_negative_integers_and_last_event_id_wins():
    assert parse_cursor(None, None) == 0
    assert parse_cursor("7", "3") == 7 and parse_cursor(None, "3") == 3
    for bad in ["-1", "x", "1.5", ""]:
        with pytest.raises(BadCursor):
            parse_cursor(bad, None)


async def test_events_arrive_in_order_with_their_ids(store, auth):
    await append(store, 3)
    s = stream(store, auth)
    chunks = [await next_chunk(s) for _ in range(3)]
    assert [seq_of(c) for c in chunks] == [1, 2, 3]
    assert chunks[0].startswith("id: 1\nevent: phase_started\ndata: ")


async def test_a_gap_while_paging_ends_with_resync(store, auth):
    await append(store, 3)
    s = stream(store, auth)
    assert seq_of(await next_chunk(s)) == 1
    assert seq_of(await next_chunk(s)) == 2  # first page done
    await append(store, 4)  # keep 4: events 3 and 4 are trimmed before the next page
    assert await next_chunk(s) == control("resync")
    with pytest.raises(StopAsyncIteration):
        await anext(s)


@pytest.mark.parametrize("cursor", [1, 7])  # before the kept events, past the last one
async def test_a_cursor_outside_the_kept_events_resyncs_at_once(store, auth, cursor):
    await append(store, 6)  # kept: 3..6
    assert await next_chunk(stream(store, auth, cursor)) == control("resync")


async def test_a_commit_right_after_an_empty_read_is_not_missed(store, auth):
    class Racing(Store):
        """Commits an event right after a read finds nothing, before the reader waits."""

        raced = False

        async def events_after(self, *args, **kw):
            page = await super().events_after(*args, **kw)
            if page == [] and not self.raced:
                self.raced = True
                await append(self)
            return page

    racing = Racing(store.db, keep_events=4)
    racing.notifier = store.notifier
    s = event_stream(racing, auth[0], auth[1], "r1", 0, LIMITS)  # heartbeat 30 s
    assert seq_of(await next_chunk(s, timeout=2)) == 1


async def test_an_idle_stream_sends_heartbeats_without_keeping_the_session_alive(store, auth):
    sessions, session, _ = auth
    s = stream(store, auth, limits=StreamLimits(heartbeat_s=0.05))
    assert await next_chunk(s) == ": heartbeat\n\n"
    assert session.last_seen == 0.0


async def test_logout_ends_a_stream_in_the_middle_of_a_page(store, auth):
    sessions, session, _ = auth
    await append(store, 2)
    s = stream(store, auth)
    assert seq_of(await next_chunk(s)) == 1
    sessions.logout(session.id)
    assert await next_chunk(s) == control("session_expired")  # event 2 is never sent


async def test_logout_wakes_a_waiting_stream_at_once(store, auth):
    sessions, session, _ = auth
    s = stream(store, auth)  # heartbeat 30 s
    waiting = asyncio.ensure_future(anext(s))
    await asyncio.sleep(0.05)
    sessions.logout(session.id)
    async with asyncio.timeout(1):
        assert await waiting == control("session_expired")


async def test_an_expired_session_ends_the_stream(store, auth):
    sessions, session, clock = auth
    s = stream(store, auth)
    clock.now = 101  # idle for longer than idle_ttl_s
    await append(store)  # any wake-up rechecks the session
    assert await next_chunk(s) == control("session_expired")


async def test_snapshots_and_events_agree_while_runs_change(tmp_path, catalog):
    """A snapshot's state is always the state of the last run_state event it has seen."""
    from .runharness import Harness
    from .simcluster import SimCluster

    store = await Store.open(str(tmp_path / "agree.db"))
    h = Harness(store, SimCluster(starting_snapshots=3), catalog)
    run_id = (await h.create()).body["run_id"]
    snap = await h.service.snapshot("alice", run_id)
    body = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    await h.service.submit("alice", run_id, body)
    seen = []
    while h.service.tasks:
        seen.append(await h.service.snapshot("alice", run_id))
        await asyncio.sleep(0)
    seen.append(await h.service.snapshot("alice", run_id))
    events = await h.events(run_id)
    for snap in seen:
        states = [e for e in events if e["type"] == "run_state" and e["seq"] <= snap["event_seq"]]
        assert states[-1]["payload"]["state"] == snap["state"]
        assert states[-1]["payload"]["revision"] == snap["revision"]
    assert seen[-1]["state"] == "healthy" and len(seen) > 2
    await store.close()
