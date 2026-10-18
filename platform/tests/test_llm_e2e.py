"""M7a's success criterion with real models on real Docker: local Ollama tiers fix a
single-service typo (small) and a cross-service typo (large), and a fix that sets an
undeclared key waits for a person.

Skipped unless CAGENT_RUNNER points to the runner binary and CAGENT_LLM_E2E=1 (it needs the
models in platform/models.yaml served by a local Ollama). CI does not run it.
"""

import os
import subprocess
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.hub import RunnerHub, start
from cagent.llm import LiteLLMCompleter, Router, load_model_config
from cagent.orchestrator import Orchestrator, Settings
from cagent.spec import ClusterSpec

from .conftest import FIXTURES, REPO, REPO_CATALOG
from .test_hub_e2e import TOKEN, free_port

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ or os.environ.get("CAGENT_LLM_E2E") != "1",
    reason="set CAGENT_RUNNER and CAGENT_LLM_E2E=1 (needs local Ollama models)",
)


async def test_models_heal_typos_and_hold_an_undeclared_key_for_approval(tmp_path):
    catalog = load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")
    router = Router(load_model_config(REPO / "platform" / "models.yaml"), LiteLLMCompleter())
    name = f"m7-e2e-{int(time.time()) % 100000}"
    spec = ClusterSpec.model_validate(
        {
            "name": name,
            "purpose": "three services fail on configuration only a model can read from logs",
            "middleware": [
                {"template": "modecheck"},
                {"template": "cacheclient"},
                {"template": "redis"},
                {"template": "flagcheck"},
            ],
        }
    )
    hub_port = free_port()
    hub = RunnerHub({TOKEN: "e2e"})
    server = await start(hub, "127.0.0.1", hub_port)
    env = {**os.environ, "CAGENT_TOKEN": TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
    runner = subprocess.Popen(
        [os.environ["CAGENT_RUNNER"], "serve", "-hub", f"ws://127.0.0.1:{hub_port}/runner"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    approvals = []
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        o = Orchestrator(
            hub,
            catalog,
            InMemorySaver(),
            Settings(poll_interval_s=1, stability_window_s=5, startup_timeout_s=120),
            router=router,
        )
        status = await o.start("run-1", spec, "e2e")
        while status.waiting:
            kind = status.waiting["kind"]
            assert kind in ("approval", "patch_approval"), status.waiting
            if kind == "patch_approval":
                approvals.append(status.waiting)
            status = await o.resume("run-1", {"approve": True})

        assert status.outcome == "healthy"
        by_service = {f["signature"].split("@")[1]: f for f in status.fixes}
        assert by_service["modecheck"]["rule"].startswith("llm:")
        assert by_service["cacheclient"]["rule"] == "llm:large"
        assert status.compose["services"]["cacheclient"]["environment"]["CACHE_HOST"] == "redis"
        assert status.compose["services"]["modecheck"]["environment"]["APP_MODE"] == "server"
        assert [a["signature"] for a in approvals] == ["exit:1@flagcheck"]
        assert "FEATURE_FLAG is not a declared environment key" in approvals[0]["reasons"][0]
        for f in status.fixes:
            print(f"{f['rule']:10} {f['signature']:22} {f['reason']}")
    finally:
        await hub.call("e2e", "down", {"name": name})
        runner.terminate()
        runner.wait(timeout=10)
        server.close()
        await server.wait_closed()
