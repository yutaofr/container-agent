"""The HTTP interface: login, CSRF and Origin checks, access, and the run commands."""

import httpx
import pytest

from cagent.api import create_app
from cagent.auth import Sessions
from cagent.store import Store

from .runharness import REDIS, Harness
from .simcluster import SimCluster

ORIGIN = "http://127.0.0.1:8000"
TOKENS = {"alice": "a" * 32, "bob": "b" * 32}


@pytest.fixture
async def h(tmp_path, catalog):
    store = await Store.open(str(tmp_path / "cagent.db"))
    h = Harness(store, SimCluster(), catalog)
    h.sessions = Sessions(TOKENS, ttl_s=3600, idle_ttl_s=600)
    h.app = create_app(h.service, h.sessions, ORIGIN, secure_cookies=False)
    yield h
    await store.close()


def client(h) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=ORIGIN)


async def login(c: httpx.AsyncClient, token=TOKENS["alice"]) -> dict:
    csrf = (await c.get("/login")).json()["csrf"]
    r = await c.post("/login", data={"token": token, "csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    return {"Origin": ORIGIN, "X-CSRF-Token": r.json()["csrf"]}


async def test_login_sets_only_a_session_cookie(h):
    async with client(h) as c:
        r = await c.get("/login")
        assert "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        headers = await login(c)
        assert set(c.cookies.keys()) == {"cagent_session"}
        assert TOKENS["alice"] not in c.cookies["cagent_session"]
        assert (await c.get("/api/runs")).json() == []
        assert headers["X-CSRF-Token"]


@pytest.mark.parametrize(
    ("origin", "status"), [(None, 403), ("null", 403), ("http://evil.example", 403)]
)
async def test_login_needs_the_exact_origin(h, origin, status):
    async with client(h) as c:
        csrf = (await c.get("/login")).json()["csrf"]
        headers = {"Origin": origin} if origin else {}
        r = await c.post("/login", data={"token": TOKENS["alice"], "csrf": csrf}, headers=headers)
        assert r.status_code == status


async def test_login_needs_the_pre_session_and_the_token(h):
    async with client(h) as c:
        csrf = (await c.get("/login")).json()["csrf"]
        r = await c.post(
            "/login", data={"token": "x" * 32, "csrf": csrf}, headers={"Origin": ORIGIN}
        )
        assert (r.status_code, r.json()) == (401, {"error": "login_failed"})
    async with client(h) as c:  # a forged form without the victim's pre-session
        r = await c.post(
            "/login", data={"token": TOKENS["alice"], "csrf": csrf}, headers={"Origin": ORIGIN}
        )
        assert r.status_code == 401


async def test_everything_needs_a_session(h):
    async with client(h) as c:
        for method, path in [("GET", "/api/runs"), ("GET", "/api/runs/x"), ("POST", "/api/runs")]:
            r = await c.request(method, path, headers={"Origin": ORIGIN})
            assert (r.status_code, r.json()) == (401, {"error": "login_required"})


async def test_changes_need_the_csrf_header_and_the_origin(h):
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    async with client(h) as c:
        headers = await login(c)
        for bad in [
            {"Origin": ORIGIN},
            {**headers, "X-CSRF-Token": "x"},
            {"X-CSRF-Token": headers["X-CSRF-Token"]},
            {**headers, "Origin": "null"},
        ]:
            r = await c.post("/api/runs", json=body, headers=bad)
            assert r.status_code == 403, bad
        assert (await c.post("/api/runs", json=body, headers=headers)).status_code == 202


async def test_a_run_goes_from_creation_to_healthy_over_http(h):
    async with client(h) as c:
        headers = await login(c)
        body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
        r = await c.post("/api/runs", json=body, headers=headers)
        assert r.status_code == 202 and "cagent-replay" not in r.headers
        run_id = r.json()["run_id"]
        again = await c.post("/api/runs", json=body, headers=headers)
        assert again.status_code == 202 and again.headers["cagent-replay"] == "true"
        assert again.json() == r.json()
        await h.settle()

        snap = (await c.get(f"/api/runs/{run_id}")).json()
        command = {
            "command_id": "a1",
            "expected_revision": snap["revision"],
            "action": "answer",
            "pending_id": snap["pending"]["pending_id"],
            "decision": {"approve": True},
        }
        stale = {**command, "command_id": "a0", "expected_revision": 1}
        r = await c.post(f"/api/runs/{run_id}/commands", json=stale, headers=headers)
        assert r.status_code == 409 and r.json()["error"] == "stale"
        assert r.json()["snapshot"]["revision"] == snap["revision"]
        r = await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)
        assert r.status_code == 202
        await h.settle()
        assert (await c.get(f"/api/runs/{run_id}")).json()["state"] == "healthy"
        done = (await c.get(f"/api/runs/{run_id}/commands/a1")).json()
        assert done["status"] == "done"


async def test_malformed_commands_get_422(h):
    async with client(h) as c:
        headers = await login(c)
        r = await c.post("/api/runs", json={"command_id": "c1"}, headers=headers)
        assert (r.status_code, r.json()["error"]) == (422, "invalid_command")
        r = await c.post("/api/runs/x/commands", content=b"not json", headers=headers)
        assert r.status_code == 422


async def test_another_users_run_does_not_exist_for_them(h):
    async with client(h) as alice, client(h) as bob:
        a = await login(alice)
        b = await login(bob, TOKENS["bob"])
        body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
        run_id = (await alice.post("/api/runs", json=body, headers=a)).json()["run_id"]
        await h.settle()
        assert (await bob.get(f"/api/runs/{run_id}")).status_code == 404
        r = await bob.post(
            f"/api/runs/{run_id}/commands",
            json={"command_id": "x", "action": "abandon", "expected_revision": 2},
            headers=b,
        )
        assert r.status_code == 404
        assert (await bob.get("/api/runs")).json() == []


async def test_logout_ends_the_session(h):
    async with client(h) as c:
        headers = await login(c)
        session = h.sessions.get(c.cookies["cagent_session"])
        assert (await c.post("/logout", headers=headers)).status_code == 200
        assert session.revoked.is_set()
        c.cookies.set("cagent_session", session.id)  # replaying the old cookie
        assert (await c.get("/api/runs")).status_code == 401


async def test_the_run_record_is_served_to_its_owner(h):
    async with client(h) as alice, client(h) as bob:
        a = await login(alice)
        await login(bob, TOKENS["bob"])
        body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
        run_id = (await alice.post("/api/runs", json=body, headers=a)).json()["run_id"]
        await h.settle()
        rec = (await alice.get(f"/api/runs/{run_id}/record?after=0")).json()
        assert rec["history"] == "complete" and rec["entries"][0]["seq"] == 1
        assert any(e.get("type") == "published" for e in rec["entries"])
        assert (await bob.get(f"/api/runs/{run_id}/record")).status_code == 404
        r = await alice.get(f"/api/runs/{run_id}/record?after=-5")
        assert (r.status_code, r.json()["error"]) == (422, "bad_cursor")
