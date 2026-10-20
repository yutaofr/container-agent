"""Races and recovery in the browser, each arranged step by step: two tabs on one item, a late
POST answer, a session ending mid-request, lost events, and a page opened after the end."""

import asyncio

from playwright.async_api import Page, Route, expect

from ..serverharness import REDIS
from ..test_runs_commands import ORDERS_WITHOUT_ENV
from .conftest import create_run, log_in


async def card(page: Page, kind: str):
    c = page.locator(f'#pending article[data-kind="{kind}"]:not([data-stale])')
    await expect(c).to_be_visible(timeout=10_000)
    return c


class Hold:
    """Holds matching requests' responses until released (the server has handled them)."""

    def __init__(self):
        self.release = asyncio.Event()
        self.held = asyncio.Event()

    async def __call__(self, route: Route):
        response = await route.fetch()
        self.held.set()
        await self.release.wait()
        await route.fulfill(response=response)


async def decided(running, run_id: str) -> list[dict]:
    rec = await running.platform.service.record("alice", run_id, 0, limit=1000)
    return [e for e in rec["entries"] if e.get("type") == "decided"]


async def test_two_tabs_answer_one_item_once(page: Page, running):
    await log_in(page, running.base)
    run_id = await create_run(page, running.base, ORDERS_WITHOUT_ENV, "orders")
    other = await page.context.new_page()
    await other.goto(page.url)
    stuck = Hold()  # the second tab hears nothing new
    await other.route("**/fragments/state", stuck)
    await (await card(other, "question")).locator('input[name="answer"]').fill("prod")

    first = await card(page, "question")
    await first.locator('input[name="answer"]').fill("dev")
    await first.locator('button[value="answer"]').click()
    await card(page, "approval")

    second = other.locator('#pending article[data-kind="question"]')
    await second.locator('button[value="answer"]').click()
    await expect(second).to_contain_text("This item changed", timeout=10_000)
    await expect(second.locator('input[name="answer"]')).to_have_value("prod")  # input kept
    assert len(await decided(running, run_id)) == 1
    stuck.release.set()
    await other.unroute_all(behavior="ignoreErrors")
    await other.close()


async def test_a_late_answer_never_replaces_a_newer_card(page: Page, running):
    await log_in(page, running.base)
    await create_run(page, running.base, ORDERS_WITHOUT_ENV, "orders")
    late = Hold()
    await page.route("**/commands", late)
    question = await card(page, "question")
    await question.locator('input[name="answer"]').fill("dev")
    await question.locator('button[value="answer"]').click()
    await late.held.wait()  # the server answered; the page has not seen it
    approval = await card(page, "approval")  # the event stream brought the next card
    late.release.set()
    await asyncio.sleep(0.5)
    await expect(approval).to_be_visible()
    await expect(page.locator("#pending article.accepted")).to_have_count(0)
    await page.unroute_all(behavior="ignoreErrors")


async def test_a_session_that_ends_mid_request_keeps_input_and_ignores_the_answer(
    page: Page, running
):
    await log_in(page, running.base)
    await create_run(page, running.base, ORDERS_WITHOUT_ENV, "orders")
    late = Hold()
    await page.route("**/commands", late)
    question = await card(page, "question")
    await question.locator('input[name="answer"]').fill("dev")
    await question.locator('button[value="answer"]').click()
    await late.held.wait()
    csrf = await page.evaluate(
        "JSON.parse(document.body.getAttribute('hx-headers'))['X-CSRF-Token']"
    )
    r = await page.request.post(
        f"{running.base}/logout", headers={"Origin": running.base, "X-CSRF-Token": csrf}
    )
    assert r.ok
    await expect(page.locator(".banner")).to_contain_text("Your session ended", timeout=10_000)
    late.release.set()
    await asyncio.sleep(0.5)
    await expect(page.locator('#pending input[name="answer"]')).to_have_value("dev")
    await expect(page.locator("#pending article.accepted")).to_have_count(0)
    await page.unroute_all(behavior="ignoreErrors")


async def test_lost_events_are_recovered_by_resync(page: Page, running):
    await log_in(page, running.base)
    run_id = await create_run(page, running.base, REDIS)
    await card(page, "approval")
    running.platform.store.keep_events = 3  # a test hook: trim hard
    await page.context.set_offline(True)  # the page misses everything from here
    service = running.platform.service
    snap = await service.snapshot("alice", run_id)
    body = {
        "command_id": "api-1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    await service.submit("alice", run_id, body)
    async with asyncio.timeout(20):
        while (await service.snapshot("alice", run_id))["state"] != "healthy":
            await asyncio.sleep(0.05)
    assert (await service.snapshot("alice", run_id))["events_pruned_through"] > 0
    await page.context.set_offline(False)
    await expect(page.locator("#status .state")).to_have_text("healthy", timeout=20_000)
    await expect(page.locator("#summary")).to_contain_text("redis")
    await expect(page.locator("#pending article")).to_have_count(0)


async def test_a_page_opened_after_the_end_loads_the_whole_record(page: Page, running):
    await log_in(page, running.base)
    run_id = await create_run(page, running.base, REDIS)
    approval = await card(page, "approval")
    await approval.locator('button[value="approve"]').click()
    await expect(page.locator("#status .state")).to_have_text("healthy", timeout=20_000)
    fresh = await page.context.new_page()
    await fresh.goto(f"{running.base}/runs/{run_id}")  # no event will come any more
    record = fresh.locator("ol.record")
    await expect(record).to_contain_text("approved.")
    await expect(record).to_contain_text("The run is healthy.")
    await expect(record.locator(".command").first).to_have_text("Carried out.")
