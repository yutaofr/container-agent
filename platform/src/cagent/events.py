"""Server-sent events for one run, always read from the store.

A first connection and a reconnection take the same path: read stored events after the
cursor, page by page, each page in its own short read that checks the cursor is still inside
what is kept. A gap (trimmed events, or a cursor past the last event) ends the stream with a
`resync` control event, and the client fetches a snapshot before reconnecting. Waiting uses
the store's notification generation, so a commit between "found nothing" and "start waiting"
is not missed, and a heartbeat re-reads the database anyway.

Control events (`resync`, `session_expired`) have no id and no sequence number. Heartbeats
never count as use of the session; logout and expiry end the stream at once.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.responses import Response

from cagent.auth import Session, Sessions
from cagent.store import Store


@dataclass(frozen=True)
class StreamLimits:
    page_events: int = 100
    page_bytes: int = 256 * 1024
    heartbeat_s: float = 15.0
    send_timeout_s: float = 10.0
    streams_per_session: int = 4


class BadCursor(ValueError):
    """A cursor that is not a non-negative integer."""


def parse_cursor(last_event_id: str | None, after: str | None) -> int:
    """Last-Event-ID wins over after; with neither, start at 0."""
    raw = last_event_id if last_event_id is not None else after
    if raw is None:
        return 0
    if not raw.isdigit():
        raise BadCursor(raw)
    return int(raw)


def _event(e: dict[str, Any]) -> str:
    data = {
        "seq": e["seq"],
        "type": e["type"],
        "at": e["at"],
        "execution_id": e["execution_id"],
        "task_id": e["task_id"],
        "payload": e["payload"],
    }
    return f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(data)}\n\n"


def control(name: str) -> str:
    return f"event: {name}\ndata: {{}}\n\n"


HEARTBEAT = ": heartbeat\n\n"


async def _first(*aws: Any, timeout: float) -> bool:
    """Wait until any awaitable finishes; False if none did within timeout."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return any(t.result() for t in done if not t.cancelled() and t.exception() is None)


async def event_stream(
    store: Store,
    sessions: Sessions,
    session: Session,
    run_id: str,
    cursor: int,
    limits: StreamLimits,
) -> AsyncIterator[str]:
    """Yield SSE text for run_id after cursor until the session ends or a resync is needed."""
    while True:
        if sessions.get(session.id) is not session:
            yield control("session_expired")
            return
        seen = store.notifier.generation(run_id)  # taken before reading
        page = await store.events_after(run_id, cursor, limits.page_events, limits.page_bytes)
        if page is None:
            yield control("resync")
            return
        for e in page:
            if session.revoked.is_set():  # checked again before every business event
                yield control("session_expired")
                return
            yield _event(e)
            cursor = e["seq"]
        if page:
            continue
        until_expiry = max(0.0, sessions.deadline(session) - sessions.clock())
        woke = await _first(
            store.notifier.wait(run_id, seen, limits.heartbeat_s),
            session.revoked.wait(),
            timeout=min(limits.heartbeat_s, until_expiry),
        )
        if not woke and not session.revoked.is_set():
            yield HEARTBEAT


class EventStreamResponse(Response):
    """An event-stream response: unbuffered, with a timeout on every send, and stopped as
    soon as the client disconnects."""

    def __init__(
        self, body: AsyncIterator[str], send_timeout_s: float, on_close: Callable[[], None]
    ):
        self.status_code, self.background = 200, None
        self.body, self.send_timeout, self.on_close = body, send_timeout_s, on_close

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def stream() -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                        (b"x-accel-buffering", b"no"),  # tell proxies not to buffer
                    ],
                }
            )
            async for chunk in self.body:
                message = {"type": "http.response.body", "body": chunk.encode(), "more_body": True}
                await asyncio.wait_for(send(message), self.send_timeout)
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def disconnected() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

        tasks = [asyncio.ensure_future(stream()), asyncio.ensure_future(disconnected())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.body.aclose()
            self.on_close()
