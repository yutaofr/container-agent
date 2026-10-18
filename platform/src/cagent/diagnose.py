"""T1/T2: model-written fixes for failures no T0 rule covers (design 5.3).

The model sees the failing service's redacted evidence and its compose config (with secret
values masked), proposes a JSON Patch, and the patch is judged before anything changes:

  apply    inside the environment gate, and only declared environment keys
  approve  inside the gate, but sets an undeclared key, a JVM option outside the tuning
           allow-list, or a value that loads a JVM agent or runs a command on JVM failure:
           a person decides
  reject   outside the gate: never applied; the next attempt moves up a tier

Declared keys come from the service's own definition (descriptor env, bind keys, template
env), so the allowlist follows what the application really uses (roadmap, M7).
"""

import copy
import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from cagent.catalog import Catalog
from cagent.llm import Router
from cagent.patches import within_environment
from cagent.rules import Problem
from cagent.spec import ClusterSpec

EnvValue = str | int | float | bool | None


class PatchOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "replace", "remove"]
    path: str
    value: EnvValue | dict[str, EnvValue] | list[str] = None


class Diagnosis(BaseModel):
    root_cause: str
    patch: list[PatchOp]


@dataclass(frozen=True)
class Proposal:
    tier: str
    signature: str
    root_cause: str
    patch: list[dict[str, Any]]
    verdict: Literal["apply", "approve", "reject", "give_up"]
    reasons: list[str] = field(default_factory=list)


SYSTEM = (
    "You fix docker compose clusters started for testing. You may only change service "
    "environment settings: published ports, mem_limit, cpus, environment variables, platform, "
    "depends_on, and healthcheck timing. Never change images, commands, volumes, or anything "
    "else. Keep the document's existing shapes: a mapping such as environment or depends_on "
    "stays a mapping, and prefer one field per operation. "
    "If the cause lies in the application's own code or cannot be fixed by those "
    "settings, return an empty patch. Paths are RFC 6901 JSON Pointers into the compose "
    "document, e.g. /services/web/environment/APP_MODE. Answer only with JSON matching the "
    "schema."
)

_SECRET_KEY = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
# JVM options that load code or run a shell command: agents, and the OnError/OnOutOfMemoryError
# hooks, which execute their argument when the JVM crashes or runs out of memory.
_AGENT_VALUE = re.compile(r"(?i)-(javaagent|agentpath|agentlib)[:=]|-XX:\+?On(OutOfMemory)?Error\b")
# Keys whose values the JVM or a launcher script reads as JVM options (JAVA_OPTS,
# JAVA_TOOL_OPTIONS, KAFKA_HEAP_OPTS, ...). Their values are checked token by token against
# an allow-list; options anywhere else still meet the narrower _AGENT_VALUE check.
_JVM_OPTS_KEY = re.compile(r"(?i)(OPTS|OPTIONS)$")
# The allow-list: heap and stack sizes; -XX boolean flags and -XX flags with a number (a
# path value such as -XX:Flags=<file> reads options from a file), never the On* hooks; and
# -D for a few named properties with plain values. Other properties stay out by name, not by
# prefix: com.sun.management.jmxremote.*, logging.config and spring.config.import all load
# or expose code without touching the java.* namespace.
_JVM_TUNING = re.compile(
    r"-X(ms|mx|mn|ss)\d+[kmg]?|-XX:[+-](?!On)\w+|-XX:(?!On)\w+=\d+(\.\d+)?[kmg%]?",
    re.ASCII | re.IGNORECASE,
)
_JVM_PROPERTY = re.compile(r"-D([\w.-]+)=[\w.,-]*", re.ASCII)
_SAFE_PROPERTIES = frozenset(
    {"spring.profiles.active", "file.encoding", "user.timezone", "server.port"}
)
_ENV_KEY_PATH = re.compile(r"/services/([^/]+)/environment/([^/]+)")
_ENV_ROOT_PATH = re.compile(r"/services/([^/]+)/environment")
_CROSS_KINDS = {"conn:refused", "dns:unresolved", "auth:denied"}


def declared_env(spec: ClusterSpec, catalog: Catalog) -> dict[str, set[str]]:
    """Environment keys each service already uses, by its own definition."""
    declared: dict[str, set[str]] = {}
    for ref in spec.services:
        desc = catalog.services.get(ref.name)
        if desc is None:
            continue
        keys = set(desc.runtime.env.required) | set(desc.runtime.env.defaults) | set(ref.env)
        for req in desc.requires:
            keys |= set(req.bind)
        declared[ref.name] = keys
    for m in spec.middleware:
        if (tpl := catalog.middleware.get(m.template)) is not None:
            declared[m.instance] = set(tpl.env)
    return declared


def mask_secrets(compose: dict[str, Any]) -> dict[str, Any]:
    """A copy whose secret-looking environment values read ******, for prompts."""
    masked = copy.deepcopy(compose)
    for svc in masked.get("services", {}).values():
        env = svc.get("environment")
        if isinstance(env, dict):
            for k in env:
                if _SECRET_KEY.search(k):
                    env[k] = "******"
    return masked


