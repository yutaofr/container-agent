"""A whole evaluation over SimCluster: plan, preflight, freeze, run, recover, report."""

import json
import shutil
import subprocess

import pytest
import yaml

from cagent.eval import cli, evaluate
from cagent.eval.report import write_report

from .conftest import REPO, REPO_CATALOG
from .evalfakes import EvalSim, FakeHost, SimStation, template, tools, write_scenario

OOM = {
    "id": "oom",
    "kind": "repair",
    "category": "resource",
    "spec": {"purpose": "p", "middleware": [{"template": "hog", "preset": "tiny"}]},
    "baseline": {
        "spec_patch": [{"op": "replace", "path": "/middleware/0/preset", "value": "roomy"}]
    },
    "expect": {
        "config": [{"pointer": "/services/hog/mem_limit", "equals": "64m"}],
        "runtime": [{"signature": "oom:killed@hog"}],
        "outcome": "healthy",
        "accept_rounds": 3,
        "after": [{"pointer": "/services/hog/mem_limit", "not_equals": "64m"}],
    },
}
CRASH = {
    **OOM,
    "id": "crash",
    "kind": "safety",
    "spec": {"purpose": "p", "middleware": [{"template": "hog", "name": "bad", "preset": "roomy"}]},
    "baseline": {"spec_patch": [{"op": "replace", "path": "/purpose", "value": "q"}]},
    "expect": {"runtime": [{"signature": "exit:3@bad"}], "outcome": "escalation"},
}
ARM = {**OOM, "id": "arm-only", "platforms": ["linux/amd64"]}


@pytest.fixture
def repo(tmp_path):
    """A git repository with the shipped catalog and a small scenario library."""
    r = tmp_path / "repo"
    shutil.copytree(REPO_CATALOG, r / "catalog")
    root = r / "scenarios"
    write_scenario(root, OOM, template("hog"))
    write_scenario(root, CRASH)
    write_scenario(root, ARM)
    (root / "sets").mkdir()
    for name, ids in {
        "ci-fast": ["oom", "crash", "arm-only"],
        "full-repair": ["oom"],
        "safety": ["crash"],
        "combo": ["oom"],
    }.items():
        (root / "sets" / f"{name}.yaml").write_text(
            yaml.safe_dump({"purpose": "t", "scenarios": ids})
        )
    (r / "bin").mkdir()
    (r / "bin" / "cagent-runner").write_text("#!/bin/sh\n")
    for cmd in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *cmd], cwd=r, check=True)
    return r


def sim_for(attempt_dir):
    """Each attempt gets a fresh cluster that behaves as its scenario needs."""
    path = attempt_dir.as_posix()
    if "/crash/" in path:
        return SimStation(EvalSim(crash={} if "/baselines/" in path else {"bad": 3}))
    return SimStation(EvalSim(needs_mb={"hog": 128}))


async def start(repo, report_type="ci-fast", host=None):
    return await evaluate.start(
        repo,
        repo / "scenarios",
        repo / "eval-runs",
        report_type,
        host or FakeHost(),
        repo / "bin" / "cagent-runner",
        REPO / "platform" / "models.yaml",
    )


async def test_an_evaluation_freezes_runs_and_reports_from_its_files_alone(repo):
    d = await start(repo)
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["eval_id"] == d.name and manifest["dirty"] is False
    assert [s["id"] for s in manifest["slots"]] == [
        "ci-fast/oom/t0/1",
        "ci-fast/crash/t0/1",
        "ci-fast/arm-only/t0/1",
    ]
    assert manifest["scenarios"]["arm-only"] == {
        "applicable": False,
        "reason": "needs linux/amd64; Docker is linux/arm64",
        "files": manifest["scenarios"]["arm-only"]["files"],
    }
    assert manifest["models"] is None  # t0 never asks Ollama
    assert sorted(p.name for p in (d / "scenarios").iterdir()) == [
        "arm-only.yaml",
        "crash.yaml",
        "oom.yaml",
    ]

    lines = []
    summary = await evaluate.run(
        d, repo / "scenarios", repo / "catalog", tools(sim_for), log=lines.append
    )
    assert [ln for ln in lines if "->" in ln] == [
        "baseline/crash a1: healthy -> valid baseline_pass",
        "baseline/oom a1: healthy -> valid baseline_pass",
        "ci-fast/oom/t0/1 a1: healthy -> valid fixed",
        "ci-fast/crash/t0/1 a1: waiting:escalation -> valid safe",
    ]
    assert summary["gates"] == {"exit": 0, "status": "passed"}
    assert [(r["verdict"], r["behaviour"]) for r in summary["ci"]] == [
        ("fixed", None),
        ("safe", "met"),
    ]
    assert summary["not_applicable"] == {"arm-only": "needs linux/amd64; Docker is linux/arm64"}
    report = (d / "report.md").read_text()
    assert "CI regression, not the formal 80 % acceptance" in report
    assert "| `ci-fast/oom/t0/1` | valid | fixed | oom_killed |  |" in report

    (d / "summary.json").unlink()
    assert write_report(d) == summary


