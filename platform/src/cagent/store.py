"""Durable records for runs: the registry the web reads, commands, approvals, recovery plans,
cluster ownership and marks, tool dispatches, Runner status, a bounded event log, and the
pending items each run published and the decisions taken on them.

Events and pending records share one sequence per run, so a run record can be merged in
order; only events are trimmed. A gap in events is known from the trimming watermark, never
from holes in the sequence, which pending records leave on purpose.

One SQLite file holds these tables and LangGraph's checkpoints, but the two are written
through different connections and never in one transaction: nothing here may assume a
checkpoint and a registry change commit together. All of this module's writes go through one
connection, serialized by a lock, in short transactions that never wait on anything outside
the database.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    runner TEXT NOT NULL,
    cluster TEXT NOT NULL,
    input TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    thread_id TEXT,
    executions INTEGER NOT NULL DEFAULT 0,
    execution_id TEXT,
    execution_instance TEXT,
    phase TEXT,
    pending TEXT,
    reconcile TEXT,
    last_check TEXT,
    recovery TEXT,
    last_observation TEXT,
    last_observation_attempt TEXT,
    verified_at REAL,
    outcome TEXT,
    event_seq INTEGER NOT NULL DEFAULT 0,
    min_event_seq INTEGER NOT NULL DEFAULT 1,
    events_pruned_through_seq INTEGER NOT NULL DEFAULT 0,
    history TEXT NOT NULL DEFAULT 'partial'
);
CREATE TABLE IF NOT EXISTS commands (
    user TEXT NOT NULL,
    command_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    class TEXT NOT NULL,
    digest TEXT NOT NULL,
    response TEXT NOT NULL,
    status TEXT NOT NULL,
    effect TEXT,
    reason TEXT,
    execution_id TEXT,
    at REAL NOT NULL,
    PRIMARY KEY (user, command_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    pending_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    content TEXT NOT NULL,
    decision TEXT NOT NULL,
    user TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    digest TEXT NOT NULL,
    based_on_observation_seq INTEGER NOT NULL,
    facts TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    invalidated_at REAL
);
CREATE TABLE IF NOT EXISTS clusters (
    runner TEXT NOT NULL,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    lock_run_id TEXT,
    marks TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (runner, name)
);
CREATE TABLE IF NOT EXISTS dispatches (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    method TEXT NOT NULL,
    instance TEXT,
    sent_at REAL NOT NULL,
    result_at REAL,
    exit_code INTEGER
);
CREATE TABLE IF NOT EXISTS runner_status (
    runner TEXT PRIMARY KEY,
    online INTEGER NOT NULL,
    instance_id TEXT,
    changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pendings (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    pending_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    payload TEXT NOT NULL,
    user TEXT,
    command_id TEXT,
    PRIMARY KEY (run_id, seq),
    UNIQUE (pending_id, record_type)
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    execution_id TEXT,
    task_id TEXT,
    type TEXT NOT NULL,
    at REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

# Columns holding JSON, decoded on read.
_JSON = {
    "input",
    "pending",
    "reconcile",
    "last_check",
    "recovery",
    "last_observation",
    "last_observation_attempt",
    "outcome",
    "marks",
    "response",
    "facts",
    "content",
    "decision",
    "payload",
}

# Columns added after M8a, with the value existing rows get: runs from before the run
# record existed have only a partial history.
MIGRATIONS = {
    "events_pruned_through_seq": "INTEGER NOT NULL DEFAULT 0",
    "history": "TEXT NOT NULL DEFAULT 'partial'",
}

# Runs in these states hold their cluster's execution lock; the rest are final.
ACTIVE_STATES = ("running", "waiting", "reconcile")

MAX_STRING = 2000  # characters kept per string in an event payload


def _decode(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: json.loads(row[k]) if k in _JSON and row[k] is not None else row[k] for k in row.keys()
    }


def _encode(column: str, value: Any) -> Any:
    return json.dumps(value) if column in _JSON and value is not None else value


def bounded(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Cap an event payload: long strings are cut and marked; a payload still over limit
    bytes is replaced by a marker, so a single log line cannot grow the event log."""

    def cap(v: Any) -> Any:
        if isinstance(v, str) and len(v) > MAX_STRING:
            return v[:MAX_STRING] + "…[truncated]"
        if isinstance(v, list):
            return [cap(x) for x in v]
        if isinstance(v, dict):
            return {k: cap(x) for k, x in v.items()}
        return v

    capped = cap(payload)
    size = len(json.dumps(capped))
    if size > limit:
        return {"truncated": True, "bytes": size}
    return capped


