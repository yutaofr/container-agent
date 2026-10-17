"""M6's success criterion on real Docker: the orchestrator heals a port conflict and an OOM
kill with T0 rules alone, through the real hub and the real Runner.

Skipped unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import os
import socket
import subprocess
import time

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.hub import RunnerHub, start
from cagent.orchestrator import Orchestrator, Settings
from cagent.spec import ClusterSpec

from .conftest import FIXTURES, REPO_CATALOG
from .test_hub_e2e import TOKEN, free_port

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)


async def test_real_cluster_heals_a_port_conflict_and_an_oom_kill(tmp_path):
    catalog = load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")
    taken = socket.socket()
    taken.bind(("", 0))  # all interfaces, as the Runner's port check binds
    taken.listen()
    port = taken.getsockname()[1]
    name = f"m6-e2e-{int(time.time()) % 100000}"
    spec = ClusterSpec.model_validate(
        {
            "name": name,
            "purpose": "memhog needs ~160 MB but its preset gives 64m; redis wants a taken port",
            "middleware": [
                {"template": "memhog", "preset": "tiny"},
                {"template": "redis", "publish": port},
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
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        o = Orchestrator(
            hub,
            catalog,
            InMemorySaver(),
            Settings(poll_interval_s=1, stability_window_s=5, startup_timeout_s=120),
        )

        status = await o.start("run-1", spec, "e2e")
        assert status.waiting["kind"] == "approval"
        published = yaml.safe_load(status.waiting["compose"])["services"]["redis"]["ports"]
        assert published == [f"{port + 1000}:6379"], "the port conflict is fixed before approval"

        status = await o.resume("run-1", {"approve": True})
        assert status.outcome == "healthy", status.waiting
        rules = [f["rule"] for f in status.fixes]
        assert rules[0] == "port_conflict" and "oom_killed" in rules, rules
        # Re-applying the OOM fix must not see redis's own published port as a new conflict.
        assert rules.count("port_conflict") == 1, rules
        assert status.compose["services"]["memhog"]["mem_limit"] != "64m"
    finally:
        await hub.call("e2e", "down", {"name": name})
        runner.terminate()
        runner.wait(timeout=10)
        server.close()
        await server.wait_closed()
        taken.close()
