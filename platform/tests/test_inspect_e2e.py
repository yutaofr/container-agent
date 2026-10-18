"""M7b's acceptance on real Docker: an unregistered two-module project is inspected by the real
Runner, gets a drafted descriptor, and builds and runs once the user saves it.

The model is scripted, so this runs in CI; test_llm_e2e.py asks real models. Skipped unless
CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.hub import RunnerHub, start
from cagent.llm import Router
from cagent.orchestrator import Orchestrator, Settings
from cagent.spec import ClusterSpec

from .conftest import REPO_CATALOG
from .fakellm import CONFIG, FakeCompleter
from .test_hub_e2e import TOKEN, free_port

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

# The deployable module has its own Dockerfile; the library module has none.
PROJECT = {
    "pom.xml": "<project><modules><module>api</module><module>lib</module></modules></project>",
    "api/pom.xml": "<project><artifactId>api</artifactId></project>",
    "lib/pom.xml": "<project><artifactId>lib</artifactId></project>",
    "api/Dockerfile": (
        "FROM busybox:1.37\n"
        "RUN mkdir -p /www && echo ok > /www/index.html\n"
        'CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]\n'
    ),
}
RUNTIME = {
    "ports": [{"name": "http", "container": 8080}],
    # busybox has wget, not the curl an HTTP healthcheck uses
    "health_cmd": ["CMD-SHELL", "wget -q -O /dev/null http://localhost:8080/"],
    "probe_paths": ["/"],
}


async def test_unregistered_project_is_drafted_saved_built_and_run(tmp_path):
    root = tmp_path / "projects"
    project = root / "shop"
    for rel, body in PROJECT.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(body)
    name = f"m7b-e2e-{int(time.time()) % 100000}"
    spec = ClusterSpec.model_validate(
        {
            "name": name,
            "purpose": "run the shop api from its working directory",
            "services": [{"name": "api", "source": {"kind": "workdir", "ref": str(project)}}],
        }
    )
    fake = FakeCompleter({"fake/large": [RUNTIME]})
    hub_port = free_port()
    hub = RunnerHub({TOKEN: "e2e"})
    server = await start(hub, "127.0.0.1", hub_port)
    env = {**os.environ, "CAGENT_TOKEN": TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
    runner = subprocess.Popen(
        [
            os.environ["CAGENT_RUNNER"],
            "serve",
            "-hub",
            f"ws://127.0.0.1:{hub_port}/runner",
            "-workdir-root",
            str(root),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tag = None
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        o = Orchestrator(
            hub,
            load_catalog(REPO_CATALOG),
            InMemorySaver(),
            Settings(poll_interval_s=1, stability_window_s=5, startup_timeout_s=120),
            router=Router(CONFIG, fake),
        )
        status = await o.start("run-1", spec, "e2e")
        assert status.waiting["kind"] == "save_descriptors", status.waiting
        [(path, text)] = status.waiting["files"].items()
        assert path == ".cagent/services/api.yaml"
        assert "dockerfile: api/Dockerfile" in text

        Path(project, path).parent.mkdir(parents=True)
        Path(project, path).write_text(text)  # what the user does after reviewing the draft
        status = await o.resume("run-1", {"saved": True})
        assert status.waiting["kind"] == "approval", status.waiting

        status = await o.resume("run-1", {"approve": True})
        assert status.outcome == "healthy", status.waiting
        tag = status.compose["services"]["api"]["image"]
    finally:
        await hub.call("e2e", "down", {"name": name})
        runner.terminate()
        runner.wait(timeout=10)
        server.close()
        await server.wait_closed()
        if tag:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
