"""Cards in the browser: each kind answered from the page, nothing injected ever runs, and
hidden values never reach the page."""

from playwright.async_api import Page, expect

from ..serverharness import REDIS
from .conftest import create_run, log_in, put_pending

EVIL = (
    '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>'
    '<button hx-post="/logout" hx-trigger="load">x</button>'
)


async def watch_requests(page: Page) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(f"{r.method} {r.url}"))
    return seen


async def wait_card(page: Page, kind: str):
    card = page.locator(f'#pending article[data-kind="{kind}"]')
    await expect(card).to_be_visible(timeout=10_000)
    return card


async def test_injected_text_never_runs_or_sends_requests(page: Page, running):
    console = await log_in(page, running.base)
    run_id = await create_run(page, running.base, REDIS)
    await wait_card(page, "approval")
    requests = await watch_requests(page)
    await put_pending(
        running,
        run_id,
        "patch_approval",
        {
            "signature": EVIL,
            "tier": "large",
            "root_cause": EVIL,
            "patch": [{"op": "add", "path": "/services/redis/environment", "value": {"A": EVIL}}],
            "reasons": [EVIL],
        },  # fmt: skip
        compose={"services": {"redis": {"image": "redis"}}},
    )
    card = await wait_card(page, "patch_approval")
    await expect(card).to_contain_text("onerror=")  # shown as text
    assert await page.evaluate("window.pwned") is None
    assert not [r for r in requests if "/logout" in r]
    assert console.errors == []


async def test_a_hidden_default_is_used_without_reaching_the_page(page: Page, running):
    await log_in(page, running.base)
    run_id = await create_run(page, running.base, REDIS)
    await wait_card(page, "approval")
    gap = {"kind": "missing_env", "target": "app", "message": "m", "options": [],
           "default": "hunter2-default", "key": "DB_PASSWORD"}  # fmt: skip
    await put_pending(
        running,
        run_id,
        "question",
        {
            "gap": gap,
            "question": "Set DB_PASSWORD.",
            "options": [],
            "default": "hunter2-default",
            "error": None,
        },  # fmt: skip
    )
    card = await wait_card(page, "question")
    assert "hunter2-default" not in await page.content()
    await card.locator('button[value="default"]').click()
    record = page.locator("ol.record")
    await expect(record).to_contain_text("took the default.", timeout=10_000)
    assert "hunter2-default" not in await page.content()


async def test_each_kind_of_card_is_answered_from_the_page(page: Page, running):
    console = await log_in(page, running.base)
    run_id = await create_run(page, running.base, REDIS)
    await wait_card(page, "approval")
    items = [
        ("save_descriptors",
         {"service": "shop", "workdir": "/w/shop", "note": "n", "error": None,
          "files": {".cagent/services/shop.yaml": "x: 1\n"}},
         "saved", "said the file was saved."),
        ("patch_approval",
         {"signature": "s", "tier": "large", "root_cause": "r",
          "patch": [{"op": "replace", "path": "/services/redis/image", "value": "redis:7.2"}],
          "reasons": ["undeclared key"]},
         "reject", "rejected."),
        ("escalation", {"reason": "init_failed", "script": "01-schema.sql", "log": ["boom"]},
         "abort", "aborted."),
    ]  # fmt: skip
    for kind, payload, choice, said in items:
        await put_pending(
            running, run_id, kind, payload, compose={"services": {"redis": {"image": "redis:7.4"}}}
        )
        card = await wait_card(page, kind)
        if kind == "escalation":
            await expect(card).to_contain_text("may run init again")
        if kind == "patch_approval":
            await expect(card.locator("table.preview")).to_contain_text('"redis:7.4"')
        await card.locator(f'button[value="{choice}"]').click()
        await expect(page.locator("ol.record")).to_contain_text(said, timeout=10_000)
    assert console.errors == []
