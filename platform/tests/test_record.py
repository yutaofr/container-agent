"""The run record: pending items as published, decisions, masking, one sequence with
events, trimming, paging, migration, and access."""

import json
import sqlite3

import pytest

from cagent.orchestrator import Status
from cagent.record import MASK, decision_record, mask_published, page, size
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import ORDERS_WITHOUT_ENV, answer
from .test_runs_recovery import command, interrupted_run

SECRET_GAP = {
    "kind": "missing_env",
    "target": "order-service",
    "message": "order-service: missing required env DB_PASSWORD",
    "options": ["hunter2"],
    "default": "hunter2",
    "key": "DB_PASSWORD",
}


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def entries(h, run_id, user="alice"):
    out, after = [], 0
    while True:
        rec = await h.service.record(user, run_id, after, limit=3)
        out += rec["entries"]
        after = rec["after"]
        if not rec["more"]:
            return out, rec


def kinds(es):
    return [(e["type"], e.get("kind")) for e in es if e["source"] == "pending"]


def test_secret_values_are_masked_everywhere_but_names_and_actions_stay():
    payload = {
        "gap": SECRET_GAP,
        "question": "order-service needs DB_PASSWORD.",
        "options": ["hunter2"],
        "default": "hunter2",
        "error": "hunter2 is too short",
    }
    masked = mask_published("question", payload)
    assert "hunter2" not in json.dumps(masked)
    assert masked["gap"]["key"] == "DB_PASSWORD" and masked["default"] == MASK
    assert masked["options"] == [MASK] and masked["error"] == MASK
    assert decision_record("question", payload, {"answer": "s3cret"}) == {
        "action": "answer",
        "answer": MASK,
    }
    assert decision_record("question", payload, {"answer": None}) == {"action": "default"}
    assert decision_record("question", payload, {"action": "abort"}) == {"action": "abort"}
    plain = {"gap": {"kind": "unsupported_version", "key": None}, "default": "8.4"}
    assert mask_published("question", plain) == plain
    assert decision_record("question", plain, {"answer": "8.0"}) == {
        "action": "answer",
        "answer": "8.0",
    }
    assert decision_record("save_descriptors", {}, {"saved": True}) == {"action": "saved"}


def test_one_oversized_entry_is_a_page_of_its_own():
    big = {"seq": 1, "source": "event", "type": "observation", "payload": {"x": "y" * 500}}
    small = {"seq": 2, "source": "event", "type": "phase_started", "payload": {"phase": "up"}}
    first, more = page([], [big, small], limit=10, max_bytes=100)
    assert first == [{**big, "payload": {"truncated": True, "bytes": size(big)}, "truncated": True}]
    assert more
    second, more = page([], [small], limit=10, max_bytes=100)
    assert second == [small] and not more


async def test_questions_are_published_and_decided_with_secrets_masked(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create(spec=ORDERS_WITHOUT_ENV)).body["run_id"]
    await answer(h, run_id, {"answer": " "}, command_id="a1")  # rejected: re-asked with an error
    await answer(h, run_id, {"answer": "dev-secret"}, command_id="a2")
    es, rec = await entries(h, run_id)
    assert rec["history"] == "complete" and not rec["gap"]
    assert kinds(es) == [
        ("published", "question"),
        ("decided", "question"),
        ("published", "question"),
        ("decided", "question"),
        ("published", "approval"),
    ]
    # Masking covers the question's records. The approval that follows shows the compose
    # that will run, value included: approval evidence must be what actually runs.
    questions = [e for e in es if e.get("kind") == "question"]
    assert "dev-secret" not in json.dumps(questions)
    decided = [e for e in es if e["type"] == "decided"]
    assert decided[1]["decision"] == {"action": "answer", "answer": MASK}
    assert decided[1]["user"] == "alice" and decided[1]["published"]
    assert decided[1]["command"] == {
        "command_id": "a2",
        "status": "done",
        "effect": None,
        "reason": None,
    }
    seqs = [e["seq"] for e in es]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # a publication commits with its run_state, which names it
    published = next(e for e in es if e["type"] == "published")
    state = next(e for e in es if e["seq"] == published["seq"] + 1)
    assert state["type"] == "run_state" and state["pending_kept"]
    assert state["payload"]["pending_id"] == published["pending_id"]


