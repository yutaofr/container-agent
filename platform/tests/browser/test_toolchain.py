"""The toolchain itself: an async page against a real server, a session cookie, and an
event stream in Chromium that sees a change made through the API."""

from playwright.async_api import Page

from ..serverharness import ENV, REDIS
from .conftest import fail_on_purpose


async def test_a_page_logs_in_and_hears_an_event(page: Page, running):
    api = page.request
    csrf = (await (await api.get(f"{running.base}/login")).json())["csrf"]
    r = await api.post(
        f"{running.base}/login",
        form={"token": ENV["CAGENT_ALICE"], "csrf": csrf},
        headers={"Origin": running.base},
    )
    assert r.ok
    headers = {"Origin": running.base, "X-CSRF-Token": (await r.json())["csrf"]}
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (
        await (await api.post(f"{running.base}/api/runs", data=body, headers=headers)).json()
    )["run_id"]

    await page.goto(f"{running.base}/api/runs/{run_id}")  # the browser carries the cookie
    await page.evaluate(
        """(url) => {
            window.seen = [];
            const es = new EventSource(url);
            es.addEventListener("run_state", (e) => {
                window.seen.push(JSON.parse(e.data).payload.state);
            });
        }""",
        f"{running.base}/api/runs/{run_id}/events?after=0",
    )
    await page.wait_for_function("window.seen.includes('waiting')", timeout=10_000)
    assert not fail_on_purpose(), "failing on purpose to check the artifacts"
