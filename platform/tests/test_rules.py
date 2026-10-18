import pytest

from cagent.patches import apply, fingerprint, within_environment
from cagent.rules import (
    Problem,
    first_fix,
    problems_from_snapshot,
    problems_from_validate,
    to_mb,
    upstream_first,
)


def compose(**services):
    return {"name": "x", "services": services}


def fixed(problem: Problem, doc: dict) -> dict:
    fix = first_fix(problem, doc)
    assert fix is not None and within_environment(fix.patch), fix
    return apply(doc, fix.patch)


def test_port_conflict_moves_only_the_clashing_port():
    doc = compose(db={"ports": ["13306:3306", "13307:33060"]}, other={"ports": ["14306:80"]})
    out = fixed(Problem("port:conflict", "db", "13306"), doc)
    assert out["services"]["db"]["ports"] == ["15306:3306", "13307:33060"]  # 14306 is taken
    assert doc["services"]["db"]["ports"][0] == "13306:3306"  # input untouched


def test_runtime_bind_failure_moves_every_published_port():
    out = fixed(
        Problem("bind:address_in_use", "web"), compose(web={"ports": ["8080:80", "8443:443"]})
    )
    assert out["services"]["web"]["ports"] == ["9080:80", "9443:443"]


def test_oom_doubles_memory_up_to_the_cap():
    out = fixed(Problem("oom:killed", "db"), compose(db={"mem_limit": "512m"}))
    assert out["services"]["db"]["mem_limit"] == "1024m"
    assert first_fix(Problem("oom:killed", "db"), compose(db={"mem_limit": "4g"})) is None
    assert (
        fixed(Problem("oom:killed", "db"), compose(db={}))["services"]["db"]["mem_limit"] == "512m"
    )


def test_java_heap_doubles_xmx_and_keeps_room_for_it():
    doc = compose(app={"environment": {"JAVA_OPTS": "-Xms128m -Xmx512m"}, "mem_limit": "768m"})
    out = fixed(Problem("oom:app", "app"), doc)
    assert out["services"]["app"]["environment"]["JAVA_OPTS"] == "-Xms128m -Xmx1024m"
    assert out["services"]["app"]["mem_limit"] == "2048m"
    assert first_fix(Problem("oom:app", "app"), compose(app={"environment": {}})) is None


def test_arch_mismatch_adds_platform_once():
    out = fixed(Problem("image:arch_mismatch", "legacy"), compose(legacy={"image": "old:1"}))
    assert out["services"]["legacy"]["platform"] == "linux/amd64"
    assert first_fix(Problem("image:arch_mismatch", "legacy"), out) is None


def test_slow_start_extends_the_grace_period():
    doc = compose(
        web={"healthcheck": {"test": ["CMD", "true"], "start_period": "10s", "retries": 30}}
    )
    out = fixed(Problem("health:unhealthy", "web"), doc)
    assert out["services"]["web"]["healthcheck"]["start_period"] == "30s"
    assert out["services"]["web"]["healthcheck"]["retries"] == 60


def test_unknown_failures_have_no_rule():
    assert first_fix(Problem("exit:1", "web"), compose(web={})) is None


def test_problem_extraction():
    assert problems_from_validate({"port_conflicts": [{"service": "db", "port": 13306}]}) == [
        Problem("port:conflict", "db", "13306")
    ]
    snap = {
        "services": [
            {"service": "db", "signatures": ["oom:killed@db"]},
            {"service": "web", "signatures": [], "health": "unhealthy", "health_log": "timeout"},
            {"service": "ok", "signatures": [], "health": "healthy"},
        ]
    }
    assert problems_from_snapshot(snap) == [
        Problem("oom:killed", "db"),
        Problem("health:unhealthy", "web", "timeout"),
    ]


def test_upstream_problems_hide_their_symptoms():
    doc = compose(db={}, app={"depends_on": {"db": {"condition": "service_healthy"}}})
    problems = [Problem("conn:refused", "app"), Problem("oom:killed", "db")]
    assert upstream_first(problems, doc) == [Problem("oom:killed", "db")]


@pytest.mark.parametrize(
    "size, mb", [("512m", 512), ("1g", 1024), ("1536M", 1536), ("2GB", 2048), ("bogus", None)]
)
def test_to_mb(size, mb):
    assert to_mb(size) == mb


def test_patches_outside_the_environment_are_flagged():
    assert within_environment([{"op": "replace", "path": "/services/db/mem_limit", "value": "1g"}])
    assert not within_environment([{"op": "replace", "path": "/services/db/image", "value": "x"}])
    assert not within_environment([{"op": "add", "path": "/services/db/privileged", "value": True}])
    assert not within_environment([{"op": "add", "path": "/volumes/data", "value": {}}])


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": {"c": 2}}) == fingerprint({"b": {"c": 2}, "a": 1})


# --- the environment gate (Task 9) ----------------------------------------------------------


def op(name: str, path: str, **extra) -> dict:
    return {"op": name, "path": path, **extra}


