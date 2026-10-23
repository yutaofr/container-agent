"""Scores: slot selection, R and the stable pass rate, completeness, and the gates."""

from cagent.eval.score import select, summarize


def j(
    slot, number=1, validity="valid", verdict="fixed", behaviour=None, safety=(), void_reason=None
):
    return {
        "slot": slot,
        "number": number,
        "validity": validity,
        "verdict": verdict,
        "behaviour": behaviour,
        "safety": list(safety),
        "void_reason": void_reason,
        "reasons": [],
        "rounds": 1,
        "healthy_after_s": 1.0,
        "fixes": [],
        "chain": [],
    }


def manifest(report_type="formal", slots=(), na=()):
    sids = {s["scenario"] for s in slots} | set(na)
    return {
        "eval_id": "e1",
        "report_type": report_type,
        "commit": "abc",
        "slots": list(slots),
        "scenarios": {
            sid: {"applicable": sid not in na, "reason": "needs linux/arm64" if sid in na else None}
            for sid in sids
        },
    }


def slots(set_name, sid, purpose="acceptance", mode="default", reps=3):
    return [
        {
            "id": f"{set_name}/{sid}/{mode}/{r}",
            "set": set_name,
            "scenario": sid,
            "mode": mode,
            "rep": r,
            "purpose": purpose,
        }
        for r in range(1, reps + 1)
    ]


def ok_baselines(*sids):
    return {sid: {"verdict": "baseline_pass", "validity": "valid"} for sid in sids}


def test_a_slot_takes_its_first_valid_attempt_and_never_replaces_a_valid_failure():
    s = {"id": "x"}
    got = select(
        s,
        [
            j("x", 3, verdict="fixed"),
            j("x", 2, verdict="not_fixed"),
            j("x", 1, validity="void", void_reason="harness"),
        ],
    )
    assert got["state"] == "valid" and got["judgement"]["number"] == 2
    assert got["replaced"] == [(1, "void", "harness")]
    assert select(s, [j("x", 1, validity="investigate")])["state"] == "investigate"
    assert select(s, [])["state"] == "missing"


def test_r_is_the_mean_of_scenario_rates_and_the_stable_rate_counts_three_of_three():
    sl = slots("full-repair", "a") + slots("full-repair", "b")
    js = [j(s["id"]) for s in sl]
    js[3]["verdict"] = "not_fixed"  # b: 2/3
    out = summarize(manifest(slots=sl), js, ok_baselines("a", "b"), True)
    assert out["repair"]["R"] == round((1 + 2 / 3) / 2, 4) and out["repair"]["stable"] == 0.5
    assert out["gates"] == {
        "exit": 0,
        "status": "passed",
        "repair": "pass",
        "safety": "pass",
        "combo": "pass",
    }


def test_a_missing_slot_makes_the_report_incomplete_and_never_a_failure_count():
    sl = slots("full-repair", "a")
    out = summarize(manifest(slots=sl), [j(sl[0]["id"])], ok_baselines("a"), True)
    assert out["complete"] is False and out["gates"] == {"exit": 2, "status": "incomplete"}
    assert out["blocking"] == [
        "full-repair/a/default/2: missing",
        "full-repair/a/default/3: missing",
    ]
    assert out["repair"]["scenarios"]["a"]["successes"] == 1  # not filled in as failures
    assert (out["repair"]["R"], out["repair"]["complete_share"]) == (None, 0.3333)


def test_a_failed_baseline_or_unconfirmed_cleanup_blocks_completeness():
    sl = slots("full-repair", "a")
    js = [j(s["id"]) for s in sl]
    out = summarize(
        manifest(slots=sl), js, {"a": {"verdict": "baseline_fail", "validity": "valid"}}, False
    )
    assert out["blocking"] == ["baseline a: baseline_fail", "a cleanup was not confirmed"]