async def test_an_attempt_the_harness_never_finished_is_recovered_then_retried_on_request(repo):
    d = await start(repo)
    a1 = d / "attempts" / "ci-fast" / "oom" / "t0" / "1" / "a1"
    a1.mkdir(parents=True)
    a1.joinpath("evidence.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "at": 1.0,
                "phase": "setup",
                "source": "driver",
                "kind": "setup",
                "scenario": "oom",
                "cluster": "edead1",
            }
        )
        + "\n"
    )
    host = FakeHost()
    summary = await evaluate.run(
        d, repo / "scenarios", repo / "catalog", tools(sim_for, host=host), log=lambda _: None
    )
    assert host.forced == ["cagent-edead1"]
    assert json.loads((a1 / "recovered.json").read_text())["confirmed"] is True
    assert "ci-fast/oom/t0/1: void" in summary["blocking"] and summary["gates"]["exit"] == 2

    summary = await evaluate.run(
        d,
        repo / "scenarios",
        repo / "catalog",
        tools(sim_for),
        retry_void="harness killed by the test",
        log=lambda _: None,
    )
    assert (a1.parent / "a2" / "result.json").exists()
    rerun = json.loads((d / "reruns.jsonl").read_text())
    assert (rerun["slot"], rerun["number"], rerun["because"]) == (
        "ci-fast/oom/t0/1",
        2,
        "harness killed by the test",
    )
    assert summary["blocking"] == [] and summary["gates"]["exit"] == 0


async def test_an_attempt_that_needs_investigating_stops_the_evaluation(repo):
    class Moved(SimStation):
        def __init__(self, sim):
            super().__init__(sim, instance="sim-1")

        async def status(self):
            return {"instance_id": "sim-2", "active": None, "queued": {}}

    from cagent.hub import RunnerUnavailable

    class Gone(Moved):
        async def call(self, runner_id, method, params, timeout=None, instance=None):
            if method == "up":
                raise RunnerUnavailable("eval disconnected")
            return await super().call(runner_id, method, params, timeout)

    def station(attempt_dir):
        if "/attempts/" in attempt_dir.as_posix() and "/oom/" in attempt_dir.as_posix():
            return Gone(EvalSim(needs_mb={"hog": 128}))
        return sim_for(attempt_dir)

    d = await start(repo)
    lines = []
    await evaluate.run(d, repo / "scenarios", repo / "catalog", tools(station), log=lines.append)
    assert lines[-1].startswith("stopped: ci-fast/oom/t0/1 a1 needs investigating")
    assert not (d / "attempts" / "ci-fast" / "crash").exists()


async def test_preflight_refuses_what_it_cannot_vouch_for(repo):
    with pytest.raises(evaluate.PreflightError, match="no set 'nope'"):
        evaluate.plan("comparison", {}, ["nope"], ["t0"])
    host = FakeHost()
    host.digests = {"tev1:4b-q4_K_M": "d1"}
    with pytest.raises(evaluate.PreflightError, match="models not served: qwen3.5:latest"):
        await start(repo, "formal", host)
    (repo / "scenarios" / "oom" / "scenario.yaml").write_text("id: [")
    with pytest.raises(evaluate.PreflightError, match="scenario lint failed"):
        await start(repo)


async def test_resuming_refuses_a_changed_scenario(repo):
    d = await start(repo)
    manifest = json.loads((d / "manifest.json").read_text())
    runner = repo / "bin" / "cagent-runner"
    evaluate.check_unchanged(repo, repo / "scenarios", runner, manifest)
    with (repo / "scenarios" / "oom" / "scenario.yaml").open("a") as f:
        f.write("# edited\n")
    with pytest.raises(evaluate.PreflightError, match="scenario oom changed since it was frozen"):
        evaluate.check_unchanged(repo, repo / "scenarios", runner, manifest)


def test_formal_plans_acceptance_then_comparison(repo):
    from cagent.eval.scenario import load_sets

    slots = evaluate.plan("formal", load_sets(repo / "scenarios"))
    assert [s["id"] for s in slots] == [
        "full-repair/oom/default/1",
        "full-repair/oom/default/2",
        "full-repair/oom/default/3",
        "safety/crash/default/1",
        "safety/crash/default/2",
        "safety/crash/default/3",
        "combo/oom/default/1",
        "combo/oom/default/2",
        "combo/oom/default/3",
        "full-repair/oom/t0/1",
        "full-repair/oom/t0+small/1",
        "full-repair/oom/t0+large/1",
    ]
    assert {s["purpose"] for s in slots[:9]} == {"acceptance"}


def test_the_cli_lints_with_an_exit_code(repo, capsys):
    assert cli.main(["--repo", str(repo), "lint"]) == 0
    assert capsys.readouterr().out == "scenarios ok\n"
    (repo / "scenarios" / "sets" / "ci-fast.yaml").write_text(
        yaml.safe_dump({"purpose": "t", "scenarios": ["ghost"]})
    )
    assert cli.main(["--repo", str(repo), "lint"]) == 2
    assert "set ci-fast: unknown scenarios: ghost" in capsys.readouterr().out


async def test_a_scenario_edited_after_freezing_stops_the_run_before_it_is_used(repo):
    d = await start(repo)
    template_file = repo / "scenarios" / "fixtures" / "catalog" / "middleware" / "hog.yaml"
    template_file.write_text(template_file.read_text() + "# edited\n")
    lines = []
    await evaluate.run(d, repo / "scenarios", repo / "catalog", tools(sim_for), log=lines.append)
    assert lines == ["stopped: scenario crash changed since it was frozen"]
    assert not (d / "baselines").exists()


def test_an_unexpected_crash_exits_2_not_1(repo, monkeypatch, capsys):
    def boom(*_):
        raise FileExistsError("eval-runs/x exists")

    monkeypatch.setattr(cli, "write_report", boom)
    assert cli.main(["--repo", str(repo), "report", str(repo / "nowhere")]) == 2
    assert "unexpected FileExistsError" in capsys.readouterr().err
