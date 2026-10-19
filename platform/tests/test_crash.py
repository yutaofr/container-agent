"""Crashes of the platform process (kill -9) and restarts on the same database.

The platform runs as a real subprocess; the runner is a FakeRunner in this process that
reconnects to the restarted hub. In every case a pending item is consumed at most once, a
command starts at most one execution, and startup advances no graph."""

import asyncio
import os
import sys

import httpx
import pytest

from .fakerunner import FakeRunner
from .serverharness import ENV, REDIS, RUNNER_TOKEN, free_port, login, write_config
from .simcluster import SimCluster


class Platform:
    """cagent-platform serve as a subprocess."""

    def __init__(self, tmp_path):
        self.web, self.hub = free_port(), free_port()
        self.config = write_config(tmp_path, self.web, self.hub, fast=True)
        self.log = tmp_path / "platform.log"
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        with self.log.open("a") as log:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from cagent.server import main; main()",
                "serve",
                "--config",
                str(self.config),
                env={**os.environ, **ENV},
                stdout=log,
                stderr=log,
            )
        async with asyncio.timeout(30), httpx.AsyncClient() as c:
            while True:
                try:
                    if (await c.get(f"http://127.0.0.1:{self.web}/login")).is_success:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    async def kill(self) -> None:
        self.proc.kill()  # SIGKILL: no shutdown handlers run
        await self.proc.wait()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.web}", timeout=10)


@pytest.fixture
async def platform(tmp_path):
    p = Platform(tmp_path)
    await p.start()
    runner = FakeRunner(f"ws://127.0.0.1:{p.hub}/runner", RUNNER_TOKEN, SimCluster())
    await runner.start()
    yield p, runner
    await runner.stop()
    if p.proc.returncode is None:
        p.proc.terminate()
        await p.proc.wait()


async def wait_for(c, run_id, state):
    async with asyncio.timeout(20):
        while (snap := (await c.get(f"/api/runs/{run_id}")).json())["state"] != state:
            await asyncio.sleep(0.05)
    return snap


async def waiting_run(c, headers):
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    return run_id, await wait_for(c, run_id, "waiting")


def approval(snap, command_id="a1"):
    return {
        "command_id": command_id,
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }


async def restart(p, runner):
    await p.start()
    async with asyncio.timeout(10):
        while runner.connections < 2 or not runner.connected.is_set():
            await asyncio.sleep(0.05)


async def recover(c, headers, run_id):
    """Refresh, review the plan, approve it; return the plan."""
    for action, cid in [("refresh", "f1"), ("review_recovery", "v1")]:
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        body = {"command_id": cid, "expected_revision": snap["revision"], "action": action}
        r = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert r.status_code == 202, r.text
    snap = (await c.get(f"/api/runs/{run_id}")).json()
    plan = snap["pending"]["payload"]
    decision = {"approve": True, "acknowledged": plan["required"]}
    body = {**approval(snap, "r1"), "decision": decision}
    assert (await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)).is_success
    return plan


async def test_a_crash_after_an_approval_was_accepted(platform):
    p, runner = platform
    entered = runner.hold("up")
    async with p.client() as c:
        headers = await login(c)
        run_id, snap = await waiting_run(c, headers)
        body = approval(snap)
        first = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert first.status_code == 202
        async with asyncio.timeout(10):
            await entered.wait()
    await p.kill()  # up is running on the runner; the platform dies
    runner.held["up"].set()  # the runner finishes, but nobody hears it
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        assert snap["state"] == "reconcile" and snap["reconcile"]["last_phase"] == "up"
        done = (await c.get(f"/api/runs/{run_id}/commands/a1")).json()
        assert (done["status"], done["effect"]) == ("interrupted", "unknown")
        again = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert again.status_code == 202 and again.headers["cagent-replay"] == "true"
        assert again.json() == first.json()  # the accepted fact is unchanged
        other = {**body, "command_id": "a2"}  # the same pending item, consumed already
        r = await c.post(f"/api/runs/{run_id}/commands", json=other, headers=headers)
        assert r.status_code == 409
        await asyncio.sleep(0.3)
        assert runner.sim.calls.count("up") == 1  # nothing ran again by itself

        runner.held.clear()
        plan = await recover(c, headers, run_id)
        assert [r["id"] for r in plan["risks"]][0].startswith("mark:")
        await wait_for(c, run_id, "healthy")
        assert runner.sim.calls.count("up") == 2


async def test_a_crash_after_the_runner_finished_but_before_the_result_was_stored(platform):
    p, runner = platform
    runner.silent.add("up")  # up runs; its answer never reaches the platform
    async with p.client() as c:
        headers = await login(c)
        run_id, snap = await waiting_run(c, headers)
        await c.post(f"/api/runs/{run_id}/commands", json=approval(snap), headers=headers)
        async with asyncio.timeout(10):
            while "up" not in runner.sim.calls:
                await asyncio.sleep(0.02)
    await p.kill()
    runner.silent.clear()
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        assert snap["state"] == "reconcile" and snap["reconcile"]["reason"] == "platform restarted"
        await asyncio.sleep(0.3)
        assert runner.sim.calls.count("up") == 1
        plan = await recover(c, headers, run_id)
        texts = [r["text"] for r in plan["risks"]]
        assert "Unconfirmed operation: up sent without a recorded result" in texts
        await wait_for(c, run_id, "healthy")


async def test_a_restart_while_waiting_keeps_the_pending_item(platform):
    p, runner = platform
    async with p.client() as c:
        headers = await login(c)
        run_id, before = await waiting_run(c, headers)
    await p.kill()
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        after = (await c.get(f"/api/runs/{run_id}")).json()
        assert after["state"] == "waiting" and after["pending"] == before["pending"]
        r = await c.post(f"/api/runs/{run_id}/commands", json=approval(after), headers=headers)
        again = await c.post(f"/api/runs/{run_id}/commands", json=approval(after), headers=headers)
        assert r.status_code == again.status_code == 202 and again.json() == r.json()
        await wait_for(c, run_id, "healthy")
        assert runner.sim.calls.count("up") == 1
