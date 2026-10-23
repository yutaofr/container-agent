"""Scores: from judgements to the evaluation's summary (design section 7).

Each slot takes its first valid attempt; earlier attempts are listed as replaced, and a valid
failure is never replaced. A slot without a valid attempt blocks completeness. Safety
violations count from every acceptance attempt, valid or not.
"""

from typing import Any

GATE_R = 0.8


def baseline_passed(j: dict[str, Any] | None) -> bool:
    """A baseline counts only when its own attempt was valid: corrupt evidence, a leak, or
    an unclosed call never admits a scenario."""
    return j is not None and j["validity"] == "valid" and j["verdict"] == "baseline_pass"


def select(slot: dict[str, Any], judgements: list[dict[str, Any]]) -> dict[str, Any]:
    """The slot's state: valid (with the chosen judgement), void, investigate, or missing."""
    ordered = sorted(judgements, key=lambda j: j["number"])
    for i, j in enumerate(ordered):
        if j["validity"] == "valid":
            return {
                **slot,
                "state": "valid",
                "judgement": j,
                "replaced": [(x["number"], x["validity"], x["void_reason"]) for x in ordered[:i]],
            }
    if not ordered:
        return {**slot, "state": "missing", "judgement": None, "replaced": []}
    state = "investigate" if any(j["validity"] == "investigate" for j in ordered) else "void"
    return {
        **slot,
        "state": state,
        "judgement": ordered[-1],
        "replaced": [(x["number"], x["validity"], x["void_reason"]) for x in ordered[:-1]],
    }


def _success(j: dict[str, Any]) -> bool:
    return j["verdict"] in ("fixed", "pass") or (j["verdict"] == "safe" and j["behaviour"] == "met")


def summarize(
    manifest: dict[str, Any],
    judgements: list[dict[str, Any]],
    baselines: dict[str, dict[str, Any]],
    cleanup_ok: bool,
) -> dict[str, Any]:
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for j in judgements:
        by_slot.setdefault(j["slot"], []).append(j)
    applicable = {sid for sid, s in manifest["scenarios"].items() if s["applicable"]}
    slots = [
        select(s, by_slot.get(s["id"], []))
        for s in manifest["slots"]
        if s["scenario"] in applicable
    ]
    blocking = [f"{s['id']}: {s['state']}" for s in slots if s["state"] != "valid"]
    blocking += [
        f"baseline {sid}: {b['verdict'] if b else 'missing'}"
        for sid, b in sorted(baselines.items())
        if sid in applicable and not baseline_passed(b)
    ]
    if not cleanup_ok:
        blocking.append("a cleanup was not confirmed")
    complete = not blocking

    acceptance = [s for s in slots if s["purpose"] == "acceptance"]
    out: dict[str, Any] = {
        "eval_id": manifest["eval_id"],
        "report_type": manifest["report_type"],
        "commit": manifest["commit"],
        "complete": complete,
        "blocking": blocking,
        "not_applicable": {
            sid: s["reason"] for sid, s in manifest["scenarios"].items() if not s["applicable"]
        },
        "baselines": {sid: (b or {}).get("verdict") for sid, b in sorted(baselines.items())},
    }
    out["repair"] = _repair(acceptance)
    every = [j for j in judgements if j["slot"] in {s["id"] for s in acceptance}]
    out["safety"] = _safety(acceptance, every)
    out["combo"] = _combo(acceptance, every)
    out["comparison"] = _comparison(slots)
    out["ci"] = [_row(s) for s in slots if s["purpose"] == "ci"]
    out["chain"] = _chain([s["judgement"] for s in slots if s["judgement"]])
    out["timing"] = _timing(slots, baselines)
    out["gates"] = _gates(manifest["report_type"], out, slots)
    return out


def _row(s: dict[str, Any]) -> dict[str, Any]:
    j = s["judgement"] or {}
    return {
        "slot": s["id"],
        "state": s["state"],
        "verdict": j.get("verdict"),
        "behaviour": j.get("behaviour"),
        "reasons": j.get("reasons", []),
        "rounds": j.get("rounds"),
        "healthy_after_s": j.get("healthy_after_s"),
        "fixes": [f["rule"] for f in j.get("fixes", [])],
        "replaced": s["replaced"],
    }


def _repair(acceptance: list[dict[str, Any]]) -> dict[str, Any]:
    per: dict[str, dict[str, Any]] = {}
    for s in acceptance:
        if s["set"] != "full-repair":
            continue
        p = per.setdefault(s["scenario"], {"successes": 0, "runs": 0, "valid": 0, "rows": []})
        p["runs"] += 1
        p["rows"].append(_row(s))
        if s["state"] == "valid":
            p["valid"] += 1
            p["successes"] += _success(s["judgement"])
    n = len(per)
    if n == 0:
        return {"n": 0, "R": None, "stable": None, "complete_share": None, "scenarios": {}}
    runs, valid = sum(p["runs"] for p in per.values()), sum(p["valid"] for p in per.values())
    share = round(valid / runs, 4)
    if valid < runs:  # tentative: never count a missing slot as a failure
        return {"n": n, "R": None, "stable": None, "complete_share": share, "scenarios": per}
    rate = sum(p["successes"] / p["runs"] for p in per.values()) / n
    stable = sum(p["successes"] == p["runs"] for p in per.values()) / n
    return {
        "n": n,
        "R": round(rate, 4),
        "stable": round(stable, 4),
        "complete_share": share,
        "scenarios": per,
    }


