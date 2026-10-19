"""M8a on real Docker: a run created over HTTP reaches healthy through the real hub and the
real Runner, with observations, Runner status, and the instance the Runner reports.

Skipped unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import asyncio
import os
import subprocess
import time

import httpx
import pytest

from cagent.config import load_config
from cagent.hub import status
from cagent.orchestrator import Settings
from cagent.server import Platform, web_server

from .conftest import FIXTURES
from .serverharness import ENV, RUNNER_TOKEN, free_port, login, write_config

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

SETTINGS = Settings(poll_interval_s=1, stability_window_s=3, startup_timeout_s=120)


async def test_a_run_over_http_reaches_healthy_on_real_docker(tmp_path):
    port, hub_port = free_port(), free_port()
    config = load_config(write_config(tmp_path, port, hub_port), ENV)
    config.catalog.append(FIXTURES / "e2e-catalog")
    name = f"m8a-e2e-{int(time.time()) % 100000}"
    async with Platform(config, SETTINGS) as platform:
        server = web_server(platform)
        serving = asyncio.create_task(server.serve())
        env = {**os.environ, "CAGENT_TOKEN": RUNNER_TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
        runner = subprocess.Popen(
            [os.environ["CAGENT_RUNNER"], "serve", "-hub", f"ws://127.0.0.1:{hub_port}/runner"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await platform.hub.wait_for_runner("laptop-1", timeout=10)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
                headers = await login(c)
                spec = {"purpose": "a cache", "middleware": [{"template": "redis"}]}
                body = {"command_id": "c1", "runner": "laptop-1", "cluster": name, "spec": spec}
                run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
                snap = await wait_for(c, run_id, "waiting", 60)
                assert snap["pending"]["kind"] == "approval"
                command = {
                    "command_id": "a1",
                    "expected_revision": snap["revision"],
                    "action": "answer",
                    "pending_id": snap["pending"]["pending_id"],
                    "decision": {"approve": True},
                }
                r = await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)
                assert r.status_code == 202, r.text
                snap = await wait_for(c, run_id, "healthy", 180)

            report = await status(platform.hub, "laptop-1")
            assert report["active"] is None and report["queued"] == {}
            assert snap["runner_status"] == {
                "online": True,
                "instance_id": report["instance_id"],
                "changed_at": snap["runner_status"]["changed_at"],
            }
            [redis] = snap["last_observation"]["services"]
            assert (redis["service"], redis["state"]) == ("redis", "running")
            assert snap["verified_at"] >= snap["last_observation"]["received_at"] - 1
            events = await platform.store.events_after(run_id, 0, 10_000, 10_000_000)
            types = {e["type"] for e in events}
            assert {"phase_started", "phase_interrupted", "observation", "run_state"} <= types
        finally:
            await platform.hub.call("laptop-1", "down", {"name": name})
            runner.terminate()
            runner.wait(timeout=10)
            server.should_exit = True
            await serving


async def wait_for(c, run_id, state, timeout):
    async with asyncio.timeout(timeout):
        while (snap := (await c.get(f"/api/runs/{run_id}")).json())["state"] != state:
            assert snap["state"] not in ("reconcile", "rejected", "escalated"), snap
            await asyncio.sleep(0.5)
    return snap
