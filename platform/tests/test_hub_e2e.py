"""The real Go Runner against the real hub: tool calls, then surviving a hub restart.

Skipped unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it. Needs the docker
CLI for snapshot.
"""

import os
import socket
import subprocess

import pytest

from cagent.generate import generate, to_yaml
from cagent.hub import RunnerError, RunnerHub, start

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

TOKEN = "e2e-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_runner_answers_over_the_hub_and_survives_a_restart(tmp_path, catalog, orders_spec):
    port = free_port()
    hub = RunnerHub({TOKEN: "e2e"})
    server = await start(hub, "127.0.0.1", port)
    env = {**os.environ, "CAGENT_TOKEN": TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
    runner = subprocess.Popen(
        [os.environ["CAGENT_RUNNER"], "serve", "-hub", f"ws://127.0.0.1:{port}/runner"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        assert {"validate", "up", "snapshot", "verify", "down", "build"} <= set(hub.tools("e2e"))

        # M2's generated compose passes the Runner's validation, sent as content over RPC.
        compose = to_yaml(generate(orders_spec, catalog))
        good = await hub.call("e2e", "validate", {"name": "orders-poc", "compose": compose})
        assert good.ok, good
        assert good.output["findings"] == [] and good.output["port_conflicts"] == []

        risky = "services:\n  web: { image: nginx:1.27, privileged: true }\n"
        bad = await hub.call("e2e", "validate", {"name": "risky", "compose": risky})
        assert bad.exit_code == 1 and bad.output["findings"][0]["rule"] == "privileged"

        snap = await hub.call("e2e", "snapshot", {"name": "e2e-none"})
        assert snap.ok and snap.output["services"] == []

        with pytest.raises(RunnerError):
            await hub.call("e2e", "down", {"name": "-rf"})

        # Restart the hub on the same port; the Runner must reconnect by itself.
        server.close()
        await server.wait_closed()
        hub = RunnerHub({TOKEN: "e2e"})
        server = await start(hub, "127.0.0.1", port)
        await hub.wait_for_runner("e2e", timeout=15)
        assert (await hub.call("e2e", "snapshot", {"name": "e2e-none"})).ok
    finally:
        runner.terminate()
        assert runner.wait(timeout=10) == 0, "runner did not shut down cleanly on SIGTERM"
        server.close()
        await server.wait_closed()