@pytest.mark.parametrize(
    "patch",
    [
        [op("replace", "/services/db/ports/0", value="15306:3306")],
        [op("add", "/services/db/ports/-", value="15306:3306")],
        [op("add", "/services/db/mem_limit", value="512m")],
        [op("replace", "/services/db/mem_limit", value="1024m")],
        [op("replace", "/services/app/environment/JAVA_OPTS", value="-Xmx1024m")],
        [op("add", "/services/legacy/platform", value="linux/amd64")],
        [op("add", "/services/web/healthcheck/start_period", value="30s")],
        [op("replace", "/services/web/healthcheck/retries", value=60)],
        [op("add", "/services/web/healthcheck/interval", value="10s")],
        [op("add", "/services/web/healthcheck/timeout", value="5s")],
        [op("remove", "/services/web/cpus")],
        [op("test", "/services/web/mem_limit", value="64m")],
        [op("add", "/services/web/environment", value={"LOG_LEVEL": "debug"})],
        [op("add", "/services/web/environment/~1LD_X", value="1")],  # key "/LD_X", not "LD_X"
    ],
)
def test_gate_allows_what_the_rules_need(patch):
    assert within_environment(patch)


def test_every_rules_fix_passes_the_gate():
    """A future narrowing of the gate must not silently disable a rule."""
    cases = [
        (Problem("port:conflict", "db", "13306"), compose(db={"ports": ["13306:3306"]})),
        (Problem("bind:address_in_use", "web"), compose(web={"ports": ["8080:80", "8443:443"]})),
        (Problem("oom:killed", "db"), compose(db={"mem_limit": "512m"})),
        (Problem("oom:killed", "db"), compose(db={})),
        (
            Problem("oom:app", "app"),
            compose(app={"environment": {"JAVA_OPTS": "-Xmx512m"}, "mem_limit": "768m"}),
        ),
        (
            Problem("oom:app", "app"),
            compose(app={"environment": {"JAVA_OPTS": "-Xmx512m"}}),  # adds mem_limit
        ),
        (Problem("image:arch_mismatch", "legacy"), compose(legacy={"image": "old:1"})),
        (
            Problem("health:unhealthy", "web"),
            compose(web={"healthcheck": {"test": ["CMD", "true"], "retries": 30}}),
        ),
        (
            Problem("health:unhealthy", "web"),
            compose(web={"healthcheck": {"test": ["CMD", "true"], "start_period": "10s"}}),
        ),
    ]
    for problem, doc in cases:
        fix = first_fix(problem, doc)
        assert fix is not None, problem
        assert within_environment(fix.patch), (problem, fix.patch)


@pytest.mark.parametrize(
    "why, patch",
    [
        (
            "copy carries a source pointer",
            [op("copy", "/services/db/environment/X", **{"from": "/services/db/image"})],
        ),
        ("move", [op("move", "/services/db/mem_limit", **{"from": "/services/db/image"})]),
        ("unknown op", [op("frobnicate", "/services/db/mem_limit", value="1g")]),
        ("missing op", [{"path": "/services/db/mem_limit", "value": "1g"}]),
        ("missing path", [{"op": "remove"}]),
        (
            "a from on an allowed op",
            [op("add", "/services/db/mem_limit", value="1g", **{"from": "/x"})],
        ),
        ("trailing newline", [op("replace", "/services/db/mem_limit\n", value="1g")]),
        ("newline in the service name", [op("replace", "/services/db\n/mem_limit", value="1g")]),
        ("extra segment", [op("replace", "/services/db/mem_limit/extra", value="1g")]),
        (
            "healthcheck test command",
            [op("replace", "/services/db/healthcheck/test", value=["CMD", "x"])],
        ),
        (
            "whole healthcheck object",
            [op("replace", "/services/db/healthcheck", value={"test": ["CMD", "x"]})],
        ),
        ("LD_PRELOAD key", [op("add", "/services/db/environment/LD_PRELOAD", value="/x.so")]),
        ("DYLD key", [op("add", "/services/db/environment/DYLD_INSERT_LIBRARIES", value="/x")]),
        (
            "LD_ key inside the whole mapping",
            [op("replace", "/services/db/environment", value={"A": "1", "LD_LIBRARY_PATH": "/x"})],
        ),
        (
            "DYLD_ entry inside a list-form environment",
            [op("add", "/services/db/environment", value=["A=1", "DYLD_LIBRARY_PATH=/x"])],
        ),
        (
            "LD_ entry appended to a list",
            [op("add", "/services/db/environment/-", value="LD_PRELOAD=/x")],
        ),
        ("escaped slash after LD_", [op("add", "/services/db/environment/LD_A~1B", value="1")]),
        ("invalid escape", [op("add", "/services/db/environment/A~2", value="1")]),
        (
            "one bad op among good ones",
            [
                op("replace", "/services/db/mem_limit", value="1g"),
                op("replace", "/services/db/image", value="evil"),
            ],
        ),
        ("empty path", [op("add", "", value={})]),
        ("not a patch operation", ["replace"]),
    ],
)
def test_gate_rejects(why, patch):
    assert not within_environment(patch), why


def test_port_conflict_near_the_top_moves_down():
    # Ephemeral ports reach 65535; stepping up would leave the valid range.
    out = fixed(Problem("port:conflict", "db", "65271"), compose(db={"ports": ["65271:6379"]}))
    assert out["services"]["db"]["ports"] == ["64271:6379"]
