"""Recovery plans and the state a recovery thread starts from."""

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from cagent.generate import to_yaml
from cagent.orchestrator import Orchestrator, Settings
from cagent.recovery import (
    CONTINUE,
    INIT_PARTIAL,
    INSTANCE_CHANGED,
    PREVIOUS_UNCONFIRMED,
    RESUME,
    build_plan,
    compose_digest,
    make_recovery_initial,
)

from .conftest import spec_with
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
IDLE = {"at": 1.0, "instance_id": "i-1", "idle": True, "same_instance": True}
INIT = {"image": "busybox", "labels": {"cagent.role": "init"}}


async def no_sleep(_seconds: float) -> None:
    return None


def values(**overrides):
    spec = spec_with(services=[], middleware=[{"template": "redis"}]).model_dump()
    base = {
        "cluster": "orders-poc",
        "runner": "laptop-1",
        "purpose": None,
        "spec": spec,
        "descriptors": {},
        "compose": {"services": {"redis": {"image": "redis:7.4"}}},
        "fixes": [{"rule": "port_conflict"}],
        "built": True,
        "approved": True,
        "allow": ["redis:cap-add"],
        "unchecked": {"previous": {}},
        "proposal": {"patch": []},
        "report": {"passed": True},
        "deadline": 0.0,
        "attempts": {"x": 3},
        "rounds": 7,
        "outcome": None,
    }
    return {**base, **overrides}


def plan(v=None, check=IDLE, dispatched=(), last_approved=None, marks=()):
    return build_plan(
        values=v,
        last_approved=last_approved,
        check=check,
        observation={"services": [{"service": "redis", "state": "running", "exit_code": 0}]},
        dispatched=list(dispatched),
        marks=list(marks),
        catalog_digest="c1",
    )


def test_images_are_reused_only_when_built_and_no_build_was_sent():
    assert plan(values()).content["build"]["decision"] == "skip_all"
    assert plan(values(), dispatched=["build"]).content["build"]["decision"] == "rebuild_all"
    assert plan(values(built=False)).content["build"]["decision"] == "rebuild_all"


def test_unverifiable_risks_need_acknowledging():
    v = values(compose={"services": {"redis": {"image": "redis"}, "redis-init": INIT}})
    p = plan(v, check={**IDLE, "same_instance": False})
    assert p.mode == RESUME and p.required == [INSTANCE_CHANGED, INIT_PARTIAL]
    assert "cannot verify" in p.content["risks"][0]["text"]


def test_a_compose_changed_since_approval_is_shown():
    v = values()
    approved = {"id": "a1", "at": 5.0, "candidate_digest": "other", "content": {"compose": "x"}}
    ids = [r["id"] for r in plan(v, last_approved=approved).content["risks"]]
    assert "compose_changed" in ids
    same = {**approved, "candidate_digest": compose_digest(v["compose"])}
    assert "compose_changed" not in [r["id"] for r in plan(v, last_approved=same).content["risks"]]


def test_a_new_run_on_a_marked_cluster_needs_no_compose():
    p = plan(None, marks=[{"id": "m1", "reason": "up sent without a result"}])
    assert p.mode == CONTINUE and p.required == [PREVIOUS_UNCONFIRMED]
    assert p.content["marks"] == ["m1"] and p.content["steps"] == ["start"]


def test_a_plan_changes_when_its_facts_do():
    a = plan(values())
    b = plan(values(), check={**IDLE, "instance_id": "i-2", "same_instance": False})
    assert a.facts["instance_id"] == "i-1" and a.digest != b.digest
    assert plan(values()).digest == a.digest


def test_the_recovery_state_inherits_no_stale_control_state():
    v = values()
    p = plan(v)
    state = make_recovery_initial(p.content, v, deadline=99.0, source={"thread_id": "r1"})
    assert state["compose"] == v["compose"] and state["fixes"] == v["fixes"]
    assert (state["approved"], state["allow"], state["built"]) == (True, [], True)
    assert state["unchecked"] is None and state["proposal"] is None and state["report"] is None
    assert (state["deadline"], state["attempts"], state["rounds"]) == (99.0, {}, 0)
    assert state["recovered"] and state["source"] == {"thread_id": "r1"}


async def test_a_recovery_thread_starts_at_validate_and_follows_the_build_decision(catalog):
    sim = SimCluster()
    o = Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep)
    v = values()
    state = make_recovery_initial(plan(v).content, v, deadline=1e18, source={})
    status = await o.start_recovery("r1:2", state)
    assert status.outcome == "healthy"
    assert sim.calls[:2] == ["validate", "up"]  # approved, images reused: no build
    assert yaml.safe_load(sim.requests[0][1]["compose"]) == v["compose"]


async def test_a_finding_in_recovery_goes_back_to_a_normal_approval(catalog):
    sim = SimCluster(findings=[{"service": "redis", "rule": "cap-add", "detail": "x"}])
    o = Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep)
    v = values()
    state = make_recovery_initial(plan(v).content, v, deadline=1e18, source={})
    status = await o.start_recovery("r1:2", state)
    assert status.waiting["kind"] == "approval" and status.waiting["allow"] == []
    assert to_yaml(status.compose) == plan(v).content["compose"]
    values_, checkpoint_id = await o.checkpoint("r1:2")
    assert values_["recovered"] and checkpoint_id
