"""Evidence: every record flushed and redacted; wrappers write down calls and nothing more."""

import json

import pytest
from pydantic import BaseModel

from cagent.eval.observe import EvidenceCaller, EvidenceCompleter, EvidenceLog, read_records
from cagent.eval.redact import Redactor
from cagent.hub import InstanceChanged, ToolResult


class Answer(BaseModel):
    text: str


class Target:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        self.calls.append((method, instance))
        if self.fail:
            raise self.fail
        return ToolResult(0, {"ok": True}, "")


def log(tmp_path):
    return EvidenceLog(tmp_path / "evidence.jsonl", Redactor(b"k"), clock=lambda: 7.0)


async def test_calls_are_written_before_they_are_sent_and_pinned_to_one_process(tmp_path):
    ev, target, seen = log(tmp_path), Target(), []

    async def before(method):
        seen.append((method, len(ev.records)))

    caller = EvidenceCaller(target, ev, instance="i-1", before=before)
    await caller.call("eval", "up", {"name": "c", "compose": "services:\n  a: {image: x}\n"})
    await caller.call("eval", "snapshot", {"name": "c"})
    assert target.calls == [("up", "i-1"), ("snapshot", "i-1")]
    assert seen == [("up", 0)]  # logs are read before up, never before snapshot
    records, intact = read_records(tmp_path / "evidence.jsonl")
    assert intact and records == ev.records
    assert [(r["kind"], r.get("call")) for r in records] == [
        ("start", None),
        ("end", 1),
        ("start", None),
        ("end", 3),
    ]
    assert records[0]["params"]["compose"] == {"services": {"a": {"image": "x"}}}
    assert records[0]["at"] == 7.0 and records[0]["phase"] == "setup"


@pytest.mark.parametrize("error", [InstanceChanged("moved"), TimeoutError("slow")])
async def test_a_failed_call_is_closed_with_its_error_and_raised(tmp_path, error):
    ev = log(tmp_path)
    with pytest.raises(type(error)):
        await EvidenceCaller(Target(error), ev).call("eval", "verify", {})
    assert ev.records[-1]["kind"] == "error" and ev.records[-1]["call"] == 1
    assert ev.records[-1]["error"] == type(error).__name__


async def test_a_model_call_records_the_prompt_answer_and_digest_drift(tmp_path):
    class Inner:
        async def complete(self, model, system, user, schema, timeout_s, think=None):
            return schema(text="fixed")

    digests = iter([{"m:1": "d1"}, {"m:1": "d2"}])

    async def now():
        return next(digests)

    ev = log(tmp_path)
    c = EvidenceCompleter(Inner(), ev, frozen={"m:1": "d1"}, digests=now)
    answer = await c.complete("ollama_chat/m:1", "sys", "the prompt", Answer, 5)
    assert answer.text == "fixed"
    kinds = [(r["source"], r["kind"]) for r in ev.records]
    assert kinds == [("model", "start"), ("model", "end"), ("model", "drift")]
    assert ev.records[0]["prompt"] == "the prompt" and ev.records[1]["answer"] == {"text": "fixed"}
    assert c.drift == ["m:1 after call: d2 != d1"]


def test_a_corrupt_line_is_reported_not_skipped_silently(tmp_path):
    path = tmp_path / "evidence.jsonl"
    path.write_text(json.dumps({"seq": 1}) + "\n{broken\n")
    assert read_records(path) == ([{"seq": 1}], False)