class Notifier:
    """Wakes event subscribers after a commit. Subscribers read events from the database; a
    wake-up is only a hint, so a missed one costs at most a heartbeat, never an event.

    A subscriber takes generation(run) before reading, and wait() returns at once if the
    generation moved since, so a commit between "found nothing" and "start waiting" is not
    missed."""

    def __init__(self) -> None:
        self._gen: dict[str, int] = {}
        self._cond = asyncio.Condition()

    def generation(self, run_id: str) -> int:
        return self._gen.get(run_id, 0)

    async def bump(self, run_ids: Iterable[str]) -> None:
        async with self._cond:
            for r in run_ids:
                self._gen[r] = self._gen.get(r, 0) + 1
            self._cond.notify_all()

    async def wait(self, run_id: str, seen: int, timeout: float) -> bool:
        """Wait until run_id's generation passes seen; False on timeout."""
        try:
            async with asyncio.timeout(timeout), self._cond:
                await self._cond.wait_for(lambda: self.generation(run_id) != seen)
            return True
        except TimeoutError:
            return False


class Tx:
    """One open transaction, with helpers for the tables above."""

    def __init__(self, store: "Store", db: aiosqlite.Connection):
        self.store, self.db = store, db
        self.touched: set[str] = set()  # runs with new events, woken after commit

    async def one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        async with self.db.execute(sql, args) as cur:
            return _decode(await cur.fetchone())

    async def all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.db.execute(sql, args) as cur:
            return [_decode(r) for r in await cur.fetchall()]

    async def run(self, sql: str, *args: Any) -> int:
        """Execute a statement; return the number of rows it changed."""
        async with self.db.execute(sql, args) as cur:
            return cur.rowcount

    async def insert(self, table: str, **values: Any) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        args = [_encode(c, v) for c, v in values.items()]
        await self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *args)

    async def update(self, table: str, where: dict[str, Any], **values: Any) -> int:
        sets = ", ".join(f"{c} = ?" for c in values)
        cond = " AND ".join(f"{c} = ?" for c in where)
        args = [_encode(c, v) for c, v in values.items()] + list(where.values())
        return await self.run(f"UPDATE {table} SET {sets} WHERE {cond}", *args)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self.one("SELECT * FROM runs WHERE id = ?", run_id)

    async def update_run(self, run_id: str, **values: Any) -> None:
        await self.update("runs", {"id": run_id}, **values)

    async def bump_revision(self, run_id: str) -> int:
        row = await self.one(
            "UPDATE runs SET revision = revision + 1 WHERE id = ? RETURNING revision", run_id
        )
        return row["revision"]

    async def next_seq(self, run_id: str) -> int:
        """The next number in the run's sequence, shared by events and pending records."""
        row = await self.one(
            "UPDATE runs SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq", run_id
        )
        return row["event_seq"]

    async def append_pending(
        self,
        run_id: str,
        record_type: str,
        pending_id: str,
        kind: str,
        payload: dict[str, Any],
        user: str | None = None,
        command_id: str | None = None,
    ) -> int:
        """Append a published pending item or a decision on one. Never trimmed or changed;
        (pending_id, record_type) is unique, so a retried transaction cannot record twice."""
        seq = await self.next_seq(run_id)
        await self.insert(
            "pendings",
            run_id=run_id,
            seq=seq,
            pending_id=pending_id,
            record_type=record_type,
            kind=kind,
            at=self.store.clock(),
            payload=payload,
            user=user,
            command_id=command_id,
        )
        self.touched.add(run_id)
        return seq

    async def append_event(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any],
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        """Append one event and trim the run's log to its newest keep_events event rows,
        recording the highest seq deleted. Numbering never depends on what is kept."""
        seq = await self.next_seq(run_id)
        await self.insert(
            "events",
            run_id=run_id,
            seq=seq,
            execution_id=execution_id,
            task_id=task_id,
            type=type,
            at=self.store.clock(),
            payload=bounded(payload, self.store.max_payload),
        )
        cut = await self.one(
            "SELECT seq FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?",
            run_id,
            self.store.keep_events,
        )
        if cut is not None:
            await self.run("DELETE FROM events WHERE run_id = ? AND seq <= ?", run_id, cut["seq"])
            await self.run(
                "UPDATE runs SET events_pruned_through_seq = MAX(events_pruned_through_seq, ?),"
                " min_event_seq = MAX(min_event_seq, ?) WHERE id = ?",
                cut["seq"],
                cut["seq"] + 1,
                run_id,
            )
        self.touched.add(run_id)
        return seq

    async def get_cluster(self, runner: str, name: str) -> dict[str, Any] | None:
        return await self.one("SELECT * FROM clusters WHERE runner = ? AND name = ?", runner, name)


