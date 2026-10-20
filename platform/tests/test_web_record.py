"""Record fragments: entries after a cursor, decisions with their command's result, gaps,
masked answers, and escaping."""

import re

from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_api import client
from .test_runs_commands import answer
from .test_web import html_login
from .test_web_cards import EVIL, put_pending
from .test_web_run import new_run


def seqs(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'data-seq="(\d+)"', html)]


async def test_the_record_pages_from_a_cursor_with_its_headers(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await answer(web, run_id, {"approve": True})
        r = await c.get(f"/runs/{run_id}/fragments/record?after=0")
        assert r.status_code == 200 and r.text.lstrip().startswith("<template")
        got = seqs(r.text)
        assert got == sorted(got) and got[0] == 1
        assert r.headers["cagent-record-after"] == str(got[-1])
        assert r.headers["cagent-record-more"] == "false"
        assert "The agent asked for the compose file to approve." in r.text
        assert "alice" in r.text and "approved." in r.text and "Carried out." in r.text
        later = await c.get(f"/runs/{run_id}/fragments/record?after={got[-1]}")
        assert seqs(later.text) == [] and later.headers["cagent-record-after"] == str(got[-1])


async def test_an_open_command_is_marked_and_its_result_can_be_fetched(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        snap = await web.service.snapshot("alice", run_id)
        body = {
            "command_id": "a1",
            "expected_revision": snap["revision"],
            "action": "answer",
            "pending_id": snap["pending"]["pending_id"],
            "decision": {"approve": True},
        }
        await web.service.submit("alice", run_id, body)  # accepted, not yet finished
        html = (await c.get(f"/runs/{run_id}/fragments/record")).text
        assert 'data-command-id="a1" data-status="accepted"' in html
        await web.settle()
        done = await c.get(f"/runs/{run_id}/fragments/command/a1")
        assert 'data-status="done"' in done.text and "Carried out." in done.text
        assert (await c.get(f"/runs/{run_id}/fragments/command/nope")).status_code == 404


async def test_hidden_answers_and_model_text_stay_hidden_and_escaped(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        gap = {
            "kind": "missing_env",
            "target": "app",
            "message": "m",
            "options": [],
            "default": None,
            "key": "DB_PASSWORD",
        }
        await put_pending(
            web,
            run_id,
            "question",
            {"gap": gap, "question": EVIL, "options": [], "default": None, "error": None},
        )
        snap = await web.service.snapshot("alice", run_id)
        await answer(web, run_id, {"answer": "s3cret-value"}, command_id="q1")
        html = (await c.get(f"/runs/{run_id}/fragments/record")).text
        assert "s3cret-value" not in html and "answered (hidden)" in html
        assert EVIL not in html and "&lt;script&gt;" in html
        assert snap["pending"]["kind"] == "question"


async def test_a_trimmed_record_says_so_and_keeps_every_decision(tmp_path, catalog):
    from cagent.api import create_app
    from cagent.auth import Sessions

    from .test_api import ORIGIN, TOKENS

    store = await Store.open(str(tmp_path / "small.db"), keep_events=2)
    web = Harness(store, SimCluster(starting_snapshots=3), catalog)
    web.sessions = Sessions(TOKENS, ttl_s=3600, idle_ttl_s=600)
    web.app = create_app(web.service, web.sessions, ORIGIN, secure_cookies=False)
    try:
        async with client(web) as c:
            await html_login(c)
            run_id = await new_run(web, c)
            await answer(web, run_id, {"approve": True})
            html = (await c.get(f"/runs/{run_id}/fragments/record?after=0")).text
            assert "Some earlier events were trimmed" in html
            assert html.count('class="entry asked"') == 1
            assert html.count('class="entry decided"') == 1
    finally:  # an open store would keep the test process from exiting
        await web.service.close()
        await store.close()


async def test_a_run_from_before_the_record_says_its_history_may_be_incomplete(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        assert "may be incomplete" not in (await c.get(f"/runs/{run_id}")).text
        async with web.store.tx() as t:
            await t.update_run(run_id, history="partial")
        assert "may be incomplete" in (await c.get(f"/runs/{run_id}")).text
