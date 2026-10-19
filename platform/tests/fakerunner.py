"""A Runner process stand-in: dials the hub over WebSocket like cagent-runner and answers
from a SimCluster. Tests can hold a method (it starts, then waits) or make it silent (it
runs, and its answer is never sent), and it reconnects when the hub restarts."""

import asyncio
import contextlib
import json
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .simcluster import SimCluster


class FakeRunner:
    def __init__(self, url: str, token: str, sim: SimCluster, instance: str = "i-1"):
        self.url, self.token, self.sim, self.instance = url, token, sim, instance
        self.held: dict[str, asyncio.Event] = {}  # method -> release
        self.entered: dict[str, asyncio.Event] = {}  # method -> it started
        self.silent: set[str] = set()  # run, but never answer
        self.connections = 0
        self.connected = asyncio.Event()
        self.active: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def hold(self, method: str) -> asyncio.Event:
        self.entered[method] = asyncio.Event()
        self.held[method] = asyncio.Event()
        return self.entered[method]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        async with asyncio.timeout(10):
            await self.connected.wait()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self._serve()
            except (OSError, ConnectionClosed, InvalidStatus):
                pass
            self.connected.clear()
            await asyncio.sleep(0.05)

    async def _serve(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with connect(self.url, additional_headers=headers) as ws:
            tools = ["build", "inspect", "snapshot", "status", "up", "validate", "verify"]
            hello = {"tools": tools, "instance_id": self.instance}
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "hello", "params": hello}))
            self.connections += 1
            self.connected.set()
            async for raw in ws:
                req = json.loads(raw)
                if req["method"] == "status":
                    report = {"instance_id": self.instance, "active": self.active, "queued": {}}
                    await self._answer(ws, req, {"exit_code": 0, "output": report})
                    continue
                asyncio.create_task(self._call(ws, req))

    async def _call(self, ws, req: dict[str, Any]) -> None:
        method = req["method"]
        name = req["params"].get("name")
        self.active = {"call_id": req["id"], "method": method, "cluster": name, "started_at": "t"}
        try:
            if method in self.held:
                self.entered[method].set()
                await self.held[method].wait()
            res = await self.sim.call("runner", method, req["params"])
        finally:
            self.active = None
        if method in self.silent:
            return
        out = {"exit_code": res.exit_code, "output": res.output, "stderr": res.stderr}
        with contextlib.suppress(ConnectionClosed):
            await self._answer(ws, req, out)

    @staticmethod
    async def _answer(ws, req: dict[str, Any], result: dict[str, Any]) -> None:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}))
