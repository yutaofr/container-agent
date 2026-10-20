"""Browser tests: a real platform (uvicorn, hub, RunService) and a FakeRunner on the test
event loop, driven by Playwright's async API.

Run with the session loop the Playwright fixtures need, given explicitly:
    uv run pytest tests/browser -o asyncio_default_fixture_loop_scope=session \
        -o asyncio_default_test_loop_scope=session --browser chromium
"""

import asyncio
import os
import re
from dataclasses import dataclass

import pytest_asyncio

from cagent.config import load_config
from cagent.orchestrator import Settings
from cagent.server import Platform, web_server

from ..fakerunner import FakeRunner
from ..serverharness import ENV, RUNNER_TOKEN, free_port, write_config
from ..simcluster import SimCluster

RUN_URL = re.compile(r"/runs/[0-9a-f]{32}$")
FAST = Settings(poll_interval_s=0.05, stability_window_s=0.1, startup_timeout_s=5)


@dataclass
class Running:
    platform: Platform
    base: str
    runner: FakeRunner


@pytest_asyncio.fixture
async def running(tmp_path):
    port = free_port()
    config = load_config(write_config(tmp_path, port), ENV)
    async with Platform(config, FAST) as platform:
        server = web_server(platform, graceful_s=1)
        serving = asyncio.create_task(server.serve())
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        runner = FakeRunner(
            f"ws://127.0.0.1:{platform.hub_port}/runner", RUNNER_TOKEN, SimCluster()
        )
        await runner.start()
        yield Running(platform, f"http://127.0.0.1:{port}", runner)
        await runner.stop()
        server.should_exit = True
        await serving


def fail_on_purpose() -> bool:
    """Set CAGENT_BROWSER_FAIL=1 to check that a failing test leaves a trace and screenshot."""
    return os.environ.get("CAGENT_BROWSER_FAIL") == "1"


class Console:
    """Collects console errors and page errors (CSP violations show up here too)."""

    def __init__(self, page):
        self.errors: list[str] = []
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))


async def log_in(page, base: str, user: str = "alice") -> Console:
    console = Console(page)
    await page.goto(f"{base}/login")
    await page.fill('input[name="token"]', ENV[f"CAGENT_{user.upper()}"])
    await page.click('button[type="submit"]')
    await page.wait_for_url(f"{base}/")
    return console


async def create_run(page, base: str, spec: dict, cluster: str = "demo") -> str:
    """Create a run from the new-run form; return its id once its page is open."""
    import json

    await page.goto(f"{base}/runs/new")
    await page.fill('input[name="cluster"]', cluster)
    await page.check('input[name="input"][value="spec"]')
    await page.fill('textarea[name="spec"]', json.dumps(spec))
    await page.click('button:has-text("Create run")')
    await page.wait_for_url(RUN_URL)
    return page.url.rsplit("/", 1)[1]


async def put_pending(running: Running, run_id: str, kind: str, payload: dict, compose=None):
    """A test hook through the service, never an HTTP route: make the run wait on an item
    of any kind, as an execution would."""
    from cagent.orchestrator import Status

    service = running.platform.service
    async with running.platform.store.tx() as t:
        await t.update_run(run_id, state="running", pending=None, execution_id="hook")
    status = Status(None, {"kind": kind, **payload}, compose, [], "hook")
    await service._finished(run_id, "hook", ("alice", "hook"), status)