async def test_approvals_escalations_and_their_decisions_are_kept(store, catalog):
    h = Harness(store, SimCluster(crash={"redis": 3}), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    await answer(h, run_id, {"action": "abort"}, command_id="a2")
    es, _ = await entries(h, run_id)
    assert kinds(es) == [
        ("published", "approval"),
        ("decided", "approval"),
        ("published", "escalation"),
        ("decided", "escalation"),
    ]
    first = next(e for e in es if e["source"] == "pending")
    assert "redis" in first["payload"]["compose"]
    decided = [e["decision"] for e in es if e["type"] == "decided"]
    assert decided == [{"approve": True, "allow": []}, {"action": "abort"}]


async def test_recovery_plans_and_declines_are_kept(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    await answer(h, run_id, {"approve": False}, command_id="r1")
    es, _ = await entries(h, run_id)
    assert kinds(es)[-2:] == [("published", "recovery"), ("decided", "recovery")]
    plan = [e for e in es if e.get("kind") == "recovery"][0]["payload"]
    assert plan["mode"] == "resume_execution" and plan["plan_id"]


@pytest.mark.parametrize(
    ("kind", "waiting", "decision", "kept"),
    [
        (
            "save_descriptors",
            {"service": "shop", "workdir": "/w", "files": {"a": "b"}, "note": "n", "error": None},
            {"saved": True},
            {"action": "saved"},
        ),
        (
            "patch_approval",
            {
                "signature": "s",
                "tier": "large",
                "root_cause": "r",
                "patch": [{"op": "replace", "path": "/services/redis/image", "value": "x"}],
                "reasons": ["undeclared key"],
            },
            {"approve": False},
            {"approve": False},
        ),
    ],
)
async def test_the_other_kinds_are_kept_too(store, catalog, kind, waiting, decision, kept):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    async with store.tx() as t:  # put the run back in an execution, as an answer would
        await t.update_run(run_id, state="running", pending=None, execution_id="x")
    status = Status(None, {"kind": kind, **waiting}, None, [], "interrupt-1")
    await h.service._finished(run_id, "x", ("alice", "c1"), status)
    await answer(h, run_id, decision, command_id="k1")
    es, _ = await entries(h, run_id)
    published, decided = [e for e in es if e.get("kind") == kind]
    assert published["payload"] == waiting
    assert decided["decision"] == kept


async def test_a_repeated_command_records_one_decision(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    _, body = await answer(h, run_id, {"approve": True})
    assert (await h.service.submit("alice", run_id, body)).replay
    es, _ = await entries(h, run_id)
    assert [e["type"] for e in es if e["source"] == "pending"] == ["published", "decided"]


async def test_a_failed_transaction_leaves_no_half_record(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    before = await h.service.record("alice", run_id)
    with pytest.raises(RuntimeError):
        async with store.tx() as t:
            await t.append_pending(run_id, "decided", "p", "approval", {"approve": True})
            raise RuntimeError("crash inside Tx1")
    assert await h.service.record("alice", run_id) == before


async def test_a_decision_survives_a_crash_after_tx1(store, catalog):
    h = Harness(store, SimCluster(), catalog)

    async def crash_before_tx2(*_args):
        return None

    run_id = (await h.create()).body["run_id"]
    h.service._finished = crash_before_tx2
    await answer(h, run_id, {"approve": True})
    h.service = h.new_service()
    await h.service.recover_on_startup()
    es, _ = await entries(h, run_id)
    assert kinds(es) == [("published", "approval"), ("decided", "approval")]
    assert es[-1]["source"] == "event"  # the reconcile run_state came after


async def test_trimmed_events_never_hide_pending_records(tmp_path, catalog):
    store = await Store.open(str(tmp_path / "small.db"), keep_events=3)
    h = Harness(store, SimCluster(starting_snapshots=4), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    rec = await h.service.record("alice", run_id, 0, limit=1000)
    assert rec["gap"] and rec["events_pruned_through"] > 0
    es, _ = await entries(h, run_id)  # three at a time across the watermark
    assert kinds(es) == [("published", "approval"), ("decided", "approval")]
    assert len([e for e in es if e["source"] == "event"]) == 3
    seqs = [e["seq"] for e in es]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert await store.events_after(run_id, 0, 10, 10_000) is None  # SSE must resync
    await store.close()


async def test_the_record_belongs_to_the_runs_owner(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    for user in ("bob", "carol"):
        with pytest.raises(RunError) as err:
            await h.service.record(user, run_id)
        assert err.value.status == 404
    for after in (-1, 10_000):
        with pytest.raises(RunError) as err:
            await h.service.record("alice", run_id, after)
        assert err.value.code == "bad_cursor"


async def test_runs_from_before_the_record_are_marked_partial(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)  # the M8a schema, with a run waiting for approval
    db.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, runner TEXT NOT NULL,
            cluster TEXT NOT NULL, input TEXT NOT NULL, created_at REAL NOT NULL,
            state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, thread_id TEXT,
            executions INTEGER NOT NULL DEFAULT 0, execution_id TEXT,
            execution_instance TEXT, phase TEXT, pending TEXT, reconcile TEXT,
            last_check TEXT, recovery TEXT, last_observation TEXT,
            last_observation_attempt TEXT, verified_at REAL, outcome TEXT,
            event_seq INTEGER NOT NULL DEFAULT 0, min_event_seq INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO runs (id, owner, runner, cluster, input, created_at, state, event_seq)
        VALUES ('old', 'alice', 'laptop-1', 'demo', '{}', 0, 'waiting', 7);
        """
    )
    db.commit()
    db.close()
    store = await Store.open(str(path))
    async with store.read() as t:
        run = await t.get_run("old")
    await store.close()
    assert run["history"] == "partial" and run["events_pruned_through_seq"] == 0
    assert run["event_seq"] == 7
