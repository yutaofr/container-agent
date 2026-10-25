"""Secrets in text a model or a person reads: what counts as one, and masking it.

A model never needs a secret value to diagnose a cluster, and anything it reads may come
back in a patch or a root cause. So every prompt, and every model text shown to a person,
goes through these functions. They mask output copies only; the configuration and the
patches under judgement keep their values.

Three things are masked (design F1, section 4):

- a value in a credential position, whether or not it is known: MySQL's -p<value> after a
  MySQL-family client in the same simple command, --password=<value> and --password <value>
  for any command, URL user info, and an assignment to a secret-named key;
- a known secret value of LONG_SECRET characters or more, anywhere;
- a whole shell field that uses syntax the tokenizer does not follow (command substitution,
  backticks, here-documents, eval, a nested sh -c, unbalanced quotes) and holds a trigger:
  a credential-position value, found in the raw text, that is not a pure reference.

A pure reference is exactly $NAME or ${NAME}, optionally quoted; the container fills it in,
so it is no secret. Short values such as "app" or "root" are also user and database names:
masking every occurrence would destroy diagnostics, so they are masked only in credential
positions.
"""

import copy
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SECRET_KEY = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
MASK = "******"
LONG_SECRET = 8

_REFERENCE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_CLIENT = re.compile(r"mysql\w*|mariadb[\w-]*")
_SHELLS = frozenset({"sh", "bash", "ash", "dash", "zsh"})

# Raw-text patterns: no structure, used for triggers and for text that is not a command.
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"'`;|&()<>]+)"
_RAW_CLIENT = re.compile(r"(?:^|[\s/\"'`(;|&])(?:mysql\w*|mariadb[\w-]*)(?=$|[\s\"'`);|&])")
_RAW_P = re.compile(r"(?<![^\s\"'`(;|&])(-p)" + _VALUE)
_PASSWORD_OPT = re.compile(r"(--password(?:=|\s+))" + _VALUE)
_URL_USERINFO = re.compile(r"(://[^:/@\s\"']+:)([^@\s\"']+)(@)")
_ASSIGNMENT = re.compile(
    r"([\w.-]*(?:password|passwd|pwd|secret|token|api_?key|access_?key)[\w.-]*[\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s\"'&;,]+)",
    re.IGNORECASE,
)


def is_reference(value: str) -> bool:
    """True when the whole value, outer quotes removed, is $NAME or ${NAME}."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return _REFERENCE.fullmatch(v) is not None


def trigger(text: str) -> bool:
    """True when the raw text holds a credential-position value that is not a reference."""
    if _RAW_CLIENT.search(text) and any(
        not is_reference(m.group(2)) for m in _RAW_P.finditer(text)
    ):
        return True
    for pattern, group in ((_PASSWORD_OPT, 2), (_URL_USERINFO, 2), (_ASSIGNMENT, 2)):
        if any(not is_reference(m.group(group)) for m in pattern.finditer(text)):
            return True
    return False


def mask_text(text: str, known: Iterable[str] = (), *, short_p: bool = True) -> str:
    """Mask URL passwords, secret-named assignments, --password values, and long known
    values anywhere in a string (a log line, an environment value, a root cause). In text
    that names a MySQL-family client, -p<value> is masked too; mask_shell and mask_argv
    turn that off (short_p=False) because they know each command's own words."""

    def keep_refs(m: re.Match[str]) -> str:
        value = m.group(2)
        return (
            m.group(0)
            if is_reference(value)
            else m.group(1) + MASK + m.group(0)[m.end(2) - m.start() :]
        )

    text = _URL_USERINFO.sub(keep_refs, text)
    text = _ASSIGNMENT.sub(keep_refs, text)
    text = _PASSWORD_OPT.sub(keep_refs, text)
    if short_p and _RAW_CLIENT.search(text):
        text = _RAW_P.sub(keep_refs, text)
    for value in sorted(
        {k for k in known if len(k) >= LONG_SECRET and not is_reference(k)}, key=len, reverse=True
    ):
        text = text.replace(value, MASK)
    return text


@dataclass(frozen=True)
class _Word:
    start: int
    end: int
    value: str  # quotes removed


def _parse(text: str) -> tuple[list[list[_Word]], bool]:
    """Split shell text into simple commands of words, following quotes. The second value
    is True when the text uses syntax this tokenizer does not follow."""
    commands: list[list[_Word]] = [[]]
    unsupported = False
    start: int | None = None
    value: list[str] = []
    i, n = 0, len(text)

    def end_word(at: int) -> None:
        nonlocal start, value
        if start is not None:
            commands[-1].append(_Word(start, at, "".join(value)))
        start, value = None, []

    while i < n:
        c = text[i]
        if c in " \t":
            end_word(i)
            i += 1
        elif c in ";|&\n":
            end_word(i)
            if commands[-1]:
                commands.append([])
            i += 1
        elif c in "<>":
            end_word(i)
            if text.startswith("<<", i) or text[i + 1 : i + 2] == "(":
                unsupported = True
            i += 1
        elif c in "()`":
            end_word(i)
            unsupported = True
            i += 1
        else:
            if start is None:
                start = i
            if c == "\\":
                value.append(text[i + 1 : i + 2])
                i += 2
            elif c == "'":
                j = text.find("'", i + 1)
                if j < 0:
                    return commands, True
                value.append(text[i + 1 : j])
                i = j + 1
            elif c == '"':
                j = i + 1
                while j < n and text[j] != '"':
                    if text[j] == "\\":
                        j += 1
                    elif text[j] == "`" or text.startswith("$(", j):
                        unsupported = True
                    j += 1
                if j >= n:
                    return commands, True
                value.append(text[i + 1 : j])
                i = j + 1
            elif text.startswith("$(", i):
                unsupported = True
                value.append("$")
                i += 1
            else:
                value.append(c)
                i += 1
    end_word(n)
    for words in commands:
        names = [posixpath.basename(w.value) for w in words]
        if "eval" in names or any(
            a in _SHELLS and b == "-c" for a, b in zip(names, names[1:], strict=False)
        ):
            unsupported = True
    return [c for c in commands if c], unsupported


