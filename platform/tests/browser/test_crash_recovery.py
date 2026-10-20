"""A platform killed mid-run (kill -9) and restarted on the same database, seen from the
browser: the old session ends, the user logs in again, and the run continues only after
a recovery the user approves."""

import asyncio

import pytest_asyncio
from playwright.async_api import Page, expect

from ..fakerunner import FakeRunner
from ..serverharness import REDIS, RUNNER_TOKEN
from ..simcluster import SimCluster
from ..test_crash import Platform
from .conftest import create_run, log_in


@pytest_asyncio.fixture
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


async def test_a_killed_platform_resumes_only_after_an_approved_recovery(page: Page, platform):
    p, runner = platform
    base = f"http://127.0.0.1:{p.web}"
    entered = runner.hold("up")
    await log_in(page, base)
    run_id = await create_run(page, base, REDIS)
    approval = page.locator('#pending article[data-kind="approval"]')
    await approval.locator('button[value="approve"]').click()
    async with asyncio.timeout(10):
        await entered.wait()

    await p.kill()  # up is running on the Runner; the platform dies
    runner.held["up"].set()  # the Runner finishes; nobody hears it
    await p.start()
    async with asyncio.timeout(10):
        while runner.connections < 2:
            await asyncio.sleep(0.05)

    # The old page's session is gone with the old process.
    await expect(page.locator(".banner")).to_contain_text("Your session ended", timeout=15_000)
    assert runner.sim.calls.count("up") == 1  # nothing was sent again by itself

    await log_in(page, base)
    await page.goto(f"{base}/runs/{run_id}")
    panel = page.locator('#pending article[data-kind="reconcile"]')
    await expect(panel).to_contain_text("Reconcile this run")
    await expect(page.locator("#status")).to_contain_text("platform restarted")
    await panel.locator('button:has-text("Refresh check")').click()
    review = page.locator('#pending button:has-text("Review recovery plan")')
    await expect(review).to_be_enabled(timeout=10_000)
    await review.click()
    plan = page.locator('#pending article[data-kind="recovery"]')
    await expect(plan).to_contain_text("Unconfirmed operation: up sent without a recorded result")
    assert runner.sim.calls.count("up") == 1
    await plan.locator('button[value="approve"]').click()
    await expect(page.locator("#status .state")).to_have_text("healthy", timeout=20_000)
    assert runner.sim.calls.count("up") == 2
