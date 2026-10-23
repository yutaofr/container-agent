"""The report, recomputed from an evaluation directory alone.

    <dir>/manifest.json                     frozen before the first slot ran
    <dir>/scenarios/<id>.yaml               the frozen scenarios
    <dir>/baselines/<id>/a<n>/              one attempt: evidence.jsonl, result.json
    <dir>/attempts/<slot>/a<n>/

`write_report` judges every attempt again, so a new judge version yields a new report from
the same files. It writes judgements.json, summary.json, and report.md.
"""

import json
from pathlib import Path
from typing import Any

import yaml

from cagent.eval.judge import JUDGE_VERSION, judge
from cagent.eval.observe import read_records
from cagent.eval.scenario import Scenario
from cagent.eval.score import GATE_R, summarize

FORMAT = 1


def _attempt_dirs(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p.parent for p in base.rglob("evidence.jsonl"))


def judge_dir(d: Path, frozen: dict[str, Scenario], slot: str) -> tuple[dict, bool]:
    """(judgement, cleanup confirmed) of one attempt directory."""
    number = int(d.name.removeprefix("a"))
    records, intact = read_records(d / "evidence.jsonl")
    result_path = d / "result.json"
    setup = next((r for r in records if r["kind"] == "setup"), {})
    sid = setup.get("scenario") or slot.split("/")[1]  # <set>/<scenario>/... or baseline/<id>
    if not result_path.exists():
        # The harness died inside the attempt. Judge what reached disk anyway: the attempt is
        # void, but a dangerous change already in its evidence must still count.
        result = {
            "scenario": sid,
            "variant": setup.get("variant", "fault"),
            "mode": setup.get("mode"),
            "slot": slot,
            "number": number,
            "end": None,
            "values": setup.get("values") or {},
            "settings": {},
            "cleanup": {},
        }
        j = judge(frozen[sid], result, records, intact)
        j["reasons"].insert(0, "no result.json: the attempt did not finish")
        recovered = d / "recovered.json"  # written when a later run cleaned up after it
        return j, recovered.exists() and json.loads(recovered.read_text())["confirmed"]
    result = json.loads(result_path.read_text())
    j = judge(frozen[result["scenario"]], result, records, intact)
    return j, bool((result.get("cleanup") or {}).get("confirmed"))


def collect(directory: Path) -> tuple[dict, list[dict], dict[str, dict | None], bool]:
    manifest = json.loads((directory / "manifest.json").read_text())
    frozen = {
        sid: Scenario.model_validate(
            yaml.safe_load((directory / "scenarios" / f"{sid}.yaml").read_text())
        )
        for sid in manifest["scenarios"]
    }
    judgements, cleanup_ok = [], True
    for d in _attempt_dirs(directory / "attempts"):
        slot = d.parent.relative_to(directory / "attempts").as_posix()
        j, ok = judge_dir(d, frozen, slot)
        judgements.append(j)
        cleanup_ok = cleanup_ok and ok
    baselines: dict[str, dict | None] = {
        sid: None for sid in manifest["scenarios"] if manifest["scenarios"][sid]["applicable"]
    }
    for d in _attempt_dirs(directory / "baselines"):
        sid = d.parent.name
        j, ok = judge_dir(d, frozen, f"baseline/{sid}")
        cleanup_ok = cleanup_ok and ok
        if baselines.get(sid) is None or baselines[sid]["validity"] != "valid":
            baselines[sid] = j
    return manifest, judgements, baselines, cleanup_ok


def write_report(directory: Path) -> dict[str, Any]:
    manifest, judgements, baselines, cleanup_ok = collect(directory)
    summary = summarize(manifest, judgements, baselines, cleanup_ok)
    summary["format"], summary["judge_version"] = FORMAT, JUDGE_VERSION
    (directory / "judgements.json").write_text(
        json.dumps({"judgements": judgements, "baselines": baselines}, indent=2, sort_keys=True)
    )
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (directory / "report.md").write_text(render(manifest, summary))
    return summary


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f} %"


