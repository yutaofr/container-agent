"""A platform.yaml for tests, and helpers to talk to a running platform over HTTP and SSE."""

import json
import socket
from pathlib import Path

import httpx
import yaml

from .conftest import FIXTURE_CATALOG, REPO_CATALOG

RUNNER_TOKEN = "r" * 40
ENV = {"CAGENT_RUNNER": RUNNER_TOKEN, "CAGENT_ALICE": "a" * 40, "CAGENT_BOB": "b" * 40}
REDIS = {"purpose": "a cache", "services": [], "middleware": [{"template": "redis"}]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp: Path, web_port: int, hub_port: int = 0, fast: bool = False) -> Path:
    doc = {
        "db": {"path": "data/cagent.db"},
        "catalog": [str(REPO_CATALOG), str(FIXTURE_CATALOG)],
        "hub": {"port": hub_port, "runners": {"laptop-1": {"token_env": "CAGENT_RUNNER"}}},
        "web": {"origin": f"http://127.0.0.1:{web_port}", "dev_http": True, "port": web_port},
        "users": {
            "alice": {"token_env": "CAGENT_ALICE", "runners": ["laptop-1"]},
            "bob": {"token_env": "CAGENT_BOB", "runners": ["laptop-1"]},
        },
    }
    if fast:
        doc["orchestrator"] = {"poll_interval_s": 0.05, "stability_window_s": 0.1}
    path = tmp / "platform.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


async def login(c: httpx.AsyncClient, user: str = "alice") -> dict[str, str]:
    csrf = (await c.get("/login")).json()["csrf"]
    origin = str(c.base_url).rstrip("/")
    token = ENV[f"CAGENT_{user.upper()}"]
    r = await c.post("/login", data={"token": token, "csrf": csrf}, headers={"Origin": origin})
    r.raise_for_status()
    return {"Origin": origin, "X-CSRF-Token": r.json()["csrf"]}


async def sse(response: httpx.Response):
    """Yield (id, event, data) from an event-stream response; heartbeats are skipped."""
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "":
            if "event" in fields:
                data = json.loads(fields.get("data", "{}"))
                yield fields.get("id"), fields["event"], data
            fields = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(": ")
            fields[key] = value
