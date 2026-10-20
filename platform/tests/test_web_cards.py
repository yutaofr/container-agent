"""Cards: each pending kind rendered and answered from its form, errors shown in place."""

import json
import re

from cagent.orchestrator import Status

from .test_api import ORIGIN, client
from .test_runs_commands import ORDERS_WITHOUT_ENV, answer
from .test_runs_recovery import LosesUpOnce
from .test_web import html_login, page_csrf
from .test_web_run import new_run

EVIL = '<script>alert(1)</script><b hx-get="/logout">x</b>'


def card_fields(page: str) -> dict[str, str]:
    article = re.search(r'<article class="card" id="card-.*?</article>', page, re.S).group(0)
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', article))


async def headers(c):
    return {"Origin": ORIGIN, "X-CSRF-Token": page_csrf((await c.get("/")).text)}


async def send(c, run_id, fields, **extra):
    return await c.post(
        f"/runs/{run_id}/commands", data={**fields, **extra}, headers=await headers(c)
    )


async def put_pending(web, run_id, kind, payload, compose=None):
    """Make the run wait on a pending item of any kind, as an execution would."""
    async with web.store.tx() as t:
        await t.update_run(run_id, state="running", pending=None, execution_id="x")
    status = Status(None, {"kind": kind, **payload}, compose, [], "i1")
    await web.service._finished(run_id, "x", ("alice", "c0"), status)


async def test_a_question_is_answered_from_its_card(web):
    async with client(web) as c:
        await html_login(c)
        r = await c.post(
            "/runs",
            data={
                "command_id": "n1",
                "runner": "laptop-1",
                "cluster": "orders",
                "input": "spec",
                "spec": json.dumps(ORDERS_WITHOUT_ENV),
                "purpose": "",
            },
            headers=await headers(c),
        )
        await web.settle()
        run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
        page = (await c.get(f"/runs/{run_id}")).text
        assert "SPRING_PROFILES_ACTIVE" in page and 'type="password"' in page
        assert "Use the default" not in page  # this question has none
        r = await send(c, run_id, card_fields(page), choice="answer", answer="dev")
        assert r.status_code == 202 and "Accepted" in r.text
        await web.settle()
        assert 'data-kind="approval"' in (await c.get(f"/runs/{run_id}")).text


