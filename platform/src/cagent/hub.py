"""Runner Hub: Runners dial in over WebSocket; the platform calls their tools with JSON-RPC 2.0.

The hub never connects to a user's machine. A Runner authenticates with a bearer token, which
the hub maps to a runner id; after that, `call` sends a request and awaits the matching result.
"""

import asyncio
import hmac
import itertools
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

# Seconds to wait for a result. Builds compile and pull images, so they get longer.
DEFAULT_TIMEOUT = 120.0
TIMEOUTS = {"build": 900.0, "up": 300.0}

# Seconds a new connection has to send its hello before the hub drops it.
HELLO_TIMEOUT = 10.0


class RunnerUnavailable(Exception):
    """The runner is not connected, or disconnected before answering."""


class InstanceChanged(RunnerUnavailable):
    """The runner connected now is another process than the one a call was pinned to."""


class RunnerError(Exception):
    """The runner refused the call: unknown method or invalid params (JSON-RPC error)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolResult:
    """One subcommand's outcome, as the CLI would report it."""

    exit_code: int
    output: Any
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class _Runner:
    ws: ServerConnection
    tools: list[str] = field(default_factory=list)
    instance: str | None = None  # the runner process, unchanged across its reconnects
    pending: dict[int, asyncio.Future] = field(default_factory=dict)


# Told about every connection change: (runner id, connected, instance id).
Listener = Callable[[str, bool, str | None], None]


class RunnerHub:
    def __init__(self, tokens: Mapping[str, str]):
        """tokens maps each accepted bearer token to the runner id it authenticates."""
        self._tokens = dict(tokens)
        self._runners: dict[str, _Runner] = {}
        self._ids = itertools.count(1)
        self._changed = asyncio.Condition()
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        """Call listener(runner_id, connected, instance) whenever a runner connects or goes."""
        self._listeners.append(listener)

    def _tell(self, runner_id: str, runner: "_Runner | None") -> None:
        for listener in self._listeners:
            listener(runner_id, runner is not None, runner.instance if runner else None)

    def _runner_id(self, request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        presented = auth.removeprefix("Bearer ").encode()
        for token, runner_id in self._tokens.items():
            if hmac.compare_digest(presented, token.encode()):
                return runner_id
        return None

    def _authenticate(self, connection: ServerConnection, request: Request) -> Response | None:
        if self._runner_id(request) is None:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        runner_id = self._runner_id(ws.request)
        assert runner_id is not None  # _authenticate already rejected anything else
        # A runner counts as connected only once it has said hello, so callers that wait for
        # it can rely on its tool list.
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT))
        except (TimeoutError, ConnectionClosed, ValueError):
            await ws.close(reason="expected hello")
            return
        if not isinstance(first, dict) or first.get("method") != "hello":
            await ws.close(reason="expected hello")
            return
        params = first.get("params") or {}
        runner = _Runner(
            ws, tools=list(params.get("tools", [])), instance=params.get("instance_id") or None
        )
        # Swap in the new connection before closing the old one, so no call can be routed
        # to a connection that is already closing.
        async with self._changed:
            old = self._runners.get(runner_id)
            self._runners[runner_id] = runner
            self._changed.notify_all()
        self._tell(runner_id, runner)
        if old is not None:
            await old.ws.close(reason="replaced by a new connection")
        try:
            async for raw in ws:
                self._receive(runner, raw)
        except ConnectionClosed:
            pass
        finally:
            for fut in runner.pending.values():
                if not fut.done():
                    fut.set_exception(RunnerUnavailable(f"{runner_id} disconnected"))
            async with self._changed:
                gone = self._runners.get(runner_id) is runner
                if gone:
                    del self._runners[runner_id]
                self._changed.notify_all()
            if gone:
                self._tell(runner_id, None)

    def _receive(self, runner: _Runner, raw: str | bytes) -> None:
        msg = json.loads(raw)
        fut = runner.pending.get(msg.get("id"))
        if fut is None or fut.done():
            return  # an answer to a call that already timed out
        if error := msg.get("error"):
            fut.set_exception(RunnerError(error.get("code", 0), error.get("message", "")))
        else:
            fut.set_result(msg.get("result"))

    def connected(self) -> set[str]:
        return set(self._runners)

    def instance(self, runner_id: str) -> str | None:
        """The connected runner's process instance, or None when it is not connected."""
        runner = self._runners.get(runner_id)
        return runner.instance if runner else None

    def tools(self, runner_id: str) -> list[str]:
        runner = self._runners.get(runner_id)
        return list(runner.tools) if runner else []

    async def wait_for_runner(self, runner_id: str, timeout: float) -> None:
        """Return once runner_id is connected; raise TimeoutError after timeout seconds."""
        async with asyncio.timeout(timeout), self._changed:
            await self._changed.wait_for(lambda: runner_id in self._runners)

    async def call(
        self,
        runner_id: str,
        method: str,
        params: Mapping[str, Any],
        timeout: float | None = None,
        instance: str | None = None,
    ) -> ToolResult:
        """Call one tool on a runner and return its result. With instance, the call is sent
        only if the connected runner is that process, and on the connection just checked."""
        runner = self._runners.get(runner_id)
        if runner is None:
            raise RunnerUnavailable(f"{runner_id} is not connected")
        if instance is not None and runner.instance != instance:
            raise InstanceChanged(f"{runner_id} is now instance {runner.instance}, not {instance}")
        call_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        runner.pending[call_id] = fut
        try:
            request = {"jsonrpc": "2.0", "id": call_id, "method": method, "params": dict(params)}
            try:
                await runner.ws.send(json.dumps(request))
            except ConnectionClosed as e:
                raise RunnerUnavailable(f"{runner_id} disconnected") from e
            result = await asyncio.wait_for(fut, timeout or TIMEOUTS.get(method, DEFAULT_TIMEOUT))
        finally:
            runner.pending.pop(call_id, None)
        return ToolResult(
            exit_code=result["exit_code"],
            output=result.get("output"),
            stderr=result.get("stderr", ""),
        )


async def status(hub: RunnerHub, runner_id: str) -> dict[str, Any]:
    """The runner's own report: {instance_id, active, queued}. It answers even while a long
    call runs, because the runner handles status outside its call queue."""
    res = await hub.call(runner_id, "status", {})
    if not res.ok or not isinstance(res.output, dict):
        raise RunnerError(0, f"status failed: {res.stderr}")
    return res.output


async def start(hub: RunnerHub, host: str, port: int) -> Server:
    """Serve the hub until the returned server is closed."""
    return await serve(hub._handle, host, port, process_request=hub._authenticate)
