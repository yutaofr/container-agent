"""Evidence: an append-only log per attempt, and wrappers that only observe.

Every record is redacted and flushed to disk as it is written, so a crash leaves the calls
that were open visibly unclosed. A call writes `start` before it is sent and `end` or
`error` when it returns; nothing is inferred for a call without an ending.

EvidenceCaller wraps the orchestrator's ToolCaller and EvidenceCompleter wraps the model
Completer. Neither corrects an answer, adds evidence the system did not have, or changes a
retry: they write down what passed through them.
"""

import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from cagent.eval.redact import Redactor
from cagent.hub import InstanceChanged, ToolResult


class EvidenceError(Exception):
    """Evidence could not be written: the attempt cannot be judged."""


class EvidenceLog:
    def __init__(self, path: Path, redactor: Redactor, clock: Callable[[], float] = time.time):
        self.path, self.redactor, self.clock = path, redactor, clock
        self.phase = "setup"
        self.failure: str | None = None
        self.records: list[dict[str, Any]] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a")

    def write(self, source: str, kind: str, **data: Any) -> int:
        """Append one redacted record and return its sequence number."""
        seq = len(self.records) + 1
        try:
            rec = {
                "seq": seq,
                "at": self.clock(),
                "phase": self.phase,
                "source": source,
                "kind": kind,
                **self.redactor.value(data),
            }
            self._file.write(json.dumps(rec, sort_keys=True) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        except Exception as e:
            # Remembered as well as raised: a caller such as the model Router may swallow
            # the exception, and a harness failure must never pass for a system result.
            self.failure = self.failure or f"{type(e).__name__}: {e}"
            raise EvidenceError(f"cannot write evidence: {e}") from e
        self.records.append(rec)
        return seq

    def close(self) -> None:
        self._file.close()


def read_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """The records of an evidence file, and whether every line parsed."""
    records, intact = [], True
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            intact = False
    return records, intact


def _params_view(params: dict[str, Any]) -> dict[str, Any]:
    """Tool parameters as evidence: a compose string becomes the document it holds."""
    view = dict(params)
    if isinstance(view.get("compose"), str):
        try:
            view["compose"] = yaml.safe_load(view["compose"])
        except yaml.YAMLError:
            pass
    return view


class EvidenceCaller:
    """The orchestrator's ToolCaller: the target, observed, and pinned to one Runner process.
    before(method) runs ahead of `up` and `down`, which recreate or remove containers, so
    their logs can be read first."""

    def __init__(
        self,
        target: Any,
        log: EvidenceLog,
        instance: str | None = None,
        before: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.target, self.log, self.instance, self.before = target, log, instance, before

    async def call(
        self, runner_id: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> ToolResult:
        if self.before is not None and method in ("up", "down"):
            try:
                await self.before(method)
            except Exception as e:  # reading logs is the harness's work, never the system's
                self.log.failure = self.log.failure or f"before {method}: {e!r}"
                raise EvidenceError(f"before {method}: {e}") from e
        view = _params_view(params)
        self.log.redactor.learn(view.get("compose"))
        n = self.log.write("tool", "start", method=method, params=view)
        pin = {"instance": self.instance} if self.instance is not None else {}
        try:
            res = await self.target.call(runner_id, method, params, timeout, **pin)
        except InstanceChanged as e:
            self.log.write(
                "tool", "error", call=n, method=method, error="InstanceChanged", detail=str(e)
            )
            raise
        except BaseException as e:
            self.log.write(
                "tool", "error", call=n, method=method, error=type(e).__name__, detail=str(e)[:500]
            )
            raise
        self.log.write(
            "tool",
            "end",
            call=n,
            method=method,
            exit_code=res.exit_code,
            output=res.output,
            stderr=res.stderr,
        )
        return res


class EvidenceCompleter:
    """A model Completer, observed. digests() reads each model's current digest; a digest
    different from the frozen one marks drift (the call itself still goes ahead)."""

    def __init__(
        self,
        inner: Any,
        log: EvidenceLog,
        frozen: dict[str, str] | None = None,
        digests: Callable[[], Awaitable[dict[str, str]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.inner, self.log, self.frozen, self.digests = inner, log, frozen or {}, digests
        self.clock = clock
        self.drift: list[str] = []

    async def _check(self, model: str, when: str) -> None:
        if self.digests is None:
            return
        name = model.split("/", 1)[-1]
        try:
            now = (await self.digests()).get(name)
        except Exception as e:  # a failed digest check is the harness's, never the model's
            self.log.failure = self.log.failure or f"digest check: {e!r}"
            raise EvidenceError(f"digest check: {e}") from e
        if now != self.frozen.get(name):
            self.drift.append(f"{name} {when} call: {now} != {self.frozen.get(name)}")
            self.log.write(
                "model", "drift", model=name, when=when, digest=now, frozen=self.frozen.get(name)
            )

    async def complete(self, model, system, user, schema, timeout_s, think=None):
        await self._check(model, "before")
        n = self.log.write(
            "model", "start", model=model, schema=schema.__name__, prompt=user, think=think
        )
        t0 = self.clock()
        try:
            answer = await self.inner.complete(model, system, user, schema, timeout_s, think)
        except BaseException as e:
            self.log.write(
                "model",
                "error",
                call=n,
                error=type(e).__name__,
                detail=str(e)[:500],
                duration_s=round(self.clock() - t0, 3),
            )
            raise
        self.log.write(
            "model",
            "end",
            call=n,
            answer=answer.model_dump(mode="json"),
            duration_s=round(self.clock() - t0, 3),
        )
        await self._check(model, "after")
        return answer