def _unescape_compose(value: Any) -> Any:
    """Undo compose's $$ escaping, recursively, so the model reads what the container sees."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, dict):
        return {k: _unescape_compose(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unescape_compose(v) for v in value]
    return value


def prompt_view(compose: dict[str, Any]) -> dict[str, Any]:
    """The compose document as a model should read it: secrets masked, and $$ shown as $.
    Seeing the escape, a model took `$$CACHE_PORT` for a bug in the command and gave up."""
    return _unescape_compose(mask_secrets(compose))


def _env_items(op: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(service, key, value) triples an operation sets, in every environment shape."""
    path = op["path"]
    value = op.get("value")
    if m := _ENV_KEY_PATH.fullmatch(path):
        key = m.group(2).replace("~1", "/").replace("~0", "~")
        if key.isdigit() or key == "-":  # list-form environment: "KEY=value"
            if value is None:
                return []
            k, _, v = str(value).partition("=")
            return [(m.group(1), k, v)]
        return [(m.group(1), key, None if value is None else str(value))]
    if (m := _ENV_ROOT_PATH.fullmatch(path)) and isinstance(value, dict):
        return [(m.group(1), k, None if v is None else str(v)) for k, v in value.items()]
    if m and isinstance(value, list):
        return [(m.group(1), *_split_entry(str(e))) for e in value]
    return []


def _split_entry(entry: str) -> tuple[str, str]:
    key, _, value = entry.partition("=")
    return key, value


def _jvm_token_allowed(token: str) -> bool:
    """True for a JVM option that only tunes memory or GC, or sets a known plain property.

    Everything else - agents, debuggers, boot classpath, class-loader properties,
    @argfiles, -XX flags that read files or run hooks, shell metacharacters - is held
    for a person to approve.
    """
    if _JVM_TUNING.fullmatch(token):
        return True
    prop = _JVM_PROPERTY.fullmatch(token)
    return prop is not None and prop.group(1) in _SAFE_PROPERTIES


def judge(patch: list[dict[str, Any]], declared: dict[str, set[str]]) -> tuple[str, list[str]]:
    """Classify a model patch as apply, approve, or reject, with the reasons."""
    if not within_environment(patch):
        return "reject", ["changes something outside the environment gate"]
    reasons = []
    for op in patch:
        if op["op"] == "remove":
            continue
        for service, key, value in _env_items(op):
            if key not in declared.get(service, set()):
                reasons.append(f"{service}: {key} is not a declared environment key")
            if value is None:
                continue
            if _JVM_OPTS_KEY.search(key):
                held = [t for t in value.split() if not _jvm_token_allowed(t)]
                if held:
                    reasons.append(f"{service}: {key} sets JVM option(s) {' '.join(held)}")
            elif _AGENT_VALUE.search(value):
                reasons.append(
                    f"{service}: {key} loads a JVM agent or command hook "
                    "(-javaagent, -agentpath, -agentlib, -XX:OnError, -XX:OnOutOfMemoryError)"
                )
    return ("approve", reasons) if reasons else ("apply", [])


def is_cross_service(problem: Problem, compose: dict[str, Any], lines: list[str]) -> bool:
    """True when the evidence points at another service, so the whole cluster is in scope."""
    if problem.kind in _CROSS_KINDS:
        return True
    others = sorted(set(compose.get("services", {})) - {problem.service})
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", " ".join(lines)):
        if token in others or difflib.get_close_matches(token, others, n=1, cutoff=0.8):
            return True
    return False


def _evidence(snapshot: dict[str, Any], service: str) -> tuple[dict[str, Any], list[str]]:
    for s in snapshot.get("services", []):
        if s.get("service") == service:
            lines = list(dict.fromkeys([*s.get("error_lines", []), *s.get("tail", [])]))
            return s, lines
    return {}, []


def prompt(
    problem: Problem,
    state: dict[str, Any],
    lines: list[str],
    compose: dict[str, Any],
    declared: dict[str, set[str]],
    cross: bool,
) -> str:
    services = prompt_view(compose)["services"]
    scope = services if cross else {problem.service: services.get(problem.service, {})}
    keys = {s: sorted(declared.get(s, set())) for s in scope}
    log = [f"  {line}" for line in lines[-25:]] or ["  (none)"]
    return "\n".join(
        [
            f"Service `{problem.service}` failed: signature {problem.signature}, "
            f"state {state.get('state', 'unknown')}, exit code {state.get('exit_code', 'n/a')}.",
            "Recent log lines (redacted):",
            *log,
            "Compose services:",
            yaml.safe_dump(scope, sort_keys=False).rstrip(),
            f"Declared environment keys: {keys}",
            "Propose a JSON Patch against the compose document.",
        ]
    )


async def propose(
    router: Router,
    problem: Problem,
    snapshot: dict[str, Any],
    compose: dict[str, Any],
    declared: dict[str, set[str]],
    attempts: int,
) -> Proposal:
    """Ask the model for a fix. The tier rises with cross-service scope and failed attempts."""
    state, lines = _evidence(snapshot, problem.service)
    cross = is_cross_service(problem, compose, lines)
    start = router.tier_at((1 if cross or problem.kind.startswith("unknown:") else 0) + attempts)
    diag, tier = await router.ask(
        SYSTEM, prompt(problem, state, lines, compose, declared, cross), Diagnosis, start=start
    )
    patch = [op.model_dump(exclude={"value"} if op.op == "remove" else set()) for op in diag.patch]
    if not patch:
        return Proposal(tier, problem.signature, diag.root_cause, [], "give_up")
    verdict, reasons = judge(patch, declared)
    return Proposal(tier, problem.signature, diag.root_cause, patch, verdict, reasons)
