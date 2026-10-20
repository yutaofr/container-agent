"""The run page: four regions from one read, the state fragment, escaping, and ended runs."""

import json
import re
import uuid

from .runharness import REDIS
from .test_api import ORIGIN, client
from .test_runs_commands import answer
from .test_web import html_login, page_csrf


async def new_run(web, c, cluster="demo"):
    headers = {"Origin": ORIGIN, "X-CSRF-Token": page_csrf((await c.get("/")).text)}
    fields = {"command_id": uuid.uuid4().hex, "runner": "laptop-1", "cluster": cluster}
    fields |= {"input": "spec", "spec": json.dumps(REDIS), "purpose": ""}
    r = await c.post("/runs", data=fields, headers=headers)
    await web.settle()
    return r.headers["hx-redirect"].rsplit("/", 1)[1]


def region(html: str, name: str) -> str:
    return re.search(rf'<section id="{name}".*?</section>', html, re.S).group(0)


async def test_the_page_shows_four_regions_from_one_read(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        page = (await c.get(f"/runs/{run_id}")).text
        snap = await web.service.snapshot("alice", run_id)
        assert f'data-event-seq="{snap["event_seq"]}"' in page
        assert f'data-pending-id="{snap["pending"]["pending_id"]}"' in page
        assert "waiting" in region(page, "status") and "No observation yet." in page
        assert 'id="record"' in page


async def test_the_state_fragment_is_one_read_and_never_counts_as_use(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        session = web.sessions.get(c.cookies["cagent_session"])
        seen = session.last_seen
        r = await c.get(f"/runs/{run_id}/fragments/state")
        assert r.status_code == 200 and r.text.lstrip().startswith("<template")
        snap = await web.service.snapshot("alice", run_id)
        assert r.headers["cagent-event-seq"] == str(snap["event_seq"])
        assert r.headers["cagent-revision"] == str(snap["revision"])
        assert r.headers["cache-control"] == "no-store"
        assert session.last_seen == seen


async def test_other_users_and_strangers_get_nothing(web):
    async with client(web) as alice, client(web) as bob, client(web) as nobody:
        await html_login(alice)
        await html_login(bob, "b" * 32)
        run_id = await new_run(web, alice)
        assert (await bob.get(f"/runs/{run_id}")).status_code == 404
        assert (await bob.get(f"/runs/{run_id}/fragments/state")).status_code == 404
        assert (await nobody.get(f"/runs/{run_id}")).status_code == 303
        assert (await nobody.get(f"/runs/{run_id}/fragments/state")).status_code == 401


async def test_log_lines_and_model_text_are_escaped(web):
    evil = '<script>alert(1)</script><a hx-get="/logout" href=x>'
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        observation = {
            "received_at": 1.0,
            "seq": 1,
            "services": [
                {
                    "service": "redis",
                    "role": None,
                    "state": "exited",
                    "health": None,
                    "restart_count": 2,
                    "exit_code": 1,
                    "errors": [evil],
                    "tail": [evil],
                }
            ],
        }
        attempt = {"at": 2.0, "ok": False, "reason": evil}
        async with web.store.tx() as t:
            await t.update_run(
                run_id, last_observation=observation, last_observation_attempt=attempt
            )
        page = (await c.get(f"/runs/{run_id}")).text
        assert evil not in page and "<script>alert" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
        assert 'hx-get="/logout"' not in page
        assert "The latest collection failed" in page


async def test_an_ended_run_offers_cleanup_only_while_no_other_run_uses_the_cluster(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        await answer(web, run_id, {"approve": True})
        page = (await c.get(f"/runs/{run_id}")).text
        assert "Nothing watches this cluster afterwards." in page
        assert "cagent-runner down -name demo" in page and "Runner <b>laptop-1</b>" in page
        await new_run(web, c)  # a new run on the same cluster
        page = (await c.get(f"/runs/{run_id}")).text
        assert "cagent-runner down" not in page and "another active run" in page
