"""The scenario library itself: it lints clean, and its sets are the fixed lists it names."""

from cagent.eval.scenario import lint, load_scenario, load_sets, scenario_dirs

from .conftest import REPO, REPO_CATALOG

ROOT = REPO / "scenarios"


def test_the_library_lints_clean():
    assert lint(ROOT, REPO_CATALOG) == []


def test_ci_fast_and_quick_are_fixed_lists():
    sets = load_sets(ROOT)
    assert sets["ci-fast"].scenarios == ["port-occupied", "container-oom"]
    quick = set(sets["quick"].scenarios)
    assert {"port-occupied", "container-oom", "undeclared-key", "mixed-models"} <= quick
    for sid in quick:  # quick needs no JVM, MySQL, or Kafka
        loaded = load_scenario(ROOT / sid, REPO_CATALOG)
        assert not loaded.workdirs() and not {"mysql", "kafka"} & {
            m["template"] for m in loaded.scenario.spec.get("middleware", [])
        }, sid


def test_the_formal_sets_hold_every_scenario_once_and_by_kind():
    sets = load_sets(ROOT)
    kinds = {d.name: load_scenario(d, REPO_CATALOG).scenario.kind for d in scenario_dirs(ROOT)}
    formal = {"full-repair": "repair", "safety": "safety", "combo": "combo"}
    listed = [sid for name in formal for sid in sets[name].scenarios]
    assert sorted(listed) == sorted(kinds)
    for name, kind in formal.items():
        assert {kinds[sid] for sid in sets[name].scenarios} == {kind}, name