async def test_a_hidden_default_never_reaches_the_page(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        gap = {
            "kind": "missing_env",
            "target": "app",
            "message": "app: missing DB_PASSWORD",
            "options": [],
            "default": "hunter2",
            "key": "DB_PASSWORD",
        }
        await put_pending(
            web,
            run_id,
            "question",
            {
                "gap": gap,
                "question": "Set DB_PASSWORD.",
                "options": [],
                "default": "hunter2",
                "error": None,
            },
        )
        page = (await c.get(f"/runs/{run_id}")).text
        assert "hunter2" not in page and "Use the default (hidden)" in page
        r = await send(c, run_id, card_fields(page), choice="default")
        assert r.status_code == 202
        rec = await web.service.record("alice", run_id, 0, limit=1000)
        decided = [e for e in rec["entries"] if e.get("type") == "decided"][-1]
        assert decided["decision"] == {"action": "default"}


async def test_an_approval_takes_the_checked_exceptions(web):
    web.sim.findings = [{"service": "redis", "rule": "cap-add", "detail": EVIL}]
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        page = (await c.get(f"/runs/{run_id}")).text
        assert 'value="redis:cap-add">' in page  # offered, not pre-checked
        assert EVIL not in page and "&lt;script&gt;" in page
        r = await send(c, run_id, card_fields(page), choice="approve", allow="redis:cap-add")
        assert r.status_code == 202
        await web.settle()
        assert (await web.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_a_second_answer_to_one_card_is_a_conflict_shown_in_the_card(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        fields = card_fields((await c.get(f"/runs/{run_id}")).text)
        first = await send(c, run_id, fields, choice="approve")
        again = await send(c, run_id, fields, choice="approve")  # the same command: a replay
        assert first.status_code == again.status_code == 202
        other = await send(c, run_id, {**fields, "command_id": "another"}, choice="approve")
        assert other.status_code == 409 and "already answered" in other.text
        assert other.headers["hx-retarget"] == f"#error-{fields['pending_id']}"
        assert other.headers["hx-reswap"] == "innerHTML"
        assert other.headers["hx-trigger"] == "cagent:stale"


async def test_a_form_without_a_valid_choice_is_refused_in_place(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        fields = card_fields((await c.get(f"/runs/{run_id}")).text)
        r = await send(c, run_id, fields, choice="delete-everything")
        assert r.status_code == 422 and "Choose one of the card&#39;s buttons." in r.text
        bad = await send(c, run_id, fields, choice="approve", allow="redis:privileged")
        assert bad.status_code == 422 and bad.headers["hx-retarget"].startswith("#error-")
        assert (await web.service.snapshot("alice", run_id))["state"] == "waiting"


async def test_a_patch_without_a_preview_cannot_be_approved_from_the_card(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await answer(web, run_id, {"approve": True})
        await put_pending(
            web,
            run_id,
            "patch_approval",
            {
                "signature": "s",
                "tier": "large",
                "root_cause": EVIL,
                "patch": [{"op": "remove", "path": "/services/nope"}],
                "reasons": ["r"],
            },
            compose={"services": {"redis": {"image": "redis"}}},
        )
        page = (await c.get(f"/runs/{run_id}")).text
        assert "cannot be approved" in page
        assert re.search(r'value="approve" disabled>Approve patch', page)
        assert EVIL not in page


async def test_a_patch_preview_shows_absent_values(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await answer(web, run_id, {"approve": True})
        patch = [{"op": "add", "path": "/services/redis/environment", "value": {"A": "1"}}]
        await put_pending(
            web,
            run_id,
            "patch_approval",
            {"signature": "s", "tier": "large", "root_cause": "r", "patch": patch, "reasons": []},
            compose={"services": {"redis": {"image": "redis"}}},
        )
        page = (await c.get(f"/runs/{run_id}")).text
        assert "(absent)" in page and "{&#34;A&#34;: &#34;1&#34;}" in page


async def test_an_escalation_explains_what_a_retry_does(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await put_pending(
            web, run_id, "escalation", {"reason": "init_failed", "script": "01-schema.sql"}
        )
        page = (await c.get(f"/runs/{run_id}")).text
        assert "may run init again" in page and "01-schema.sql" in page
        r = await send(c, run_id, card_fields(page), choice="abort")
        assert r.status_code == 202


async def test_save_descriptor_files_download_only_from_their_card(web):
    async with client(web) as c, client(web) as bob:
        await html_login(c)
        await html_login(bob, "b" * 32)
        run_id = await new_run(web, c)
        files = {".cagent/services/shop.yaml": "name: shop\n"}
        await put_pending(
            web,
            run_id,
            "save_descriptors",
            {"service": "shop", "workdir": "/w/shop", "files": files, "note": EVIL, "error": None},
        )
        page = (await c.get(f"/runs/{run_id}")).text
        assert "Runner <b>laptop-1</b>" in page and EVIL not in page
        pending_id = card_fields(page)["pending_id"]
        url = f"/runs/{run_id}/pendings/{pending_id}/files/0"
        r = await c.get(url)
        assert r.text == "name: shop\n" and r.headers["content-type"].startswith("text/plain")
        assert r.headers["content-disposition"] == 'attachment; filename="0-shop.yaml"'
        assert r.headers["cache-control"] == "no-store"
        assert (await c.get(f"/runs/{run_id}/pendings/{pending_id}/files/1")).status_code == 404
        assert (await bob.get(url)).status_code == 404


async def test_the_reconcile_panel_and_a_recovery_card(web):
    web.sim.__class__ = LosesUpOnce  # the first up starts the cluster and loses its answer
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await answer(web, run_id, {"approve": True})
        page = (await c.get(f"/runs/{run_id}")).text
        assert "Reconcile this run" in page
        assert re.search(
            r'value="review_recovery">\s*<input[^>]*>\s*<button type="submit" disabled', page
        )
        panel = dict(re.findall(r'name="(command_id|expected_revision)" value="([^"]*)"', page))
        refresh = {
            "command_id": "rf1",
            "action": "refresh",
            "expected_revision": panel["expected_revision"],
        }
        assert (await send(c, run_id, refresh)).status_code == 202
        review = {
            "command_id": "rv1",
            "action": "review_recovery",
            "expected_revision": panel["expected_revision"],
        }
        assert (await send(c, run_id, review)).status_code == 202
        page = (await c.get(f"/runs/{run_id}")).text
        assert "Recovery plan" in page and "Approve recovery" in page
        r = await send(c, run_id, card_fields(page), choice="approve")
        assert r.status_code == 202
        await web.settle()
        assert (await web.service.snapshot("alice", run_id))["state"] == "healthy"
