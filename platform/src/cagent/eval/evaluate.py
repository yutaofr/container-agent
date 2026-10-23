"""An evaluation: plan its slots, preflight, freeze the manifest, then run baselines and slots.

The manifest is written before the first slot runs and never changes. Resuming an evaluation
refuses when the commit, the Runner, or any scenario file differs from what it froze. Slots
run one at a time; a cleanup that cannot be confirmed, or an attempt that needs
investigating, stops the evaluation (design sections 6 and 7).
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from cagent.eval.drive import (
    Attempt,
    CleanupUnconfirmed,
    Limits,
    Tools,
    run_attempt,
    tier_config,
)
from cagent.eval.judge import judge
from cagent.eval.observe import EvidenceCompleter, EvidenceLog, read_records
from cagent.eval.redact import Redactor
from cagent.eval.report import judge_dir, write_report
from cagent.eval.scenario import (
    LoadedScenario,
    Scenario,
    ScenarioError,
    lint,
    load_scenario,
    load_sets,
)
from cagent.eval.score import baseline_passed
from cagent.eval.station import RunnerStation, group_members, kill_group
from cagent.llm import LiteLLMCompleter, Router, load_model_config
from cagent.orchestrator import Settings

MODES = ("t0", "t0+small", "t0+large", "default")
FORMAL_SETS = ("full-repair", "safety", "combo")
COMPARISON_MODES = ("t0", "t0+small", "t0+large")
REPORT_TYPES = ("formal", "ci-fast", "comparison")


class PreflightError(Exception):
    """The evaluation cannot start: lint problems, missing models, or a changed tree."""


def plan(
    report_type: str, sets: dict[str, Any], chosen: list[str] = (), modes: list[str] = ()
) -> list[dict[str, Any]]:
    """Every slot of an evaluation, in run order."""
    slots: list[dict[str, Any]] = []

    def add(set_name: str, mode: str, reps: int, purpose: str) -> None:
        if set_name not in sets:
            raise PreflightError(f"no set {set_name!r} in scenarios/sets")
        for sid in sets[set_name].scenarios:
            for rep in range(1, reps + 1):
                slots.append(
                    {
                        "id": f"{set_name}/{sid}/{mode}/{rep}",
                        "set": set_name,
                        "scenario": sid,
                        "mode": mode,
                        "rep": rep,
                        "purpose": purpose,
                    }
                )

    if report_type == "formal":
        for s in FORMAL_SETS:
            add(s, "default", 3, "acceptance")
        for m in COMPARISON_MODES:
            add("full-repair", m, 1, "comparison")
    elif report_type == "ci-fast":
        add("ci-fast", "t0", 1, "ci")
    else:
        if not chosen or not modes:
            raise PreflightError("a comparison needs --set and --mode")
        for s in chosen:
            for m in modes:
                add(s, m, 1, "comparison")
    if bad := sorted({s["mode"] for s in slots} - set(MODES)):
        raise PreflightError(f"unknown modes: {', '.join(bad)}")
    return slots


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


async def preflight(
    repo: Path,
    root: Path,
    report_type: str,
    slots: list[dict[str, Any]],
    host: Any,
    runner: Path,
    models: Path,
) -> dict[str, Any]:
    """Check everything an evaluation needs and describe it, before anything runs."""
    if problems := lint(root, repo / "catalog"):
        raise PreflightError("scenario lint failed:\n" + "\n".join(problems))
    if not runner.is_file():
        raise PreflightError(f"no Runner binary at {runner}")
    platform = await host.platform()
    scenarios = {}
    for sid in dict.fromkeys(s["scenario"] for s in slots):
        loaded = load_scenario(root / sid, repo / "catalog")
        p = loaded.scenario.platforms
        ok = p == "any" or platform in p
        scenarios[sid] = {
            "applicable": ok,
            "files": loaded.files(),
            "reason": None if ok else f"needs {', '.join(p)}; Docker is {platform}",
        }
    images = set()
    for sid, entry in scenarios.items():
        if entry["applicable"]:
            images |= load_scenario(root / sid, repo / "catalog").images()
    model_info = None
    if any(s["mode"] != "t0" for s in slots):
        config = load_model_config(models)
        digests = await host.model_digests()
        names = [t.model.split("/", 1)[-1] for t in config.tiers.values()]
        if missing := [n for n in names if n not in digests]:
            raise PreflightError(f"models not served: {', '.join(missing)}")
        model_info = {"config": config.model_dump(), "digests": {n: digests[n] for n in names}}
    dirty = [
        ln
        for ln in _git(repo, "status", "--porcelain").splitlines()
        if not ln[3:].startswith("eval-runs/")
    ]
    return {
        "format": 1,
        "report_type": report_type,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _git(repo, "rev-parse", "HEAD"),
        "dirty": bool(dirty),
        "runner_sha256": _sha256(runner),
        "platform": platform,
        "docker": await host.versions(),
        "settings": asdict(Settings()),
        "images": await host.image_ids(sorted(images)),
        "models": model_info,
        "slots": slots,
        "scenarios": scenarios,
    }


def new_eval_dir(base: Path, commit: str) -> Path:
    stem = f"{datetime.now(UTC):%Y%m%d}-{commit[:7]}"
    n = 1
    while (base / f"{stem}-{n}").exists():
        n += 1
    return base / f"{stem}-{n}"


def freeze(directory: Path, manifest: dict[str, Any], root: Path) -> None:
    directory.mkdir(parents=True)
    (directory / "scenarios").mkdir()
    for sid in manifest["scenarios"]:
        shutil.copyfile(root / sid / "scenario.yaml", directory / "scenarios" / f"{sid}.yaml")
    manifest = {**manifest, "eval_id": directory.name}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))


def check_unchanged(repo: Path, root: Path, runner: Path, manifest: dict[str, Any]) -> None:
    """Refuse to resume when anything under test differs from the frozen manifest."""
    if _git(repo, "rev-parse", "HEAD") != manifest["commit"]:
        raise PreflightError("the commit differs from the frozen manifest")
    if _sha256(runner) != manifest["runner_sha256"]:
        raise PreflightError("the Runner binary differs from the frozen manifest")
    for sid, s in manifest["scenarios"].items():
        if load_scenario(root / sid, repo / "catalog").files() != s["files"]:
            raise PreflightError(f"scenario {sid} changed since it was frozen")


def make_tools(
    repo: Path,
    root: Path,
    runner: Path,
    models: Path,
    host: Any,
    manifest: dict[str, Any],
    settings: Settings | None = None,
) -> Tools:
    settings = settings or Settings(**manifest["settings"])
    config = load_model_config(models) if manifest.get("models") else None
    frozen = (manifest.get("models") or {}).get("digests", {})

    async def station(attempt_dir: Path) -> RunnerStation:
        home = Path(tempfile.mkdtemp(prefix="cagent-eval-"))  # holds compose files: not kept
        return await RunnerStation.start(
            runner, home, [root / "fixtures"], attempt_dir / "runner.log"
        )

    def router(mode: str, log: EvidenceLog) -> Router | None:
        tiers = tier_config(config, mode) if config else None
        if tiers is None:
            return None
        completer = EvidenceCompleter(LiteLLMCompleter(), log, frozen, host.model_digests)
        return Router(tiers, completer)

    limit = settings.deadline_s + settings.startup_timeout_s + settings.stability_window_s + 600
    return Tools(settings, host, os.urandom(32), station, router, Limits(attempt_s=limit))


def _attempts(base: Path) -> list[Path]:
    return sorted(
        (p for p in base.glob("a*") if p.is_dir()), key=lambda p: int(p.name.removeprefix("a"))
    )


class _Stop(Exception):
    """An attempt needs investigating, or a scenario changed: no further slot runs."""


class _Run:
    """One pass over an evaluation directory: baselines, then slots, in manifest order."""

    def __init__(
        self,
        directory: Path,
        root: Path,
        repo_catalog: Path,
        tools: Tools,
        retry_void: str | None,
        log: Callable[[str], None],
    ):
        self.directory, self.tools, self.retry_void, self.log = directory, tools, retry_void, log
        self.manifest = json.loads((directory / "manifest.json").read_text())
        self.frozen = {
            sid: Scenario.model_validate(
                yaml.safe_load((directory / "scenarios" / f"{sid}.yaml").read_text())
            )
            for sid in self.manifest["scenarios"]
        }
        self.loaded: dict[str, LoadedScenario] = {
            sid: load_scenario(root / sid, repo_catalog) for sid in self.manifest["scenarios"]
        }

    async def all(self) -> None:
        await recover(self.directory, self.tools.host, self.log)
        scenarios = self.manifest["scenarios"]
        applicable = [sid for sid, s in scenarios.items() if s["applicable"]]
        baseline_ok = {}
        for sid in applicable:
            slot, base = f"baseline/{sid}", self.directory / "baselines" / sid
            js = [judge_dir(d, self.frozen, slot)[0] for d in _attempts(base)]
            if not any(j["validity"] == "valid" for j in js) and (not js or self.retry_void):
                js.append(
                    await self.attempt(
                        sid,
                        "baseline",
                        "t0",
                        slot,
                        base,
                        len(js) + 1,
                        self.retry_void if js else None,
                    )
                )
            baseline_ok[sid] = any(baseline_passed(j) for j in js)
        for slot in self.manifest["slots"]:
            sid = slot["scenario"]
            if sid not in applicable or not baseline_ok[sid]:
                continue
            base = self.directory / "attempts" / slot["id"]
            js = [judge_dir(d, self.frozen, slot["id"])[0] for d in _attempts(base)]
            if any(j["validity"] == "valid" for j in js):
                continue
            if js and not (self.retry_void and all(j["validity"] == "void" for j in js)):
                continue
            await self.attempt(
                sid,
                "fault",
                slot["mode"],
                slot["id"],
                base,
                len(js) + 1,
                self.retry_void if js else None,
            )

    async def attempt(
        self,
        sid: str,
        variant: str,
        mode: str,
        slot: str,
        base: Path,
        number: int,
        because: str | None,
    ) -> dict[str, Any]:
        loaded = self.loaded[sid]
        if loaded.files() != self.manifest["scenarios"][sid]["files"]:
            raise _Stop(f"scenario {sid} changed since it was frozen")
        if because:
            with (self.directory / "reruns.jsonl").open("a") as f:
                rerun = {"slot": slot, "number": number, "because": because, "at": time.time()}
                f.write(json.dumps(rerun) + "\n")
        a = Attempt(loaded, variant, mode, slot, number, base / f"a{number}")
        self.log(f"{slot} a{number} ...")
        result = await run_attempt(self.tools, a)
        records, intact = read_records(a.dir / "evidence.jsonl")
        j = judge(self.frozen[sid], result, records, intact)
        why = f" ({'; '.join(j['reasons'])})" if j["reasons"] else ""
        self.log(f"{slot} a{number}: {result['end']} -> {j['validity']} {j['verdict']}{why}")
        if j["validity"] == "investigate":
            raise _Stop(f"{slot} a{number} needs investigating: {'; '.join(j['reasons'])}")
        return j


async def run(
    directory: Path,
    root: Path,
    repo_catalog: Path,
    tools: Tools,
    retry_void: str | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Run every baseline and slot without a valid attempt; then write the report. A
    cleanup that cannot be confirmed, an attempt to investigate, or a scenario that changed
    since freezing stops the run; the report then shows what is missing."""
    try:
        await _Run(directory, root, repo_catalog, tools, retry_void, log).all()
    except (CleanupUnconfirmed, _Stop) as e:
        log(f"stopped: {e}")
    return write_report(directory)


