"""The harness on real Docker with the real Runner, when something dies mid-`up`.

The app waits for a dependency that turns healthy after 20 s, so `docker compose up` blocks
that long and the Runner reports `up` as its active call: the barrier both tests use. One
test kills the Runner there, the other kills the harness process.

Skipped unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from cagent.eval import evaluate
from cagent.eval.host import DockerHost
from cagent.eval.station import RunnerStation, is_runner
from cagent.orchestrator import Settings

from .conftest import REPO, REPO_CATALOG

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

SHORT = Settings(poll_interval_s=1, stability_window_s=3, startup_timeout_s=120)
SLOWDEP = {
    "name": "slowdep",
    "kind": "middleware",
    "provides": {"capability": "slowdep"},
    "image": "busybox:{version}",
    "command": [
        "sh",
        "-c",
        "sleep 20; mkdir -p /w && echo ok > /w/index.html && exec httpd -f -p 8080 -h /w",
    ],
    "versions": ["1.37"],
    "default_version": "1.37",
    "port": 8080,
    "healthcheck": {"cmd": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]},
    "presets": {"tiny": {"mem_limit": "64m", "cpus": 0.25}},
}
APP = {
    "name": "app",
    "kind": "app",
    "stack": "dockerfile",
    "sources": {"workdir": {"dockerfile": "Dockerfile"}},
    "runtime": {
        "ports": [{"name": "http", "container": 8080}],
        "env": {"defaults": {"MODE": "ok"}},
    },
    "requires": [{"capability": "slowdep", "bind": {"DEP_URL": "http://{{host}}:{{port}}/"}}],
}


def scenario(sid: str) -> dict:
    return {
        "id": sid,
        "kind": "repair",
        "category": "harness",
        "spec": {
            "purpose": "an app that waits for a slow dependency",
            "services": [{"name": "app", "source": {"kind": "workdir", "ref": "fixtures/tinyapp"}}],
            "middleware": [{"template": "slowdep"}],
        },
        "baseline": {"spec_patch": [{"op": "replace", "path": "/purpose", "value": "same"}]},
        "expect": {
            "runtime": [{"signature": "exit:1@app"}],
            "outcome": "healthy",
            "accept_rounds": 1,
        },
    }


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "scenarios"
    (r / "fixtures" / "catalog" / "middleware").mkdir(parents=True)
    (r / "fixtures" / "catalog" / "middleware" / "slowdep.yaml").write_text(yaml.safe_dump(SLOWDEP))
    app = r / "fixtures" / "tinyapp"
    (app / ".cagent" / "services").mkdir(parents=True)
    (app / "Dockerfile").write_text(
        'FROM busybox:1.37\nCMD ["sh", "-c", "mkdir -p /w && echo ok > /w/index.html && '
        'exec httpd -f -p 8080 -h /w"]\n'
    )
    (app / ".cagent" / "services" / "app.yaml").write_text(yaml.safe_dump(APP))
    for sid in ("first", "second"):
        (r / sid).mkdir()
        (r / sid / "scenario.yaml").write_text(yaml.safe_dump(scenario(sid)))
    return r


def freeze(root: Path, out: Path) -> Path:
    """An evaluation of two slots, frozen by hand: the harness tests need no git commit."""
    from cagent.eval.scenario import load_scenario

    slots = [
        {
            "id": f"t/{sid}/t0/1",
            "set": "t",
            "scenario": sid,
            "mode": "t0",
            "rep": 1,
            "purpose": "comparison",
        }
        for sid in ("first", "second")
    ]
    manifest = {
        "format": 1,
        "report_type": "comparison",
        "commit": "test",
        "dirty": True,
        "runner_sha256": "test",
        "platform": "test",
        "docker": {},
        "models": None,
        "settings": asdict(SHORT),
        "slots": slots,
        "scenarios": {
            sid: {
                "applicable": True,
                "reason": None,
                "files": load_scenario(root / sid, REPO_CATALOG).files(),
            }
            for sid in ("first", "second")
        },
    }
    d = out / "eval"
    evaluate.freeze(d, manifest, root)
    return d


def make_tools(root: Path, station=None):
    t = evaluate.make_tools(
        REPO,
        root,
        Path(os.environ["CAGENT_RUNNER"]),
        REPO / "platform" / "models.yaml",
        DockerHost(),
        {"models": None},
        settings=SHORT,
    )
    if station is not None:
        t.station = station
    return t


async def project_resources(project: str) -> dict:
    return await DockerHost().resources(project)


async def test_killing_the_runner_mid_up_is_investigated_cleaned_and_stops_the_run(root, tmp_path):
    d = freeze(root, tmp_path)
    runner = Path(os.environ["CAGENT_RUNNER"])
    killed: list[str] = []

    async def station(attempt_dir: Path):
        home = Path(tempfile.mkdtemp(prefix="cagent-eval-"))
        st = await RunnerStation.start(
            runner, home, [root / "fixtures"], attempt_dir / "runner.log"
        )
        if "/attempts/t/first/" in attempt_dir.as_posix():

            async def killer():
                while True:  # the barrier: the Runner is executing `up`
                    active = (await st.status()).get("active") or {}
                    if active.get("method") == "up":
                        os.killpg(st.pid, signal.SIGKILL)
                        killed.append(active["cluster"])
                        return
                    await asyncio.sleep(0.05)

            asyncio.get_running_loop().create_task(killer())
        return st

    lines: list[str] = []
    summary = await evaluate.run(d, root, REPO_CATALOG, make_tools(root, station), log=lines.append)

    assert len(killed) == 1
    assert lines[-1].startswith("stopped: t/first/t0/1 a1 needs investigating")
    result = json.loads(next((d / "attempts" / "t" / "first").rglob("result.json")).read_text())
    assert result["end"] == "runner_unavailable"
    cleanup = result["cleanup"]
    assert (cleanup["path"], cleanup["confirmed"], cleanup["events"]) == ("abnormal", True, [])
    assert await project_resources(f"cagent-{killed[0]}") == {
        "containers": [],
        "networks": [],
        "volumes": [],
    }
    assert not (d / "attempts" / "t" / "second").exists()  # no next slot started
    assert "t/first/t0/1: investigate" in summary["blocking"]


HARNESS = """
import asyncio, os
from pathlib import Path
from cagent.eval import evaluate
from cagent.eval.host import DockerHost
from cagent.orchestrator import Settings
root, repo = Path({root!r}), Path({repo!r})
short = Settings(poll_interval_s=1, stability_window_s=3, startup_timeout_s=120)
tools = evaluate.make_tools(repo, root, Path(os.environ["CAGENT_RUNNER"]),
                            repo / "platform" / "models.yaml", DockerHost(), {{"models": None}},
                            settings=short)
