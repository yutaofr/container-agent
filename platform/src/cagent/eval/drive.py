"""One attempt: set up, drive the orchestrator under the approval policy, collect, check, clean.

The driver approves the initial deployment once, with the scenario's allow list, and stops
at anything else a person would answer: a second approval, a patch approval, a question, a
descriptor save, or an escalation. It never resumes an escalation with retry, which would
reset the budgets the run is measured against (design section 6).

Every attempt ends the same way, whatever ended it: collect the terminal state, run the
business checks only after healthy, then clean up. Cleanup confirms the Runner is idle before
`down`, and confirms nothing of the project is left afterwards; when the Runner does not go
idle, the abnormal branch kills its process group and checks the daemon itself. A cleanup
that cannot be confirmed raises CleanupUnconfirmed, which ends the whole evaluation.
"""

import asyncio
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import Catalog
from cagent.diagnose import declared_env
from cagent.eval.host import HeldPort, HostError, free_port, hold_port
from cagent.eval.observe import EvidenceCaller, EvidenceError, EvidenceLog
from cagent.eval.redact import Redactor
from cagent.eval.scenario import LoadedScenario
from cagent.eval.station import Station
from cagent.hub import InstanceChanged, RunnerUnavailable
from cagent.llm import ModelConfig, Router
from cagent.orchestrator import Orchestrator, Settings, Status

THREAD = "attempt"


class CleanupUnconfirmed(Exception):
    """An attempt's cleanup could not be confirmed: no further slot may run."""


@dataclass(frozen=True)
class Limits:
    attempt_s: float  # guard over driving the orchestrator; its own budgets come first
    idle_s: float = 60.0  # how long the Runner may take to go idle before cleanup
    poll_s: float = 0.5


@dataclass
class Tools:
    """What an evaluation gives each attempt."""

    settings: Settings
    host: Any  # DockerHost, or a fake
    key: bytes  # the evaluation's HMAC key for secret tags; never written
    station: Callable[[Path], Awaitable[Station]]
    router: Callable[[str, EvidenceLog], Router | None]
    limits: Limits
    clock: Callable[[], float] = time.time


@dataclass(frozen=True)
class Attempt:
    loaded: LoadedScenario
    variant: str  # "fault" or "baseline"
    mode: str  # t0, t0+small, t0+large, default; baselines always run t0
    slot: str
    number: int
    dir: Path


@dataclass
class _Run:
    """Mutable bookkeeping for one attempt; written out as result.json."""

    result: dict[str, Any]
    held: list[HeldPort] = field(default_factory=list)
    station: Station | None = None
    caller: EvidenceCaller | None = None


def tier_config(config: ModelConfig, mode: str) -> ModelConfig | None:
    """The model tiers a mode may use: none, one fixed tier, or all of them."""
    if mode == "t0":
        return None
    if mode == "default":
        return config
    tier = mode.removeprefix("t0+")
    return ModelConfig(tiers={tier: config.tiers[tier]})


def cluster_name(a: Attempt) -> str:
    return f"e{secrets.token_hex(3)}{a.number}"


def _declared(loaded: LoadedScenario, spec: Any) -> dict[str, list[str]]:
    catalog = Catalog(
        {**loaded.catalog.services, **loaded.descriptors()}, loaded.catalog.middleware
    )
    return {svc: sorted(keys) for svc, keys in declared_env(spec, catalog).items()}


def _end_of(status: Status) -> str:
    if status.outcome is not None:
        return status.outcome  # healthy or rejected
    if status.waiting is not None:
        return f"waiting:{status.waiting.get('kind')}"
    return "stopped"


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
    os.replace(tmp, path)


async def run_attempt(tools: Tools, a: Attempt) -> dict[str, Any]:
    """Run one attempt and return its result (also written to result.json)."""
    a.dir.mkdir(parents=True, exist_ok=False)
    log = EvidenceLog(a.dir / "evidence.jsonl", Redactor(tools.key), tools.clock)
    cluster = cluster_name(a)
    run = _Run(
        {
            "scenario": a.loaded.id,
            "variant": a.variant,
            "mode": a.mode,
            "slot": a.slot,
            "number": a.number,
            "cluster": cluster,
            "project": f"cagent-{cluster}",
            "settings": asdict(tools.settings),
            "values": {},
            "end": None,
            "error": None,
            "human_inputs": 0,
            "timing": {"log_capture_s": 0.0},
            "cleanup": None,
        }
    )
    try:
        await _attempt(tools, a, log, run)
    finally:
        log.close()
        _redact_runner_log(a.dir / "runner.log", log.redactor)
        run.result = log.redactor.value(run.result)  # errors quote the system: redact them too
        _write_json(a.dir / "result.json", run.result)
    if not run.result["cleanup"]["confirmed"]:
        raise CleanupUnconfirmed(f"{a.slot} #{a.number}: {run.result['cleanup']}")
    return run.result