async def recover(directory: Path, host: Any, log: Callable[[str], None] = print) -> None:
    """Attempts that never wrote result.json (the harness died): stop their Runner, which
    outlives the harness in its own session, then remove their project and confirm nothing
    of it is left. Raises CleanupUnconfirmed when that cannot be confirmed."""
    for evidence in sorted(directory.rglob("evidence.jsonl")):
        d = evidence.parent
        if (d / "result.json").exists() or (d / "recovered.json").exists():
            continue
        records, _ = read_records(evidence)
        setup = next((r for r in records if r["kind"] == "setup"), None)
        station = next((r for r in records if r["kind"] == "station"), None)
        info: dict[str, Any] = {"confirmed": False}
        if station and station.get("pid"):
            # The Runner may be gone while a docker command it started still runs in its
            # group; a group whose members are someone else's (a reused id) is left alone.
            members = group_members(station["pid"])
            ours = [m for m in members if "cagent-runner" in m or "docker" in m]
            if ours:
                info["group_gone"] = await kill_group(station["pid"])
            elif members:
                info["group_reused"] = members[:3]
        log_file = d / "runner.log"
        if log_file.exists():  # the key that would tag its secrets died with the harness
            log_file.write_text(Redactor(os.urandom(32)).text(log_file.read_text(errors="replace")))
        if setup is not None and info.get("group_gone", True):
            project = f"cagent-{setup['cluster']}"
            since = time.time()
            await host.force_down(project)
            info["residue"] = await host.resources(project)
            info["events"] = await host.created_since(project, since)
            info["confirmed"] = not any(info["residue"].values()) and not info["events"]
        elif setup is None:
            info["confirmed"] = info.get("group_gone", True)  # nothing was deployed yet
        (d / "recovered.json").write_text(json.dumps(info, indent=2, sort_keys=True))
        log(f"recovered {d.relative_to(directory)}: {info}")
        if not info["confirmed"]:
            raise CleanupUnconfirmed(f"{d}: {info}")


async def start(
    repo: Path,
    root: Path,
    out_base: Path,
    report_type: str,
    host: Any,
    runner: Path,
    models: Path,
    chosen: list[str] = (),
    modes: list[str] = (),
) -> Path:
    """Plan, preflight, and freeze a new evaluation; return its directory."""
    try:
        sets = load_sets(root)
    except ScenarioError as e:
        raise PreflightError(str(e)) from e
    slots = plan(report_type, sets, chosen, modes)
    manifest = await preflight(repo, root, report_type, slots, host, runner, models)
    directory = new_eval_dir(out_base, manifest["commit"])
    freeze(directory, manifest, root)
    return directory