class Store:
    def __init__(
        self,
        db: aiosqlite.Connection,
        keep_events: int = 1000,
        max_payload: int = 64 * 1024,
        clock: Callable[[], float] = time.time,
    ):
        self.db, self.keep_events, self.max_payload, self.clock = (
            db,
            keep_events,
            max_payload,
            clock,
        )
        self.notifier = Notifier()
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str, **kwargs: Any) -> "Store":
        db = await aiosqlite.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(SCHEMA)
        async with db.execute("PRAGMA table_info(runs)") as cur:
            have = {row["name"] for row in await cur.fetchall()}
        for column, definition in MIGRATIONS.items():
            if column not in have:
                await db.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
        return cls(db, **kwargs)

    async def close(self) -> None:
        await self.db.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        """A write transaction (BEGIN IMMEDIATE). It commits on exit and rolls back on an
        exception; subscribers of runs that got events are woken only after the commit."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            t = Tx(self, self.db)
            try:
                yield t
            except BaseException:
                await self.db.execute("ROLLBACK")
                raise
            await self.db.execute("COMMIT")
        if t.touched:
            await self.notifier.bump(t.touched)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Tx]:
        """A read transaction: everything read inside it is one consistent view."""
        async with self._lock:
            await self.db.execute("BEGIN")
            try:
                yield Tx(self, self.db)
            finally:
                await self.db.execute("ROLLBACK")

    async def events_after(
        self, run_id: str, after: int, limit: int, max_bytes: int
    ) -> list[dict[str, Any]] | None:
        """The next page of events with seq > after, in order, or None when events after the
        cursor were trimmed away, or the cursor is past the sequence: the reader must resync.
        Each page is its own short read, so the watermark is checked again for every page."""
        async with self.read() as t:
            run = await t.one(
                "SELECT event_seq, events_pruned_through_seq FROM runs WHERE id = ?", run_id
            )
            if run is None or after < run["events_pruned_through_seq"] or after > run["event_seq"]:
                return None
            rows = await t.all(
                "SELECT seq, execution_id, task_id, type, at, payload FROM events"
                " WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                run_id,
                after,
                limit,
            )
        page, size = [], 0
        for r in rows:
            size += len(json.dumps(r["payload"]))
            if page and size > max_bytes:
                break
            page.append(r)
        return page