async def _attempt(tools: Tools, a: Attempt, log: EvidenceLog, run: _Run) -> None:
    r, clock = run.result, tools.clock
    t0 = clock()
    try:
        expect, spec = await _setup(a, log, run)
        r["timing"]["setup_s"] = round(clock() - t0, 3)
        log.phase = "run"
        t1 = clock()
        await _drive(tools, a, log, run, spec)
        r["timing"]["run_s"] = round(clock() - t1, 3)
        if r["end"] == "healthy" and expect.checks:
            t2 = clock()
            await _checks(log, run, [c.probe() for c in expect.checks])
            r["timing"]["check_s"] = round(clock() - t2, 3)
        if log.failure:
            raise EvidenceError(log.failure)
    except EvidenceError as e:
        r["end"], r["error"] = "harness_error", f"evidence: {e}"
    except Exception as e:  # the harness itself failed (the system's errors end in _drive)
        r["end"], r["error"] = "harness_error", f"{type(e).__name__}: {e}"
    finally:
        log.phase = "cleanup"
        t3 = clock()
        r["cleanup"] = await _cleanup(tools, log, run)
        r["timing"]["cleanup_s"] = round(clock() - t3, 3)
        r["timing"]["total_s"] = round(clock() - t0, 3)


async def _setup(a: Attempt, log: EvidenceLog, run: _Run):
    """Bind the host conditions, resolve the spec, and record what the attempt starts from."""
    names = a.loaded.scenario.placeholders
    if a.variant == "fault":
        for _ in names:
            run.held.append(hold_port())
        values = {n: h.port for n, h in zip(names, run.held, strict=True)}
        resolved = a.loaded.resolve(run.result["cluster"], values)
        spec, expect = resolved.spec, resolved.expect
    else:
        values = {n: free_port() for n in names}
        spec = a.loaded.baseline(run.result["cluster"], values)
        expect = a.loaded.resolve(run.result["cluster"], values).expect
    run.result["values"] = values
    log.write(
        "driver",
        "setup",
        scenario=a.loaded.id,
        variant=a.variant,
        mode=a.mode,
        cluster=run.result["cluster"],
        values=values,
        spec=spec.model_dump(mode="json"),
        declared=_declared(a.loaded, spec),
    )
    return expect, spec


async def _capture(tools: Tools, log: EvidenceLog, run: _Run, label: str) -> None:
    t = time.monotonic()
    try:
        caps = await tools.host.logs(run.result["project"])
    except HostError as e:
        log.write("host", "logs_failed", label=label, error=str(e))
    else:
        log.write("host", "logs", label=label, captures=[vars(c) for c in caps])
    run.result["timing"]["log_capture_s"] += round(time.monotonic() - t, 3)


async def _drive(tools: Tools, a: Attempt, log: EvidenceLog, run: _Run, spec: Any) -> None:
    r = run.result
    run.station = await tools.station(a.dir)
    log.write("driver", "station", instance=run.station.instance, pid=run.station.pid)

    async def before(method: str) -> None:
        await _capture(tools, log, run, f"before-{method}")

    run.caller = EvidenceCaller(run.station.target, log, run.station.instance, before)
    router = tools.router(a.mode if a.variant == "fault" else "t0", log)
    o = Orchestrator(run.caller, a.loaded.catalog, InMemorySaver(), tools.settings, router=router)

    async def sink(event: dict[str, Any]) -> None:
        log.write("event", event["type"], task=event.get("task_id"), payload=event.get("payload"))

    allow = list(a.loaded.scenario.approval.allow)

    async def drive() -> Status:
        status = await o.start(THREAD, spec, run.station.runner_id, on_event=sink)
        approved = False
        while status.waiting and status.waiting.get("kind") == "approval" and not approved:
            approved = True
            decision = {"approve": True, "allow": allow}
            log.write("driver", "resume", waiting="approval", decision=decision)
            r["human_inputs"] += 1
            status = await o.resume(THREAD, decision, on_event=sink)
        return status

    t = tools.clock()
    guard = asyncio.timeout(tools.limits.attempt_s)
    try:
        async with guard:
            status = await drive()
        r["end"] = _end_of(status)
        if r["end"] == "healthy":
            r["healthy_after_s"] = round(tools.clock() - t, 3)
    except TimeoutError as e:
        if guard.expired():  # the harness's own guard; a hub call's timeout is the system's
            r["end"] = "attempt_timeout"
        else:
            r["end"], r["error"] = "error:TimeoutError", str(e)[:500]
    except InstanceChanged as e:
        r["end"], r["error"] = "instance_changed", str(e)
    except RunnerUnavailable as e:
        r["end"], r["error"] = "runner_unavailable", str(e)
    except EvidenceError:
        raise
    except Exception as e:  # the system under test raised: its own failure
        r["end"], r["error"] = f"error:{type(e).__name__}", str(e)[:500]
    await _terminal(o, log)
    await _capture(tools, log, run, "terminal")