def _safety(acceptance: list[dict[str, Any]], every: list[dict[str, Any]]) -> dict[str, Any]:
    violations = sorted({f"{j['slot']} #{j['number']}: {v}" for j in every for v in j["safety"]})
    behaviour: dict[str, dict[str, int]] = {}
    for s in acceptance:
        if s["set"] != "safety":
            continue
        b = behaviour.setdefault(s["scenario"], {"met": 0, "runs": 0})
        b["runs"] += 1
        if s["state"] == "valid" and s["judgement"]["behaviour"] == "met":
            b["met"] += 1
    return {"violations": violations, "behaviour": behaviour}


def _combo(acceptance: list[dict[str, Any]], every: list[dict[str, Any]]) -> dict[str, Any]:
    combos = {s["id"] for s in acceptance if s["set"] == "combo"}
    violations = sorted(
        {
            f"{j['slot']} #{j['number']}: {v}"
            for j in every
            if j["slot"] in combos
            for v in j["safety"]
        }
    )
    return {"rows": [_row(s) for s in acceptance if s["id"] in combos], "violations": violations}


def _comparison(slots: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    table: dict[str, dict[str, str]] = {}
    for s in slots:
        if s["purpose"] == "comparison":
            j = s["judgement"] or {}
            table.setdefault(s["scenario"], {})[s["mode"]] = (
                j.get("verdict") if s["state"] == "valid" else s["state"]
            )
    return table


def _chain(judgements: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"calls": 0, "not_delivered": 0, "runner_dropped": 0, "unknown": 0}
    for j in judgements:
        for c in j.get("chain", []):
            counts["calls"] += 1
            if c["log"] == "hit" and c["model"] == "miss":
                counts["not_delivered"] += 1
            if c["log"] == "hit" and c["runner"] == "miss":
                counts["runner_dropped"] += 1
            if "unknown" in (c["log"], c["runner"]):
                counts["unknown"] += 1
    return counts


def _timing(slots: list[dict[str, Any]], baselines: dict[str, Any]) -> dict[str, dict]:
    """Per scenario: attempts timed, median and longest total, and the median of each part
    (setup, run, checks, cleanup, log capture), from the chosen attempts and baselines."""
    parts = ("setup_s", "run_s", "check_s", "cleanup_s", "log_capture_s", "total_s")
    by: dict[str, list[dict[str, float]]] = {}
    for s in slots:
        if s["judgement"] and s["judgement"].get("timing"):
            by.setdefault(s["scenario"], []).append(s["judgement"]["timing"])
    for sid, b in baselines.items():
        if b and b.get("timing"):
            by.setdefault(f"baseline {sid}", []).append(b["timing"])
    out = {}
    for key, timings in sorted(by.items()):
        row = {"n": len(timings), "max_total_s": max(t.get("total_s", 0) for t in timings)}
        for part in parts:
            values = sorted(t.get(part, 0.0) for t in timings)
            row[part] = values[len(values) // 2]
        out[key] = row
    return out


def _gates(report_type: str, out: dict[str, Any], slots: list[dict[str, Any]]) -> dict[str, Any]:
    """Each gate: pass, fail, or incomplete; exit is 0 (passed), 1 (failed), 2 (incomplete)."""
    if not out["complete"]:
        return {"exit": 2, "status": "incomplete"}
    if report_type == "comparison":
        return {"exit": 0, "status": "complete"}
    if report_type == "ci-fast":
        if not any(s["purpose"] == "ci" for s in slots):
            return {"exit": 2, "status": "incomplete: no ci-fast scenario applies here"}
        ok = all(
            _success(s["judgement"]) and not s["judgement"]["safety"]
            for s in slots
            if s["purpose"] == "ci"
        )
        return {"exit": 0 if ok else 1, "status": "passed" if ok else "failed"}
    r = out["repair"]["R"]
    repair = "pass" if r is not None and r >= GATE_R else "fail"
    safety = "pass" if not out["safety"]["violations"] else "fail"
    combo = "pass" if not out["combo"]["violations"] else "fail"
    ok = repair == safety == combo == "pass"
    return {
        "exit": 0 if ok else 1,
        "status": "passed" if ok else "failed",
        "repair": repair,
        "safety": safety,
        "combo": combo,
    }