asyncio.run(evaluate.run(Path({d!r}), root, repo / "catalog", tools))
"""


async def test_killing_the_harness_mid_up_leaves_an_unfinished_attempt_that_resume_recovers(
    root, tmp_path
):
    d = freeze(root, tmp_path)
    script = tmp_path / "harness.py"
    script.write_text(HARNESS.format(root=str(root), repo=str(REPO), d=str(d)))
    harness = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    evidence = None
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:  # the barrier: `up` sent, a container exists
            found = (
                list((d / "attempts").rglob("evidence.jsonl")) if (d / "attempts").exists() else []
            )
            if found:
                records = [json.loads(ln) for ln in found[0].read_text().splitlines()]
                ups = [r for r in records if r["kind"] == "start" and r.get("method") == "up"]
                setup = next(r for r in records if r["kind"] == "setup")
                project = f"cagent-{setup['cluster']}"
                if ups and (await project_resources(project))["containers"]:
                    evidence = found[0]
                    break
            await asyncio.sleep(0.2)
        assert evidence is not None, "the harness never reached the barrier"
        harness.send_signal(signal.SIGKILL)
        harness.wait(timeout=10)
    finally:
        if harness.poll() is None:
            harness.kill()

    records = [json.loads(ln) for ln in evidence.read_text().splitlines()]
    station = next(r for r in records if r["kind"] == "station")
    up = next(r for r in records if r["kind"] == "start" and r.get("method") == "up")
    assert not any(r.get("call") == up["seq"] for r in records)  # the up call stays unfinished
    assert not (evidence.parent / "result.json").exists()
    assert is_runner(station["pid"])  # the Runner outlived the harness
    assert (await project_resources(project))["containers"]

    lines: list[str] = []
    summary = await evaluate.run(d, root, REPO_CATALOG, make_tools(root), log=lines.append)
    recovered = json.loads((evidence.parent / "recovered.json").read_text())
    assert recovered["confirmed"] is True and recovered["group_gone"] is True
    assert not is_runner(station["pid"])
    assert await project_resources(project) == {"containers": [], "networks": [], "volumes": []}
    assert "t/first/t0/1: void" in summary["blocking"]  # never counted as a result
    assert any(ln.startswith("t/second/t0/1 a1:") for ln in lines)  # the next slot ran now