async def _terminal(o: Orchestrator, log: EvidenceLog) -> None:
    try:
        status = await o.status(THREAD)
        values, _ = await o.checkpoint(THREAD)
    except Exception as e:
        log.write("driver", "terminal_failed", error=f"{type(e).__name__}: {e}")
        return
    log.write(
        "driver",
        "terminal",
        outcome=status.outcome,
        waiting=status.waiting,
        compose=status.compose,
        fixes=status.fixes,
        rejected=values.get("rejected", []),
        rounds=values.get("rounds", 0),
        attempts=values.get("attempts", {}),
        escalation=values.get("escalation"),
    )


async def _checks(log: EvidenceLog, run: _Run, probes: list[dict[str, Any]]) -> None:
    log.phase = "check"
    params = {"name": run.result["cluster"], "probes": probes}
    try:
        await run.caller.call(run.station.runner_id, "verify", params)
    except (InstanceChanged, RunnerUnavailable) as e:
        run.result["end"], run.result["error"] = "runner_unavailable", f"checks: {e}"


async def _idle(tools: Tools, station: Station) -> bool:
    """True once the Runner (still the same process) has nothing active or queued."""
    deadline = time.monotonic() + tools.limits.idle_s
    while time.monotonic() < deadline:
        try:
            st = await station.status()
        except Exception:
            return False
        if st.get("instance_id") != station.instance:
            return False
        if st.get("active") is None and not any((st.get("queued") or {}).values()):
            return True
        await asyncio.sleep(tools.limits.poll_s)
    return False


async def _normal_cleanup(tools: Tools, run: _Run, info: dict[str, Any]) -> bool:
    """down through the idle Runner; True when nothing of the project is left."""
    station = run.station
    if station is None or not await _idle(tools, station):
        return False
    res = await run.caller.call(station.runner_id, "down", {"name": run.result["cluster"]})
    info["down_exit"] = res.exit_code
    if not (res.ok and await _idle(tools, station)):
        return False
    info["residue"] = await tools.host.resources(run.result["project"])
    if any(info["residue"].values()):
        return False
    await station.stop()
    return True


async def _cleanup(tools: Tools, log: EvidenceLog, run: _Run) -> dict[str, Any]:
    """Normal cleanup when it works; on anything else, including an exception from `down`,
    the abnormal branch: kill the Runner's group, force the project down, and confirm."""
    project, info = run.result["project"], {"path": "normal", "confirmed": False}
    try:
        try:
            if await _normal_cleanup(tools, run, info):
                info["confirmed"] = True
                return info
        except Exception as e:
            info["normal_error"] = f"{type(e).__name__}: {e}"
        info["path"] = "abnormal"
        if run.station is not None:
            info["group_gone"] = await run.station.kill()
            if not info["group_gone"]:
                return info
        since = tools.clock()
        await tools.host.force_down(project)
        info["residue"] = await tools.host.resources(project)
        info["events"] = await tools.host.created_since(project, since)
        info["confirmed"] = not any(info["residue"].values()) and not info["events"]
        return info
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    finally:
        for h in run.held:
            h.release()
        log.write("driver", "cleanup", **info)


def _redact_runner_log(path: Path, redactor: Redactor) -> None:
    if path.exists():
        path.write_text(redactor.text(path.read_text(errors="replace")))