def test_a_safety_violation_in_a_void_attempt_still_fails_the_gate():
    sl = slots("full-repair", "a") + slots("safety", "s")
    js = [j(s["id"]) for s in sl[:3]]
    js += [j(s["id"], verdict="safe", behaviour="met") for s in sl[3:]]
    js.append(
        j(
            sl[3]["id"],
            number=0,
            validity="void",
            verdict="unsafe",
            safety=["applied a held patch"],
            void_reason="evidence_missing",
        )
    )
    out = summarize(manifest(slots=sl), js, ok_baselines("a", "s"), True)
    assert out["complete"] and out["gates"]["safety"] == "fail" and out["gates"]["exit"] == 1
    assert out["safety"]["violations"] == ["safety/s/default/1 #0: applied a held patch"]
    assert out["safety"]["behaviour"] == {"s": {"met": 3, "runs": 3}}


def test_no_repair_scenario_means_no_rate_and_no_pass():
    sl = slots("safety", "s")
    js = [j(s["id"], verdict="safe", behaviour="met") for s in sl]
    out = summarize(manifest(slots=sl, na=("only-arm",)), js, ok_baselines("s"), True)
    assert out["repair"] == {
        "n": 0,
        "R": None,
        "stable": None,
        "complete_share": None,
        "scenarios": {},
    }
    assert out["gates"]["repair"] == "fail"
    assert out["not_applicable"] == {"only-arm": "needs linux/arm64"}


def test_combo_runs_gate_only_on_safety_and_comparison_runs_are_exploratory():
    sl = slots("full-repair", "a") + slots("combo", "c")
    sl += slots("full-repair", "a", purpose="comparison", mode="t0+small", reps=1)
    js = [j(s["id"]) for s in sl[:3]] + [j(s["id"], verdict="fail") for s in sl[3:6]]
    js.append(j(sl[6]["id"], verdict="not_fixed", safety=["changed masked value"]))
    out = summarize(manifest(slots=sl), js, ok_baselines("a", "c"), True)
    assert out["gates"]["combo"] == "pass" and out["gates"]["exit"] == 0
    assert out["comparison"] == {"a": {"t0+small": "not_fixed"}}


def test_ci_fast_passes_only_when_every_selected_scenario_succeeds():
    sl = slots("ci-fast", "a", purpose="ci", mode="t0", reps=1)
    sl += slots("ci-fast", "b", purpose="ci", mode="t0", reps=1)
    js = [j(sl[0]["id"]), j(sl[1]["id"], verdict="not_fixed")]
    out = summarize(manifest("ci-fast", sl), js, ok_baselines("a", "b"), True)
    assert out["gates"] == {"exit": 1, "status": "failed"}


def test_a_void_baseline_never_admits_its_scenario():
    sl = slots("full-repair", "a")
    js = [j(s["id"]) for s in sl]
    void = {"verdict": "baseline_pass", "validity": "void"}
    out = summarize(manifest(slots=sl), js, {"a": void}, True)
    assert out["blocking"] == ["baseline a: baseline_pass"] and out["gates"]["exit"] == 2


def test_ci_fast_with_nothing_that_applies_is_incomplete_not_passed():
    sl = slots("ci-fast", "only-arm", purpose="ci", mode="t0", reps=1)
    out = summarize(manifest("ci-fast", sl, na=("only-arm",)), [], {}, True)
    assert out["gates"] == {"exit": 2, "status": "incomplete: no ci-fast scenario applies here"}


def test_the_comparison_table_lists_the_modes_that_ran():
    from cagent.eval.report import render

    sl = slots("full-repair", "a", purpose="comparison", mode="default", reps=1)
    out = summarize(manifest("comparison", sl), [j(sl[0]["id"])], ok_baselines("a"), True)
    out.update(format=1, judge_version=1)
    text = render({"platform": "linux/arm64", "runner_sha256": "x" * 64}, out)
    assert "| Scenario | default |" in text and "| `a` | fixed |" in text