def render(manifest: dict[str, Any], s: dict[str, Any]) -> str:
    gates = s["gates"]
    lines = [
        f"# Evaluation {s['eval_id']}",
        "",
        f"- Report type: **{s['report_type']}**"
        + (
            " — CI regression, not the formal 80 % acceptance"
            if s["report_type"] == "ci-fast"
            else ""
        ),
        f"- Commit under test: `{s['commit']}`"
        + (" (dirty tree)" if manifest.get("dirty") else ""),
        f"- Docker: {manifest['platform']} · Runner `{manifest['runner_sha256'][:12]}`",
        f"- Judge version {s['judge_version']}, report format {s['format']}",
        f"- Status: **{gates['status']}** (exit {gates['exit']})",
        "",
    ]
    if s["blocking"]:
        lines += ["## Incomplete", "", "These keep the report tentative:", ""]
        lines += [f"- {b}" for b in s["blocking"]] + [""]
    if s["not_applicable"]:
        lines += ["## Not applicable here", ""]
        lines += [f"- `{k}`: {v}" for k, v in s["not_applicable"].items()] + [""]
    r = s["repair"]
    if r["n"]:
        lines += [
            "## Repair (acceptance: default routing, three runs)",
            "",
            f"- Mean scenario repair rate R = **{_pct(r['R'])}** over N = {r['n']} "
            + (
                f"(tentative: {_pct(r['complete_share'])} of slots have a valid result) "
                if r["R"] is None
                else ""
            )
            + f"(gate {GATE_R * 100:.0f} %: {gates.get('repair', 'n/a')})",
            f"- Stable pass rate (3/3): {_pct(r['stable'])}",
            "",
            "| Scenario | Fixed | Valid runs | Rounds | Healthy after (s) | Reasons |",
            "|---|---|---|---|---|---|",
        ]
        for sid, p in sorted(r["scenarios"].items()):
            rounds = ", ".join(str(x["rounds"]) for x in p["rows"])
            after = ", ".join(str(x["healthy_after_s"]) for x in p["rows"])
            why = "; ".join(sorted({w for x in p["rows"] for w in x["reasons"]}))
            lines.append(
                f"| `{sid}` | {p['successes']}/{p['runs']} | {p['valid']} | {rounds} "
                f"| {after} | {why} |"
            )
        lines.append("")
    sf = s["safety"]
    if sf["behaviour"] or sf["violations"]:
        lines += [
            "## Safety",
            "",
            f"- Gate: **{gates.get('safety', 'n/a')}** "
            f"({len(sf['violations'])} violations in acceptance runs)",
            "",
        ]
        lines += [f"- violation: {v}" for v in sf["violations"]]
        lines += ["", "| Scenario | Expected behaviour met |", "|---|---|"]
        lines += [f"| `{k}` | {v['met']}/{v['runs']} |" for k, v in sorted(sf["behaviour"].items())]
        lines.append("")
    if s["combo"]["rows"]:
        lines += [
            "## Combined scenarios",
            "",
            f"- Gate (no safety violation): **{gates.get('combo', 'n/a')}**",
            "",
            "| Slot | Verdict | Fixes | Reasons |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| `{x['slot']}` | {x['verdict']} | {', '.join(x['fixes'])} | "
            f"{'; '.join(x['reasons'])} |"
            for x in s["combo"]["rows"]
        ]
        lines.append("")
    if s["comparison"]:
        modes = [
            m
            for m in ("t0", "t0+small", "t0+large", "default")
            if any(m in row for row in s["comparison"].values())
        ]
        note = (
            "Default routing is the acceptance runs above (three runs each)."
            if s["report_type"] == "formal"
            else "One run per scenario and mode."
        )
        lines += [
            "## Tier comparison (exploratory: one run per mode)",
            "",
            note,
            "",
            "| Scenario | " + " | ".join(modes) + " |",
            "|---|" + "---|" * len(modes),
        ]
        for sid, row in sorted(s["comparison"].items()):
            lines.append(f"| `{sid}` | " + " | ".join(str(row.get(m, "")) for m in modes) + " |")
        lines.append("")
    if s["ci"]:
        lines += [
            "## CI regression",
            "",
            "| Slot | State | Verdict | Fixes | Reasons |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| `{x['slot']}` | {x['state']} | {x['verdict']} | {', '.join(x['fixes'])} "
            f"| {'; '.join(x['reasons'])} |"
            for x in s["ci"]
        ]
        lines.append("")
    c = s["chain"]
    lines += [
        "## Diagnostic chain",
        "",
        f"- Model calls with an expected log line: {c['calls']}",
        f"- Diagnostic text not delivered (in the log, not in the prompt): {c['not_delivered']}",
        f"- In the log, not in the Runner's snapshot: {c['runner_dropped']}",
        f"- Unknown (log capture incomplete or no snapshot): {c['unknown']}",
        "",
    ]
    if s.get("timing"):
        lines += [
            "## Timing (seconds, medians; log capture counts against the deadline)",
            "",
            "| Scenario | n | Setup | Run | Checks | Cleanup | Log capture | Total | Longest |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for key, t in s["timing"].items():
            lines.append(
                f"| {key} | {t['n']} | {t['setup_s']:.0f} | {t['run_s']:.0f} | "
                f"{t['check_s']:.0f} | {t['cleanup_s']:.0f} | "
                f"{t['log_capture_s']:.1f} | {t['total_s']:.0f} | "
                f"{t['max_total_s']:.0f} |"
            )
        lines.append("")
    lines += ["## Baselines", ""]
    lines += [f"- `{k}`: {v}" for k, v in s["baselines"].items()] + [""]
    return "\n".join(lines)
