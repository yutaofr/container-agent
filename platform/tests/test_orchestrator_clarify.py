"""Clarify and project inspection in the state machine, against SimCluster and a scripted model."""

import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.llm import Router
from cagent.orchestrator import Orchestrator, Settings

from .conftest import spec_with
from .fakellm import CONFIG, FakeCompleter
from .simcluster import SimCluster
from .test_draft import SHOP, answer

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
PURPOSE = "Run order-service 1.4.2 against MySQL 5.7."


async def no_sleep(_seconds: float) -> None:
    return None


def orchestrator(sim, catalog, script=None, saver=None, settings=FAST) -> Orchestrator:
    router = Router(CONFIG, FakeCompleter(script)) if script is not None else None
    return Orchestrator(
        sim, catalog, saver or InMemorySaver(), settings, sleep=no_sleep, router=router
    )


def extraction(services, middleware=()):
    return {"name": "orders", "services": services, "middleware": list(middleware)}


ORDERS = {"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}


async def test_a_purpose_becomes_a_spec_one_question_at_a_time(catalog):
    sim = SimCluster()
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    o = orchestrator(sim, catalog, script)
    status = await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    q = status.waiting
    assert (q["kind"], q["gap"]["kind"], q["options"], q["default"]) == (
        "question",
        "unsupported_version",
        ["8.0", "8.4"],
        "8.4",
    )
    assert q["question"].endswith("Which version should mysql use?")
    status = await o.resume("t1", {"answer": None})  # take the default
    assert status.waiting["gap"]["kind"] == "no_provider"
    status = await o.resume("t1", {"answer": "kafka"})
    assert status.waiting["gap"] == {
        "kind": "missing_env",
        "target": "order-service",
        "message": "order-service: missing required env SPRING_PROFILES_ACTIVE",
        "options": [],
        "default": None,
        "key": "SPRING_PROFILES_ACTIVE",
    }
    status = await o.resume("t1", {"answer": " "})
    assert status.waiting["kind"] == "question" and status.waiting["error"] == (
        "an answer is required"
    )
    status = await o.resume("t1", {"answer": "dev"})
    assert status.waiting["kind"] == "approval"
    assert sim.calls == ["validate"]  # nothing ran on the user's machine before approval
    assert (await o.resume("t1", {"approve": True})).outcome == "healthy"
    assert sim.builds[0]["maven"] == "com.acme:order-service:1.4.2:jar"


async def test_aborting_a_question_ends_the_run(catalog):
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    o = orchestrator(SimCluster(), catalog, script)
    await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    assert (await o.resume("t1", {"action": "abort"})).outcome == "rejected"


async def test_too_many_questions_escalate(catalog):
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    settings = Settings(poll_interval_s=0, stability_window_s=0, max_questions=1)
    o = orchestrator(SimCluster(), catalog, script, settings=settings)
    await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    status = await o.resume("t1", {"answer": None})
    assert status.waiting["kind"] == "escalation" and status.waiting["reason"] == "clarify"
    assert (
        "order-service/kafka: no middleware in the spec provides kafka"
        in (status.waiting["problems"])
    )


async def test_a_purpose_needs_a_model(catalog):
    with pytest.raises(ValueError, match="needs a model router"):
        await orchestrator(SimCluster(), catalog).start_from_purpose("t1", PURPOSE, "laptop-1")


async def test_extraction_failure_escalates(catalog):
    o = orchestrator(SimCluster(), catalog, {"fake/large": [ConnectionError("down")]})
    status = await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    assert status.waiting["reason"] == "model_unavailable"


API = {"name": "orders-api", "source": {"kind": "workdir", "ref": "/src/shop"}}
SPEC = spec_with(services=[API], middleware=[])


def shop():
    return copy.deepcopy(SHOP)


async def test_an_unregistered_project_is_drafted_saved_read_back_and_built(catalog):
    sim = SimCluster(projects={"/src/shop": shop()})
    saver = InMemorySaver()
    o = orchestrator(sim, catalog, {"fake/large": [answer()]}, saver)
    status = await o.start("t1", SPEC, "laptop-1")
    w = status.waiting
    assert (w["kind"], w["service"], w["workdir"]) == (
        "save_descriptors",
        "orders-api",
        "/src/shop",
    )
    [(path, text)] = w["files"].items()
    assert path == ".cagent/services/orders-api.yaml" and "mvn -q -pl api -am package" in text
    assert "cql/keyspace.cql" in w["note"] and w["error"] is None

    status = await o.resume("t1", {"saved": True})  # said so, but did not save
    assert status.waiting["kind"] == "save_descriptors"
    assert "not saved yet" in status.waiting["error"]

    sim.projects["/src/shop"]["files"][path] = text  # the user saves the draft
    status = await o.resume("t1", {"saved": True})
    # The project's requirements now drive the questions: mysql and kafka are missing.
    assert [status.waiting["kind"], status.waiting["gap"]["kind"]] == ["question", "no_provider"]
    status = await o.resume("t1", {"answer": None})
    status = await o.resume("t1", {"answer": None})
    assert status.waiting["kind"] == "approval"

    # A restarted platform still knows the saved descriptor: it lives in the checkpoint.
    o2 = orchestrator(sim, catalog, saver=saver)
    assert (await o2.resume("t1", {"approve": True})).outcome == "healthy"
    assert sim.builds == [
        {
            "name": "orders-poc",
            "service": "orders-api",
            "stack": "java-springboot",
            "tag": status.compose["services"]["orders-api"]["image"],
            "workdir": "/src/shop",
        }
    ]
    env = status.compose["services"]["orders-api"]["environment"]
    assert env["DB_PASSWORD"] == "app" and env["KAFKA"] == "kafka:9092"


async def test_a_descriptor_already_on_disk_needs_no_model(catalog):
    project = shop()
    project["files"][".cagent/services/orders-api.yaml"] = (
        "name: orders-api\nkind: app\nstack: dockerfile\n"
        "sources:\n  workdir: { dockerfile: api/Dockerfile }\nruntime: {}\n"
    )
    sim = SimCluster(projects={"/src/shop": project})
    status = await orchestrator(sim, catalog).start("t1", SPEC, "laptop-1")
    assert status.waiting["kind"] == "approval"
    # Same checkpointer is not needed: the run continues in this orchestrator.
    o = orchestrator(sim, catalog)
    await o.start("t2", SPEC, "laptop-1")
    assert (await o.resume("t2", {"approve": True})).outcome == "healthy"
    assert sim.builds[-1]["stack"] == "dockerfile"


@pytest.mark.parametrize(
    "project, script, reason, detail",
    [
        (shop(), None, "clarify", "no model to draft"),
        (
            {**SHOP, "modules": [], "files": {"package.json": "{}"}},
            {},
            "clarify",
            "add a Dockerfile",
        ),
        (shop(), {"fake/large": [ConnectionError("down")]}, "model_unavailable", "down"),
        (None, {}, "runner_error", "not a directory"),
    ],
)
async def test_inspection_failures_escalate(catalog, project, script, reason, detail):
    sim = SimCluster(projects={"/src/shop": project} if project else {})
    status = await orchestrator(sim, catalog, script).start("t1", SPEC, "laptop-1")
    assert status.waiting["kind"] == "escalation" and status.waiting["reason"] == reason
    assert detail in str(status.waiting)
