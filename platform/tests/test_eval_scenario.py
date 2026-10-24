"""Scenarios load, bind their placeholders by type and position, and lint before any run."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cagent.eval.scenario import (
    Expect,
    PointerCheck,
    ScenarioError,
    lint,
    load_scenario,
)

from .conftest import REPO_CATALOG

WEB = {
    "name": "pingweb",
    "kind": "middleware",
    "provides": {"capability": "pingweb"},
    "image": "busybox:{version}",
    "command": ["sh", "-c", "httpd -f -p 8080"],
    "versions": ["1.37"],
    "default_version": "1.37",
    "port": 8080,
    "env": {"MODE": "server"},
    "healthcheck": {"cmd": ["CMD", "true"]},
    "presets": {"tiny": {"mem_limit": "64m", "cpus": 0.25}},
}


def scenario(**over):
    doc = {
        "id": "port-taken",
        "kind": "repair",
        "category": "port",
        "spec": {
            "purpose": "a cache",
            "middleware": [{"template": "redis", "publish": "${taken}"}],
        },
        "host": [{"occupied_port": {"as": "taken"}}],
        "baseline": {"spec_patch": []},
        "expect": {
            "config": [{"pointer": "/services/redis/ports", "publishes": "${taken}"}],
            "runtime": [{"signature": "port:conflict@redis"}],
            "outcome": "healthy",
            "accept_rounds": 1,
            "after": [{"pointer": "/services/redis/ports", "not_publishes": "${taken}"}],
            "checks": [{"service": "redis", "tcp": {"port": 6379}}],
        },
    }
    doc.update(over)
    return doc


def write(root: Path, doc: dict, layer: dict | None = None) -> Path:
    d = root / doc["id"]
    d.mkdir(parents=True)
    (d / "scenario.yaml").write_text(yaml.safe_dump(doc))
    if layer is not None:
        (d / "catalog" / "middleware").mkdir(parents=True)
        (d / "catalog" / "middleware" / f"{layer['name']}.yaml").write_text(yaml.safe_dump(layer))
    return d


def sets(root: Path, **lists: list[str]) -> None:
    (root / "sets").mkdir(exist_ok=True)
    for name, ids in lists.items():
        (root / "sets" / f"{name}.yaml").write_text(
            yaml.safe_dump({"purpose": "test", "scenarios": ids})
        )


def test_a_scenario_binds_its_held_port_as_an_integer(tmp_path):
    loaded = load_scenario(write(tmp_path, scenario()), REPO_CATALOG)
    r = loaded.resolve("e-1", {"taken": 40123})
    assert r.spec.name == "e-1"
    assert r.spec.middleware[0].publish == 40123
    assert (r.expect.config[0].op, r.expect.config[0].value) == ("publishes", 40123)
    assert r.expect.after[0].value == 40123
    assert lint(tmp_path, REPO_CATALOG) == []


def test_the_baseline_applies_its_patch_and_needs_values_for_held_ports(tmp_path):
    doc = scenario(
        expect={
            "runtime": [{"signature": "port:conflict@redis"}],
            "outcome": "healthy",
            "accept_rounds": 1,
            "after": [{"pointer": "/services/redis/ports/0", "exists": True}],
        },
        baseline={"spec_patch": [{"op": "replace", "path": "/purpose", "value": "healthy"}]},
    )
    loaded = load_scenario(write(tmp_path, doc), REPO_CATALOG)
    base = loaded.baseline("b-1", {"taken": 40999})
    assert (base.name, base.purpose, base.middleware[0].publish) == ("b-1", "healthy", 40999)
    with pytest.raises(ScenarioError, match=r"no value for placeholder \$\{taken\}"):
        loaded.baseline("b-1", {})
    assert lint(tmp_path, REPO_CATALOG) == []


def test_placeholders_must_be_declared_used_and_whole(tmp_path):
    doc = scenario(
        id="bad-holders",
        host=[{"occupied_port": {"as": "taken"}}, {"occupied_port": {"as": "spare"}}],
        expect={
            "runtime": [{"signature": "port:conflict@redis"}],
            "outcome": "healthy",
            "accept_rounds": 1,
            "after": [
                {"pointer": "/services/redis/ports/0", "equals": "${other}"},
                {"pointer": "/services/redis/ports/0", "equals": "${taken}:6379"},
            ],
        },
    )
    doc["spec"]["purpose"] = "uses ${taken} in text"
    write(tmp_path, doc)
    assert lint(tmp_path, REPO_CATALOG) == [
        "bad-holders: placeholder at spec/purpose: only a whole value at a port or expected value",
        "bad-holders: placeholder at expect/after/1/equals: only a whole value at a port or "
        "expected value",
        "bad-holders: undeclared placeholders: other",
        "bad-holders: host conditions never used: spare",
    ]


def test_a_scenario_without_a_fault_or_that_does_not_generate_fails_lint(tmp_path):
    write(
        tmp_path,
        scenario(
            id="no-fault",
            host=[],
            spec={"purpose": "p", "middleware": [{"template": "redis"}]},
            expect={
                "runtime": [{"signature": "exit:1@redis"}],
                "outcome": "healthy",
                "accept_rounds": 1,
            },
        ),
    )
    write(
        tmp_path,
        scenario(
            id="no-template",
            host=[],
            spec={"purpose": "p", "middleware": [{"template": "nosuch"}]},
            baseline={"spec_patch": [{"op": "replace", "path": "/purpose", "value": "q"}]},
            expect={
                "runtime": [{"signature": "exit:1@nosuch"}],
                "outcome": "healthy",
                "accept_rounds": 1,
            },
        ),
    )
    problems = lint(tmp_path, REPO_CATALOG)
    assert problems[0] == "no-fault: no fault: the baseline equals the scenario"
    assert problems[1].startswith("no-template: scenario does not generate: nosuch: unknown")
    assert problems[2].startswith("no-template: baseline does not generate: nosuch: unknown")
    assert len(problems) == 3


def test_the_scenario_layer_adds_templates_and_may_not_override(tmp_path):
    doc = scenario(
        id="layered",
        host=[],
        spec={"purpose": "p", "middleware": [{"template": "pingweb-typo", "name": "pingweb"}]},
        baseline={
            "spec_patch": [{"op": "replace", "path": "/middleware/0/template", "value": "pingweb"}]
        },
        expect={
            "runtime": [{"signature": "exit:1@pingweb"}],
            "outcome": "healthy",
            "accept_rounds": 1,
        },
    )
    shared = tmp_path / "fixtures" / "catalog" / "middleware"
    shared.mkdir(parents=True)
    (shared / "pingweb.yaml").write_text(yaml.safe_dump(WEB))
    d = write(tmp_path, doc, layer={**WEB, "name": "pingweb-typo", "env": {"MODE": "srvr"}})
    loaded = load_scenario(d, REPO_CATALOG)
    assert {"pingweb", "pingweb-typo", "redis"} <= set(loaded.catalog.middleware)
    assert lint(tmp_path, REPO_CATALOG) == []
    assert sorted(loaded.files()) == [
        f"{tmp_path.name}/fixtures/catalog/middleware/pingweb.yaml",
        f"{tmp_path.name}/layered/catalog/middleware/pingweb-typo.yaml",
        f"{tmp_path.name}/layered/scenario.yaml",
    ]
    (d / "catalog" / "middleware" / "redis.yaml").write_text(
        (REPO_CATALOG / "middleware" / "redis.yaml").read_text()
    )
    with pytest.raises(ScenarioError, match="duplicate name 'redis'"):
        load_scenario(d, REPO_CATALOG)


def test_workdir_projects_must_live_in_fixtures(tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "elsewhere").mkdir()
    doc = scenario(
        id="outside",
        host=[],
        spec={
            "purpose": "p",
            "services": [{"name": "app", "source": {"kind": "workdir", "ref": "elsewhere"}}],
        },
        baseline={"spec_patch": [{"op": "replace", "path": "/purpose", "value": "q"}]},
        expect={"runtime": [{"signature": "exit:1@app"}], "outcome": "healthy", "accept_rounds": 1},
    )
    loaded = load_scenario(write(tmp_path, doc), REPO_CATALOG)
    with pytest.raises(ScenarioError, match="must be a directory in fixtures/"):
        loaded.resolve("x", {})


def test_ids_match_directories_and_sets_name_known_scenarios_once(tmp_path):
    write(tmp_path, scenario())
    d = write(tmp_path, {**scenario(), "id": "other"})
    (d / "scenario.yaml").write_text(yaml.safe_dump(scenario()))  # id port-taken in other/
    sets(tmp_path, fast=["port-taken", "port-taken", "ghost"])
    problems = lint(tmp_path, REPO_CATALOG)
    assert any(
        "other/scenario.yaml: id 'port-taken' differs from its directory" in p for p in problems
    )
    assert "set fast: listed twice: port-taken" in problems
    assert "set fast: unknown scenarios: ghost" in problems


def test_expectations_fit_their_outcome():
    with pytest.raises(ValidationError, match="outcome healthy needs accept_rounds"):
        Expect.model_validate({"runtime": [{"signature": "exit:1@a"}], "outcome": "healthy"})
    with pytest.raises(ValidationError, match="checks run only after healthy"):
        Expect.model_validate(
            {
                "runtime": [{"signature": "exit:1@a"}],
                "outcome": "escalation",
                "checks": [{"service": "a", "tcp": {"port": 1}}],
            }
        )
    with pytest.raises(ValidationError, match="runtime"):
        Expect.model_validate({"runtime": [], "outcome": "escalation"})
    with pytest.raises(
        ValidationError, match="exactly one of equals, not_equals, publishes, not_publishes, exists"
    ):
        PointerCheck.model_validate({"pointer": "/a", "equals": 1, "exists": True})
    assert PointerCheck.model_validate({"pointer": "/a", "equals": None}).op == "equals"
    with pytest.raises(
        ValidationError, match="exactly one of equals, not_equals, publishes, not_publishes, exists"
    ):
        PointerCheck.model_validate({"pointer": "/a"})
