"""The orchestrator with model tiers, against SimCluster and a scripted model."""

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.hub import ToolResult
from cagent.llm import Router
from cagent.orchestrator import Orchestrator

from .conftest import FIXTURES, REPO_CATALOG, spec_with
from .fakellm import CONFIG, FakeCompleter
from .simcluster import SimCluster
from .test_orchestrator import FAST, no_sleep


@pytest.fixture
def e2e_catalog():
    return load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")


def run(sim, catalog, fake):
    return Orchestrator(
        sim, catalog, InMemorySaver(), FAST, sleep=no_sleep, router=Router(CONFIG, fake)
    )


def answer(path, value, op="replace", cause="typo"):
    return {"root_cause": cause, "patch": [{"op": op, "path": path, "value": value}]}


async def start_and_approve(o, spec):
    await o.start("t1", spec, "laptop-1")
    return await o.resume("t1", {"approve": True})


async def test_small_tier_fixes_a_declared_key_by_itself(e2e_catalog):
    sim = SimCluster(
        requires_env={"modecheck": {"APP_MODE": "server"}},
        errors={"modecheck": "FATAL: APP_MODE must be server, got srvr"},
    )
    fake = FakeCompleter(
        {"fake/small": [answer("/services/modecheck/environment/APP_MODE", "server")]}
    )
    status = await start_and_approve(
        run(sim, e2e_catalog, fake), spec_with(services=[], middleware=[{"template": "modecheck"}])
    )
    assert status.outcome == "healthy"
    assert [f["rule"] for f in status.fixes] == ["llm:small"]
    assert fake.models == ["fake/small"]


async def test_cross_service_problem_goes_to_the_large_tier(e2e_catalog):
    sim = SimCluster(
        requires_env={"cacheclient": {"CACHE_HOST": "redis"}},
        errors={"cacheclient": "nc: bad address 'rediss'"},
    )
    fake = FakeCompleter(
        {"fake/large": [answer("/services/cacheclient/environment/CACHE_HOST", "redis")]}
    )
    spec = spec_with(services=[], middleware=[{"template": "cacheclient"}, {"template": "redis"}])
    status = await start_and_approve(run(sim, e2e_catalog, fake), spec)
    assert status.outcome == "healthy" and [f["rule"] for f in status.fixes] == ["llm:large"]


async def test_undeclared_key_waits_for_a_person(e2e_catalog):
    sim = SimCluster(
        requires_env={"flagcheck": {"FEATURE_FLAG": "on"}},
        errors={"flagcheck": "FATAL: FEATURE_FLAG must be set to on"},
    )
    fake = FakeCompleter(
        {"fake/small": [answer("/services/flagcheck/environment/FEATURE_FLAG", "on", op="add")]}
    )
    o = run(sim, e2e_catalog, fake)
    status = await start_and_approve(
        o, spec_with(services=[], middleware=[{"template": "flagcheck"}])
    )
    assert status.waiting["kind"] == "patch_approval"
    assert status.waiting["reasons"] == [
        "flagcheck: FEATURE_FLAG is not a declared environment key"
    ]
    assert "up" in sim.calls and sim.calls[-1] == "snapshot"  # nothing applied yet
    status = await o.resume("t1", {"approve": True})
    assert status.outcome == "healthy" and status.fixes[-1]["rule"] == "llm:small"


async def test_rejected_approval_escalates(e2e_catalog):
    sim = SimCluster(requires_env={"flagcheck": {"FEATURE_FLAG": "on"}})
    fake = FakeCompleter(
        {"fake/small": [answer("/services/flagcheck/environment/FEATURE_FLAG", "on", op="add")]}
    )
    o = run(sim, e2e_catalog, fake)
    await start_and_approve(o, spec_with(services=[], middleware=[{"template": "flagcheck"}]))
    status = await o.resume("t1", {"approve": False})
    assert status.waiting["kind"] == "escalation" and status.waiting["reason"] == "patch_rejected"


