"""The platform as it runs: a real uvicorn server, hub, and a runner over WebSocket."""

import asyncio

import httpx
import pytest

from cagent.config import load_config
from cagent.orchestrator import Settings
from cagent.server import Platform, web_server

from .fakerunner import FakeRunner
from .serverharness import ENV, REDIS, RUNNER_TOKEN, free_port, login, sse, write_config
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)


@pytest.fixture
async def running(tmp_path):
    port = free_port()
    config = load_config(write_config(tmp_path, port), ENV)
    async with Platform(config, FAST) as platform:
        server = web_server(platform)
        task = asyncio.create_task(server.serve())
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        url = f"ws://127.0.0.1:{platform.hub_port}/runner"
        runner = FakeRunner(url, RUNNER_TOKEN, SimCluster())
        await runner.start()
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            yield platform, client, runner
        await runner.stop()
        server.should_exit = True
        await task


async def wait_for(client, run_id, state):
    async with asyncio.timeout(10):
        while (snap := (await client.get(f"/api/runs/{run_id}")).json())["state"] != state:
            await asyncio.sleep(0.02)
    return snap


async def test_a_stream_resumes_after_a_disconnect(running):
    platform, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    snap = await wait_for(c, run_id, "waiting")
    assert snap["runner_status"]["online"] is True

    async with c.stream("GET", f"/api/runs/{run_id}/events?after=0") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        first = []
        async for event in sse(r):
            first.append(event)
            if len(first) == 3:
                break  # the client goes away
    last = first[-1][0]

    command = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    assert (await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)).is_success
    seqs, states = [], []
    async with c.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": last}) as r:
        async for event_id, event, data in sse(r):
            seqs.append(int(event_id))
            if event == "run_state":
                states.append(data["payload"]["state"])
                if data["payload"]["state"] == "healthy":
                    break
    assert seqs[0] == int(last) + 1 and seqs == sorted(seqs)
    assert states[-1] == "healthy"


async def test_logout_ends_open_streams_at_once(running):
    _, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    snap = await wait_for(c, run_id, "waiting")
    after = snap["event_seq"]

    async def read():
        async with c.stream("GET", f"/api/runs/{run_id}/events?after={after}") as r:
            return [event async for _, event, _ in sse(r)]

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.2)  # the stream is waiting for news
    assert (await c.post("/logout", headers=headers)).is_success
    async with asyncio.timeout(2):
        assert await reader == ["session_expired"]


async def test_streams_check_access_and_cursors(running):
    _, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    assert (await c.get(f"/api/runs/{run_id}/events?after=-1")).status_code == 422
    assert (await c.get(f"/api/runs/{run_id}/events?after=x")).status_code == 422
    async with httpx.AsyncClient(base_url=c.base_url) as bob:
        await login(bob, "bob")
        assert (await bob.get(f"/api/runs/{run_id}/events")).status_code == 404
    async with httpx.AsyncClient(base_url=c.base_url) as nobody:
        assert (await nobody.get(f"/api/runs/{run_id}/events")).status_code == 401
