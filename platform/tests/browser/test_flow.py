"""The whole flow from the browser: create a run, answer its question, approve it, watch it
become healthy, and read the record."""

import json

from playwright.async_api import Page, expect

from ..test_runs_commands import ORDERS_WITHOUT_ENV
from .conftest import RUN_URL, log_in


async def test_a_user_runs_the_whole_flow_from_the_browser(page: Page, running):
    console = await log_in(page, running.base)
    await page.click("text=New run")
    await page.fill('input[name="cluster"]', "orders")
    await page.check('input[name="input"][value="spec"]')
    await page.fill('textarea[name="spec"]', json.dumps(ORDERS_WITHOUT_ENV))
    await page.click('button:has-text("Create run")')
    await page.wait_for_url(RUN_URL)

    card = page.locator('#pending article[data-kind="question"]')
    await expect(card).to_contain_text("SPRING_PROFILES_ACTIVE")
    await card.locator('input[name="answer"]').fill("dev")
    await card.locator('button[value="answer"]').click()

    approval = page.locator('#pending article[data-kind="approval"]')
    await expect(approval).to_be_visible(timeout=10_000)
    await approval.locator('button[value="approve"]').click()

    await expect(page.locator("#status .state")).to_have_text("healthy", timeout=20_000)
    await expect(page.locator("#status")).to_contain_text("Nothing watches this cluster")
    await expect(page.locator("#summary")).to_contain_text("order-service")
    record = page.locator("ol.record")
    await expect(record).to_contain_text("The agent asked for a question.")
    await expect(record).to_contain_text("answered (hidden).")  # a missing_env answer
    await expect(record).to_contain_text("approved.")
    await expect(record.locator('.command[data-status="accepted"]')).to_have_count(0)
    await expect(record.locator(".command").first).to_have_text("Carried out.")
    assert console.errors == []