async def test_a_patch_outside_the_gate_is_never_applied_and_the_next_try_moves_up(e2e_catalog):
    sim = SimCluster(requires_env={"modecheck": {"APP_MODE": "server"}})
    fake = FakeCompleter(
        {
            "fake/small": [answer("/services/modecheck/image", "busybox:latest")],
            "fake/large": [answer("/services/modecheck/environment/APP_MODE", "server")],
        }
    )
    status = await start_and_approve(
        run(sim, e2e_catalog, fake), spec_with(services=[], middleware=[{"template": "modecheck"}])
    )
    assert status.outcome == "healthy"
    assert fake.models == ["fake/small", "fake/large"]
    assert sim.compose["services"]["modecheck"]["image"] == "busybox:1.37"


async def test_giving_up_escalates_with_the_root_cause(e2e_catalog):
    sim = SimCluster(crash={"modecheck": 1})
    fake = FakeCompleter({"fake/small": [{"root_cause": "a bug in the application", "patch": []}]})
    await (o := run(sim, e2e_catalog, fake)).start(
        "t1", spec_with(services=[], middleware=[{"template": "modecheck"}]), "laptop-1"
    )
    status = await o.resume("t1", {"approve": True})
    assert status.waiting["reason"] == "no_fix"
    assert status.waiting["root_cause"] == "a bug in the application"


async def test_unreachable_models_escalate(e2e_catalog):
    sim = SimCluster(crash={"modecheck": 1})
    fake = FakeCompleter({"fake/small": [ConnectionError()], "fake/large": [ConnectionError()]})
    o = run(sim, e2e_catalog, fake)
    await o.start("t1", spec_with(services=[], middleware=[{"template": "modecheck"}]), "laptop-1")
    status = await o.resume("t1", {"approve": True})
    assert status.waiting["reason"] == "model_unavailable"


class RejectsListDependsOn(SimCluster):
    """Stands in for a Runner whose validate rejects what a gate-passing patch produced."""

    def _validate(self, p):
        doc = yaml.safe_load(p["compose"])
        if any(isinstance(s.get("depends_on"), list) for s in doc["services"].values()):
            return ToolResult(2, None, "services.modecheck.depends_on.0 must be a mapping")
        return super()._validate(p)


async def test_a_patch_that_breaks_the_compose_is_undone_and_retried_higher(e2e_catalog):
    sim = RejectsListDependsOn(requires_env={"modecheck": {"APP_MODE": "server"}})
    fake = FakeCompleter(
        {
            # Passes the schema and the gate, but the Runner rejects the result.
            "fake/small": [answer("/services/modecheck/depends_on", ["redis"], op="add")],
            "fake/large": [answer("/services/modecheck/environment/APP_MODE", "server")],
        }
    )
    status = await start_and_approve(
        run(sim, e2e_catalog, fake), spec_with(services=[], middleware=[{"template": "modecheck"}])
    )
    assert status.outcome == "healthy"
    assert fake.models == ["fake/small", "fake/large"]
    assert [f["rule"] for f in status.fixes] == ["llm:large"]  # the broken fix was undone
    assert "depends_on" not in status.compose["services"]["modecheck"]


async def test_a_model_repeating_a_tried_state_moves_up_instead_of_escalating(e2e_catalog):
    sim = RejectsListDependsOn(requires_env={"modecheck": {"APP_MODE": "server"}})
    bad = answer("/services/modecheck/depends_on", ["redis"], op="add")
    fake = FakeCompleter(
        {
            "fake/small": [bad],
            "fake/large": [bad, answer("/services/modecheck/environment/APP_MODE", "server")],
        }
    )
    status = await start_and_approve(
        run(sim, e2e_catalog, fake), spec_with(services=[], middleware=[{"template": "modecheck"}])
    )
    assert status.outcome == "healthy"
    assert fake.models == ["fake/small", "fake/large", "fake/large"]