def _replacements(text: str, words: list[_Word]) -> list[tuple[int, int, str]]:
    """(start, end, replacement) for each credential-position value in one simple command."""
    out: list[tuple[int, int, str]] = []
    client = False
    for k, w in enumerate(words):
        raw = text[w.start : w.end]
        if _CLIENT.fullmatch(posixpath.basename(w.value)):
            client = True
        elif client and w.value.startswith("-p") and len(w.value) > 2:
            if raw.startswith("-p"):
                if not is_reference(raw[2:]):
                    out.append((w.start + 2, w.end, MASK))
            else:
                out.append((w.start, w.end, "-p" + MASK))
        if w.value.startswith("--password="):
            cut = raw.find("=") + 1
            if not is_reference(raw[cut:]):
                out.append((w.start + cut, w.end, MASK))
        elif w.value == "--password" and k + 1 < len(words):
            nxt = words[k + 1]
            if not is_reference(text[nxt.start : nxt.end]):
                out.append((nxt.start, nxt.end, MASK))
    return out


def mask_shell(text: str, known: Iterable[str] = ()) -> str:
    """Mask credential positions in shell text; mask the whole field when it uses syntax
    the tokenizer does not follow and holds a trigger."""
    commands, unsupported = _parse(text)
    if unsupported:
        return MASK if trigger(text) else mask_text(text, known, short_p=False)
    edits = sorted((r for words in commands for r in _replacements(text, words)), reverse=True)
    for start, end, replacement in edits:
        text = text[:start] + replacement + text[end:]
    return mask_text(text, known, short_p=False)


def mask_argv(argv: list[str], known: Iterable[str] = ()) -> list[str]:
    """Mask credential positions in an argument array; a sh -c script is shell text."""
    out = list(argv)
    client, k = False, 0
    while k < len(argv):
        arg, base = argv[k], posixpath.basename(argv[k])
        if base in _SHELLS and k + 2 < len(argv) and argv[k + 1] == "-c":
            out[k + 2] = mask_shell(argv[k + 2], known)
            k += 3
            continue
        if _CLIENT.fullmatch(base):
            client = True
        elif client and arg.startswith("-p") and len(arg) > 2 and not is_reference(arg[2:]):
            out[k] = "-p" + MASK
        if arg.startswith("--password=") and not is_reference(arg[len("--password=") :]):
            out[k] = "--password=" + MASK
        elif arg == "--password" and k + 1 < len(argv) and not is_reference(argv[k + 1]):
            out[k + 1] = MASK
            out[k] = mask_text(arg, known, short_p=False)
            k += 2
            continue
        out[k] = mask_text(out[k], known, short_p=False)
        k += 1
    return out


def known_secrets(compose: dict[str, Any]) -> set[str]:
    """Values of secret-named environment keys in every service, mapping or list form;
    empty values and pure references are no secrets."""
    values: set[str] = set()
    for svc in (compose.get("services") or {}).values():
        for key, value in _env_pairs((svc or {}).get("environment")):
            if SECRET_KEY.search(key) and value and not is_reference(value):
                values.add(value)
    return values


def _env_pairs(env: Any) -> list[tuple[str, str]]:
    if isinstance(env, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in env.items()]
    if isinstance(env, list):
        return [tuple(str(e).partition("=")[::2]) for e in env]  # type: ignore[misc]
    return []


def mask_compose(compose: dict[str, Any], known: Iterable[str]) -> dict[str, Any]:
    """A masked copy of a compose document: secret-named environment values by key,
    command fields with their argument context, every other string as text."""
    known = set(known)
    out = copy.deepcopy(compose)
    for svc in (out.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        for field, value in svc.items():
            if field == "environment":
                svc[field] = _mask_env(value, known)
            elif field in ("command", "entrypoint"):
                svc[field] = _mask_command(value, known)
            elif field == "healthcheck" and isinstance(value, dict):
                svc[field] = {
                    k: _mask_test(v, known) if k == "test" else _walk(v, known)
                    for k, v in value.items()
                }
            else:
                svc[field] = _walk(value, known)
    return out


def _mask_env(env: Any, known: set[str]) -> Any:
    if isinstance(env, dict):
        return {k: MASK if SECRET_KEY.search(str(k)) else _walk(v, known) for k, v in env.items()}
    if isinstance(env, list):
        out = []
        for entry in env:
            key, sep, value = str(entry).partition("=")
            out.append(
                f"{key}={MASK}" if sep and SECRET_KEY.search(key) else mask_text(str(entry), known)
            )
        return out
    return _walk(env, known)


def _mask_command(value: Any, known: set[str]) -> Any:
    if isinstance(value, list):
        return mask_argv([str(v) for v in value], known)
    if isinstance(value, str):
        return mask_shell(value, known)
    return value


def _mask_test(value: Any, known: set[str]) -> Any:
    if isinstance(value, list) and value[:1] == ["CMD-SHELL"]:
        return ["CMD-SHELL", *[mask_shell(str(v), known) for v in value[1:]]]
    return _mask_command(value, known)


def _walk(value: Any, known: set[str]) -> Any:
    if isinstance(value, str):
        return mask_text(value, known)
    if isinstance(value, dict):
        return {k: _walk(v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, known) for v in value]
    return value
