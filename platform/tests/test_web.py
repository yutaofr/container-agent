"""HTML pages: login, the run list, creating a run, security headers, static files."""

import hashlib
import json
import re

import httpx

from cagent.web import CSP, HTMX_VERSION

from .runharness import REDIS
from .test_api import ORIGIN, TOKENS, client, login

HTMX_SHA256 = "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"


def csrf_field(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def html_login(c: httpx.AsyncClient, token=TOKENS["alice"]) -> httpx.Response:
    page = await c.get("/login")
    return await c.post(
        "/login",
        data={"token": token, "csrf": csrf_field(page.text)},
        headers={"Origin": ORIGIN},
    )


def page_csrf(html: str) -> str:
    return json.loads(re.search(r"hx-headers='([^']+)'", html).group(1))["X-CSRF-Token"]


async def test_the_login_page_sets_a_pre_session_and_strict_headers(web):
    async with client(web) as c:
        r = await c.get("/login")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["cache-control"] == "no-store"
        cookie = r.headers["set-cookie"].lower()
        assert "cagent_pre=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        config = json.loads(
            re.search(r'name="htmx-config" content="([^"]+)"', r.text)
            .group(1)
            .replace("&#34;", '"')
        )
        assert config["allowEval"] is False and config["allowScriptTags"] is False
        assert config["includeIndicatorStyles"] is False and config["historyEnabled"] is False
        assert "<script>" not in r.text  # only <script src=...>
        assert not re.search(r"\son[a-z]+=", r.text)  # no inline event handlers


async def test_logging_in_from_the_form_redirects_home(web):
    async with client(web) as c:
        r = await html_login(c)
        assert (r.status_code, r.headers["location"]) == (303, "/")
        assert set(c.cookies.keys()) == {"cagent_session"}
        home = await c.get("/")
        assert home.status_code == 200 and "alice" in home.text and "No runs yet." in home.text


async def test_a_failed_login_issues_a_new_form_and_never_echoes_the_token(web):
    async with client(web) as c:
        first = csrf_field((await c.get("/login")).text)
        r = await c.post(
            "/login", data={"token": "wrong-" + "x" * 30, "csrf": first}, headers={"Origin": ORIGIN}
        )
        assert r.status_code == 401 and "Login failed." in r.text
        assert "wrong-" not in r.text
        second = csrf_field(r.text)
        assert second != first and "cagent_pre=" in r.headers["set-cookie"]
        r = await c.post(
            "/login", data={"token": TOKENS["alice"], "csrf": second}, headers={"Origin": ORIGIN}
        )
        assert r.status_code == 303


async def test_a_login_from_another_origin_is_refused(web):
    async with client(web) as c:
        csrf = csrf_field((await c.get("/login")).text)
        r = await c.post(
            "/login", data={"token": TOKENS["alice"], "csrf": csrf}, headers={"Origin": "null"}
        )
        assert r.status_code == 403 and "cagent_session" not in c.cookies


async def test_pages_send_a_visitor_without_a_session_to_login(web):
    async with client(web) as c:
        for path in ("/", "/runs/new"):
            r = await c.get(path)
            assert (r.status_code, r.headers["location"]) == (303, "/login")


async def test_the_new_run_form_lists_the_users_runners_and_a_fresh_command_id(web):
    async with client(web) as c:
        await html_login(c)
        first = (await c.get("/runs/new")).text
        second = (await c.get("/runs/new")).text
        assert "laptop-1 — offline" in first
        ids = [
            re.search(r'name="command_id" value="([0-9a-f]+)"', t).group(1) for t in (first, second)
        ]
        assert ids[0] != ids[1]


async def test_creating_a_run_from_the_form(web):
    async with client(web) as c:
        await html_login(c)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": page_csrf((await c.get("/")).text)}
        fields = {
            "command_id": "f1",
            "runner": "laptop-1",
            "cluster": "demo",
            "input": "spec",
            "spec": json.dumps(REDIS),  # JSON is YAML
            "purpose": "",
        }
        r = await c.post("/runs", data=fields, headers=headers)
        assert r.status_code == 204 and r.headers["hx-redirect"].startswith("/runs/")
        again = await c.post("/runs", data=fields, headers=headers)  # a retry of the same form
        assert again.headers["hx-redirect"] == r.headers["hx-redirect"]
        await web.settle()
        assert "demo" in (await c.get("/")).text

        busy = await c.post("/runs", data={**fields, "command_id": "f2"}, headers=headers)
        assert busy.status_code == 409 and "already has an active run" in busy.text
        bad = await c.post(
            "/runs", data={**fields, "command_id": "f3", "spec": "a: [b"}, headers=headers
        )
        assert bad.status_code == 422 and "not valid YAML" in bad.text
        no_csrf = await c.post("/runs", data=fields, headers={"Origin": ORIGIN})
        assert no_csrf.status_code == 403


async def test_logout_from_a_page_redirects_to_login(web):
    async with client(web) as c:
        await html_login(c)
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": page_csrf((await c.get("/")).text),
            "HX-Request": "true",
        }
        r = await c.post("/logout", headers=headers)
        assert r.headers["hx-redirect"] == "/login"
        assert (await c.get("/")).status_code == 303


async def test_static_files_are_the_pinned_ones_and_cacheable(web):
    async with client(web) as c:
        r = await c.get("/static/htmx.min.js")
        assert hashlib.sha256(r.content).hexdigest() == HTMX_SHA256
        assert r.headers.get("cache-control") != "no-store"
        assert (await c.get("/static/app.css")).status_code == 200
    assert HTMX_VERSION == "2.0.10"


async def test_the_programmatic_login_moved_to_the_api(web):
    async with client(web) as c:
        assert "csrf" in (await c.get("/api/login")).json()
        await login(c)
