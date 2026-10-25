"""T1/T2: model-written fixes for failures no T0 rule covers (design 5.3).

The model sees the failing service's redacted evidence and its compose config (with secret
values masked), proposes a JSON Patch, and the patch is judged before anything changes:

  credentials  touches a secret-named environment key, in any way: a person must handle
           it (checked first, so it wins over every other verdict)
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
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

import jsonpatch
import yaml
from jsonpointer import JsonPointer, JsonPointerException
from pydantic import BaseModel, ConfigDict

from cagent.catalog import Catalog
from cagent.llm import Router
from cagent.patches import init_services, within_environment
from cagent.rules import Problem
from cagent.secrets import SECRET_KEY, env_pairs, known_secrets, mask_compose, mask_text
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
    verdict: Literal["credentials", "apply", "approve", "reject", "give_up"]
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
                if SECRET_KEY.search(k):
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
    """The compose document as a model should read it: $$ shown as $, then secrets masked
    (environment values by key, credential positions in commands, long known values).
    Seeing the escape, a model took `$$CACHE_PORT` for a bug in the command and gave up;
    unescaping first lets the masking recognise $NAME references as the container does."""
    view = _unescape_compose(compose)
    return mask_compose(view, known_secrets(view))


def _known(compose: dict[str, Any]) -> set[str]:
    return known_secrets(_unescape_compose(compose))


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


def judge(
    patch: list[dict[str, Any]],
    declared: dict[str, set[str]],
    protected: frozenset[str],
    before: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify a model patch as credentials, reject, approve, or apply, with the reasons.
    protected names the init services, which no patch may change; before is the compose
    document the patch applies to. Credential reasons name fields ("service: KEY") only."""
    fields, applies = credential_fields(patch, before)
    if fields:
        return "credentials", fields
    if not applies:
        return "reject", ["patch does not apply"]
    if not within_environment(patch, protected):
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


def credential_fields(
    patch: list[dict[str, Any]], before: dict[str, Any]
) -> tuple[list[str], bool]:
    """The credential fields a patch touches, and whether every operation applied.

    The patch is replayed op by op on a copy of before, so an index means what it means at
    that step. An operation touches a credential when it targets a secret-named key, when a
    list entry's old or new key is secret-named, when it replaces, adds, or removes an
    environment (or a service, or all services) whose old or new value holds a secret key,
    or when it changes any secret-named entry at all. After an operation fails to apply,
    the rest are judged by path and value alone on the copy as it was. Analysis only: this
    authorises nothing."""
    doc, fields, applies = copy.deepcopy(before), [], True
    for op in patch:
        parts = _parts(op)
        if parts is None:
            applies = False
            continue
        if applies:
            try:
                after = jsonpatch.apply_patch(doc, [op])
            except (jsonpatch.JsonPatchException, JsonPointerException, TypeError, KeyError):
                applies = False
            else:
                fields += _touched(op, parts, doc, after) + _changed_entries(doc, after)
                doc = after
                continue
        fields += _touched(op, parts, doc, None)
    return list(dict.fromkeys(fields)), applies


def _parts(op: Any) -> list[str] | None:
    if not isinstance(op, dict) or not isinstance(op.get("path"), str):
        return None
    try:
        return JsonPointer(op["path"]).parts
    except JsonPointerException:
        return None


def _env_of(doc: Any, service: str) -> Any:
    svc = (doc.get("services") or {}).get(service) if isinstance(doc, dict) else None
    return svc.get("environment") if isinstance(svc, dict) else None


def _secret_keys(env: Any, service: str) -> list[str]:
    return [f"{service}: {k}" for k, _ in env_pairs(env) if SECRET_KEY.search(k)]


def _services(doc: Any) -> dict[str, Any]:
    services = doc.get("services") if isinstance(doc, dict) else None
    return services if isinstance(services, dict) else {}


def _entries(doc: Any) -> list[tuple[str, str, str]]:
    out = []
    for name in _services(doc):
        out += [(name, k, v) for k, v in env_pairs(_env_of(doc, name)) if SECRET_KEY.search(k)]
    return out


def _changed_entries(old: Any, new: Any) -> list[str]:
    a, b = Counter(_entries(old)), Counter(_entries(new))
    return sorted({f"{s}: {k}" for s, k, _ in (a - b) + (b - a)})


def _touched(op: dict[str, Any], parts: list[str], doc: Any, after: Any) -> list[str]:
    """Fields an operation targets by its position. after is None once the patch stopped
    applying: then the operation's own value stands in for the new state."""
    value = op.get("value")
    if parts[:1] not in ([], ["services"]):
        return []
    if len(parts) <= 2:  # the document, all services, or one service

        def secrets_in(d: Any) -> list[str]:
            names = [parts[1]] if len(parts) == 2 else list(_services(d))
            return [f for n in names for f in _secret_keys(_env_of(d, n), n)]

        new = secrets_in(after) if after is not None else _value_secrets(value, parts)
        return secrets_in(doc) + new
    service = parts[1]
    if parts[2] != "environment":
        return []
    old_env = _env_of(doc, service)
    if len(parts) == 3:  # the whole environment
        new_env = _env_of(after, service) if after is not None else value
        return _secret_keys(old_env, service) + _secret_keys(new_env, service)
    key = parts[3]
    out = [f"{service}: {key}"] if SECRET_KEY.search(key) else []
    if key.isdigit() or key == "-":
        if isinstance(value, str):  # a list-form entry, "KEY=value"
            out += _secret_keys([value], service)
        if isinstance(old_env, list) and key.isdigit() and int(key) < len(old_env):
            if op.get("op") in ("replace", "remove"):
                out += _secret_keys([old_env[int(key)]], service)
        elif after is None and key.isdigit() and _secret_keys(old_env, service):
            out.append(f"{service}: environment")  # an index that cannot be resolved
    return out


def _value_secrets(value: Any, parts: list[str]) -> list[str]:
    """Secret keys in the value of an operation on the document, services, or a service."""
    if len(parts) == 2 and isinstance(value, dict):
        return _secret_keys(value.get("environment"), parts[1])
    if len(parts) == 1 and isinstance(value, dict):
        return [
            f
            for n, svc in value.items()
            if isinstance(svc, dict)
            for f in _secret_keys(svc.get("environment"), n)
        ]
    if len(parts) == 0 and isinstance(value, dict):
        return _value_secrets(value.get("services"), ["services"])
    return []


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
    known = _known(compose)
    log = [f"  {mask_text(line, known)}" for line in lines[-25:]] or ["  (none)"]
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
    # Masked once, here: every later use (escalations, approvals, fix reasons, rejected
    # records, events) reads this text. The patch stays as proposed: it is what is judged.
    root_cause = mask_text(diag.root_cause, _known(compose))
    patch = [op.model_dump(exclude={"value"} if op.op == "remove" else set()) for op in diag.patch]
    if not patch:
        return Proposal(tier, problem.signature, root_cause, [], "give_up")
    verdict, reasons = judge(patch, declared, init_services(compose), compose)
    return Proposal(tier, problem.signature, root_cause, patch, verdict, reasons)
