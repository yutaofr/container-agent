"""Hub tests against a scripted fake runner speaking the same JSON-RPC protocol as the Go one."""

import asyncio
import json
from collections.abc import Callable

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from cagent.hub import (
    InstanceChanged,
    RunnerError,
    RunnerHub,
    RunnerUnavailable,
    start,
    status,
)

TOKEN = "t-123"


@pytest.fixture
async def hub():
    hub = RunnerHub({TOKEN: "laptop-1"})
    server = await start(hub, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield hub, f"ws://127.0.0.1:{port}/runner"
    server.close()
    await server.wait_closed()


async def fake_runner(
    url: str, answer: Callable[[dict], dict | None], token: str = TOKEN, instance: str = "i-1"
):
    """Connect, say hello, and answer each request with answer(request); None means stay silent."""
    ws = await connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    hello = {"tools": ["validate"], "instance_id": instance}
    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "hello", "params": hello}))

    async def loop():
        async for raw in ws:
            req = json.loads(raw)
            reply = answer(req)
            if reply is not None:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], **reply}))

    return ws, asyncio.create_task(loop())


def echo(req: dict) -> dict:
    return {"result": {"exit_code": 0, "output": {"echo": req["params"]}, "stderr": ""}}


async def connected(hub: RunnerHub, url: str, answer=echo, instance="i-1") -> ClientConnection:
    ws, _ = await fake_runner(url, answer, instance=instance)
    await hub.wait_for_runner("laptop-1", timeout=5)
    return ws


async def test_call_returns_the_tool_result(hub):
    h, url = hub
    await connected(h, url)
    result = await h.call("laptop-1", "validate", {"name": "demo"})
    assert result.ok and result.output == {"echo": {"name": "demo"}}
    assert h.tools("laptop-1") == ["validate"]


async def test_non_zero_exit_is_a_result_not_an_error(hub):
    h, url = hub
    await connected(h, url, lambda req: {"result": {"exit_code": 1, "output": {"findings": [1]}}})
    result = await h.call("laptop-1", "validate", {})
    assert not result.ok and result.exit_code == 1 and result.output == {"findings": [1]}


async def test_rpc_error_is_raised(hub):
    h, url = hub
    await connected(h, url, lambda req: {"error": {"code": -32601, "message": "unknown method rm"}})
    with pytest.raises(RunnerError) as err:
        await h.call("laptop-1", "rm", {})
    assert err.value.code == -32601


async def test_bad_token_is_rejected_at_handshake(hub):
    h, url = hub
    with pytest.raises(InvalidStatus) as err:
        await fake_runner(url, echo, token="wrong")
    assert err.value.response.status_code == 401
    assert h.connected() == set()


async def test_unknown_runner_is_unavailable(hub):
    h, _ = hub
    with pytest.raises(RunnerUnavailable):
        await h.call("laptop-1", "validate", {})


async def test_disconnect_fails_the_pending_call(hub):
    h, url = hub
    ws = await connected(h, url, lambda req: None)  # never answers
    call = asyncio.create_task(h.call("laptop-1", "snapshot", {"name": "demo"}))
    await asyncio.sleep(0.1)
    await ws.close()
    with pytest.raises(RunnerUnavailable):
        await call
    assert h.connected() == set()


async def test_call_times_out(hub):
    h, url = hub
    await connected(h, url, lambda req: None)
    with pytest.raises(TimeoutError):
        await h.call("laptop-1", "snapshot", {}, timeout=0.2)


async def test_reconnect_replaces_the_old_connection(hub):
    h, url = hub
    old = await connected(h, url)
    new = await connected(h, url)
    await old.wait_closed()
    assert h.connected() == {"laptop-1"}
    assert (await h.call("laptop-1", "validate", {"n": 2})).output == {"echo": {"n": 2}}
    await new.close()


async def test_runner_counts_as_connected_only_after_hello(hub):
    h, url = hub
    ws = await connect(url, additional_headers={"Authorization": f"Bearer {TOKEN}"})
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}))  # not a hello
    await ws.wait_closed()
    assert ws.close_reason == "expected hello"
    assert h.connected() == set()


async def test_hub_knows_each_runners_instance(hub):
    h, url = hub
    assert h.instance("laptop-1") is None
    ws = await connected(h, url, instance="i-1")
    assert h.instance("laptop-1") == "i-1"
    await ws.close()
    await connected(h, url, instance="i-2")
    assert h.instance("laptop-1") == "i-2"


async def test_a_pinned_call_goes_only_to_its_instance(hub):
    h, url = hub
    sent = []
    await connected(h, url, lambda req: sent.append(req) or echo(req), instance="i-2")
    with pytest.raises(InstanceChanged):
        await h.call("laptop-1", "up", {"name": "demo"}, instance="i-1")
    assert sent == []  # refused before anything was sent
    assert (await h.call("laptop-1", "up", {"name": "demo"}, instance="i-2")).ok


async def test_listeners_hear_connects_and_disconnects(hub):
    h, url = hub
    heard = []
    h.add_listener(lambda runner, up, instance: heard.append((runner, up, instance)))
    ws = await connected(h, url, instance="i-1")
    await ws.close()
    async with asyncio.timeout(5):
        while len(heard) < 2:
            await asyncio.sleep(0.01)
    assert heard == [("laptop-1", True, "i-1"), ("laptop-1", False, None)]


async def test_status_returns_the_runners_report(hub):
    h, url = hub
    report = {"instance_id": "i-1", "active": None, "queued": {}}

    def answer(req):
        assert req["method"] == "status" and req["params"] == {}
        return {"result": {"exit_code": 0, "output": report}}

    await connected(h, url, answer)
    assert await status(h, "laptop-1") == report
