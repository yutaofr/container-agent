"""The state machine end to end against SimCluster: approvals, fixes, budgets, escalation."""

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from cagent.orchestrator import Orchestrator, Settings

from .conftest import spec_with
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)


async def no_sleep(_seconds: float) -> None:
    return None


def orchestrator(sim, catalog, checkpointer=None) -> Orchestrator:
    return Orchestrator(sim, catalog, checkpointer or InMemorySaver(), FAST, sleep=no_sleep)


MIDDLEWARE_ONLY = {
    "services": [],
    "middleware": [{"template": "mysql", "publish": 13306}, {"template": "redis"}],
}


async def approve(o: Orchestrator, thread: str, **extra):
    return await o.resume(thread, {"approve": True, **extra})


async def test_healthy_run_waits_for_approval_then_builds_and_verifies(catalog, orders_spec):
    sim = SimCluster()
    o = orchestrator(sim, catalog)
    status = await o.start("t1", orders_spec, "laptop-1")
    assert status.outcome is None and status.waiting["kind"] == "approval"
    assert "order-service" in yaml.safe_load(status.waiting["compose"])["services"]
    assert sim.calls == ["validate"]  # nothing starts before approval

    status = await approve(o, "t1")
    assert status.outcome == "healthy" and status.fixes == []
    assert sim.builds == [
        {
            "name": "orders-poc",
            "service": "order-service",
            "stack": "java-springboot",
            "tag": "cagent/order-service:release-1.4.2",
            "maven": "com.acme:order-service:1.4.2:jar",
        }
    ]
    assert sim.calls == [
        "validate",
        "validate",
        "build",
        "up",
        "snapshot",
        "verify",
        "snapshot",
        "verify",
    ]


async def test_port_conflict_is_fixed_before_the_user_sees_the_compose(catalog):
    sim = SimCluster(occupied_ports={13306})
    o = orchestrator(sim, catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert status.waiting["kind"] == "approval"
    assert yaml.safe_load(status.waiting["compose"])["services"]["mysql"]["ports"] == ["14306:3306"]
    assert [f["rule"] for f in status.fixes] == ["port_conflict"]
    assert (await approve(o, "t1")).outcome == "healthy"


async def test_oom_is_fixed_by_doubling_memory(catalog):
    sim = SimCluster(needs_mb={"mysql": 1500})
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    status = await approve(o, "t1")
    assert status.outcome == "healthy"
    assert [f["reason"] for f in status.fixes] == [
        "killed for exceeding 512m; raised mem_limit to 1024m",
        "killed for exceeding 1024m; raised mem_limit to 2048m",
    ]
    assert sim.compose["services"]["mysql"]["mem_limit"] == "2048m"


async def test_exhausted_budget_escalates(catalog):
    sim = SimCluster(needs_mb={"mysql": 100_000})
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    status = await approve(o, "t1")
    assert status.outcome is None and status.waiting["kind"] == "escalation"
    assert status.waiting["reason"] == "budget"
    assert len(status.fixes) == 3  # max_attempts_per_signature
    assert (await o.resume("t1", {"action": "abort"})).outcome == "escalated"


async def test_failure_without_a_rule_escalates(catalog):
    sim = SimCluster(crash={"redis": 1})
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    status = await approve(o, "t1")
    assert status.waiting["reason"] == "no_rule"
    assert status.waiting["problems"] == ["exit:1@redis"]


async def test_retry_after_escalation_runs_again(catalog):
    sim = SimCluster(crash={"redis": 1})
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    await approve(o, "t1")
    sim.crash = {}  # the user fixed something outside the cluster, then retried
    assert (await o.resume("t1", {"action": "retry"})).outcome == "healthy"


async def test_rejection_ends_the_run_without_starting_anything(catalog):
    sim = SimCluster()
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert (await o.resume("t1", {"approve": False})).outcome == "rejected"
    assert "up" not in sim.calls


async def test_findings_need_the_users_allow(catalog):
    sim = SimCluster(findings=[{"service": "mysql", "rule": "field:sysctls", "detail": "x"}])
    o = orchestrator(sim, catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert status.waiting["findings"][0]["rule"] == "field:sysctls"
    assert (await approve(o, "t1", allow=["mysql:field:sysctls"])).outcome == "healthy"


async def test_an_approval_lists_every_finding_and_takes_the_full_allow_set(catalog):
    sim = SimCluster(
        findings=[
            {"service": "mysql", "rule": "field:sysctls", "detail": "x"},
            {"service": "redis", "rule": "cap-add", "detail": "y"},
        ]
    )
    o = orchestrator(sim, catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert len(status.waiting["findings"]) == 2 and status.waiting["allow"] == []

    status = await approve(o, "t1", allow=["mysql:field:sysctls"])
    # redis is still uncovered: the approval comes back, again with every finding
    assert len(status.waiting["findings"]) == 2
    assert status.waiting["allow"] == ["mysql:field:sysctls"]

    # the new allow replaces the old one instead of adding to it
    status = await approve(o, "t1", allow=["redis:cap-add"])
    assert status.waiting["allow"] == ["redis:cap-add"]

    both = ["mysql:field:sysctls", "redis:cap-add"]
    assert (await approve(o, "t1", allow=both)).outcome == "healthy"
    assert all(p["allow"] == [] for m, p in sim.requests if m == "validate")
    assert [p["allow"] for m, p in sim.requests if m == "up"] == [both]


async def test_watch_waits_for_starting_healthchecks(catalog):
    sim = SimCluster(starting_snapshots=3)
    o = orchestrator(sim, catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert (await approve(o, "t1")).outcome == "healthy"
    assert sim.calls.count("snapshot") == 5  # three while starting, one settled, one to stabilize


async def test_generation_problems_escalate_for_clarification(catalog):
    spec = spec_with(services=[{"name": "billing", "source": {"kind": "release", "ref": "1"}}])
    status = await orchestrator(SimCluster(), catalog).start("t1", spec, "laptop-1")
    assert status.waiting["reason"] == "clarify"
    assert "billing: not registered in the catalog" in status.waiting["problems"]


async def test_a_new_orchestrator_resumes_from_the_checkpoint(catalog):
    saver, sim = InMemorySaver(), SimCluster()
    await orchestrator(sim, catalog, saver).start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    # A restarted platform process builds a fresh Orchestrator over the same checkpointer.
    assert (await approve(orchestrator(sim, catalog, saver), "t1")).outcome == "healthy"


async def test_oscillation_escalates(catalog):
    o = orchestrator(SimCluster(), catalog)
    compose = {"services": {"mysql": {"image": "mysql:8.4", "mem_limit": "512m"}}}
    patched = {"services": {"mysql": {"image": "mysql:8.4", "mem_limit": "1024m"}}}
    from cagent.patches import fingerprint

    state = {
        "compose": compose,
        "problems": [{"kind": "oom:killed", "service": "mysql", "detail": ""}],
        "rounds": 0,
        "deadline": 1e18,
        "attempts": {},
        "fixes": [],
        "seen": [fingerprint(compose), fingerprint(patched)],
    }
    cmd = await o._step_diagnose(state)
    assert cmd.goto == "escalate" and cmd.update["escalation"]["reason"] == "oscillation"
