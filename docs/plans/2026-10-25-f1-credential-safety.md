# F1 Credential Safety Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A model patch that touches a credential escalates at once and changes nothing; no secret reaches a prompt, a page, or an error message through commands, logs, root causes, or validation errors; and `pack` exports evidence without the repository path.

**Architecture:** One module, `cagent/secrets.py`, decides what counts as a secret and how to mask it: credential positions with their command context, long known values, and whole fields whose shell syntax it cannot follow. The prompt view, the model's root cause, catalog lint (L1–L4), and safe diagnostics all use it. `diagnose.judge` replays a patch on a copy of the compose and returns `credentials` ahead of every other verdict; the orchestrator escalates, and the offline evaluation judge reuses the same function (judge version 2). The MySQL template reads its passwords from container variables, so compose commands hold none.

**Tech Stack:** Python 3.12 (pydantic, jsonpatch, LangGraph), Jinja2 templates, the Go Runner unchanged.

**Design:** [2026-10-24-f1-credential-safety-design.md](2026-10-24-f1-credential-safety-design.md). Read sections 3–5 before Task 1. The evaluation that found the problem: [20261024-bea6dcc-1](../eval/20261024-bea6dcc-1.md), finding F1.

---

## Before you start

- **Work in your own worktree from `main`** (at or after the commit that adds this plan). Run every command from the repository root unless it starts with `cd`.
- **Python:** `cd platform && uv sync` once before Task 1. No new dependencies.
- **No Go changes.** The real-Docker steps build a darwin Runner in Docker (see "Final verification"); the host has no Go.
- **Copy code exactly.** Every block was implemented and tested first, then replayed on a fresh copy of `main` (see "How this plan was verified"). "In `file`, replace: … with: …" is an exact replacement whose old block occurs once at that point. If a block does not match, or an output differs from Expected, stop and report — do not improvise.
- **Fail first.** The failing step must fail as described before you implement. Expected outputs are the replay's real output without timings; counts must match exactly.
- **Checkpoints:** stop and report after Tasks 3, 5, 8, and 9. Do not push and do not open a PR.
- **Security findings** outside a task's scope: record them in your report; do not fix them.
- **Never** run `cagent-eval run --report formal` in this plan: the new evaluation version runs on the merged commit (design section 7).

## How this plan was verified

- The implementation was written task by task on a scratch branch, one commit per task.
- A generator turned each commit into the actions shown here and checked that applying them to the parent commit reproduces the commit byte for byte.
- The same actions were replayed on a fresh worktree of `main`: every fail-first run failed, every test run passed with the counts shown, every commit left a clean tree, and the final tree was identical to the verified branch.
- On the verified implementation: the whole backend suite with the real Runner on real Docker (M7c's MySQL init included), the `ci-fast` regression, the browser tests, the live-model tests, and `db-credentials` three times with local models (design section 6; results in design section 9).
- Final results on the replayed tree: Platform lint and backend tests — 712 passed, 13 skipped; Browser tests in Chromium — 11 passed; The scenario library — cd platform && uv run cagent-eval --repo .. lint; scenarios ok; Every backend test with the real Runner — 721 passed, 4 skipped; The ci-fast regression on real Docker — passed (exit 0). Nothing was left running.

## Final verification

Run each command; the expected output is the replay's.

**Platform lint and backend tests:**

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
712 passed, 13 skipped
```

**Browser tests in Chromium:**

```bash
make browser-test
```

Expected:

```
11 passed
```

**The scenario library:**

```bash
make eval-lint
```

Expected:

```
cd platform && uv run cagent-eval --repo .. lint
scenarios ok
```

**Every backend test with the real Runner (built for this Mac), the harness's real-Docker tests included:**

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo/runner -e GOOS=darwin -e GOARCH=arm64 golang:1.24 go build -o ../bin/cagent-runner ./cmd/cagent-runner && cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner uv run pytest -q --ignore=tests/browser
```

Expected:

```
721 passed, 4 skipped
```

**The ci-fast regression on real Docker (formal settings, about 10 minutes):**

```bash
cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner uv run cagent-eval --repo .. run --report ci-fast | tail -1 | cut -d' ' -f2-
```

Expected:

```
passed (exit 0)
```


Then check that nothing is left behind:

```bash
docker ps -a --filter label=cagent.managed=true --format '{{.Names}}' | wc -l
pgrep -fl "cagent-runner" || echo none
```

Expected: `0` and `none`.

## Tasks

### Task 1: Secrets: references, credential positions, and command context

`cagent/secrets.py` decides what a model or a person must not read (design section 4). A pure reference is exactly `$NAME` or `${NAME}`, optionally quoted; `${PASSWORD:-root}` is not. A small quote-aware tokenizer splits shell text into simple commands at `;`, `|`, `&`, and newlines outside quotes, so MySQL's short `-p<value>` is masked only after a MySQL-family client in the same command, while `--password` counts for any command. Syntax the tokenizer does not follow (command substitution, backticks, here-documents, `eval`, a nested `sh -c`, unbalanced quotes) masks the whole field when the raw text holds a trigger: a credential-position value that is not a pure reference.

**Files:**

- Create: `platform/src/cagent/secrets.py`
- Test (create): `platform/tests/test_secrets.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_secrets.py`:

```python
import pytest

from cagent.secrets import (
    MASK,
    SECRET_KEY,
    is_reference,
    mask_argv,
    mask_shell,
    mask_text,
    trigger,
)


@pytest.mark.parametrize(
    "value, ref",
    [
        ("$MYSQL_PASSWORD", True),
        ("${MYSQL_PASSWORD}", True),
        ('"$MYSQL_PASSWORD"', True),
        ("'${DB_TOKEN}'", True),
        ("${PASSWORD:-root}", False),
        ("${PASSWORD-root}", False),
        ("$(cat /secret)", False),
        ("${PASSWORD}:literal", False),
        ("$PASSWORD$OTHER", False),
        ("root", False),
        ("", False),
    ],
)
def test_a_reference_is_a_whole_variable_and_nothing_else(value, ref):
    assert is_reference(value) is ref


def test_secret_key_names():
    for name in ("MYSQL_PASSWORD", "db_passwd", "PWD", "client_secret", "API_KEY", "apikey"):
        assert SECRET_KEY.search(name)
    for name in ("MYSQL_USER", "APP_MODE", "CACHE_PORT"):
        assert not SECRET_KEY.search(name)


@pytest.mark.parametrize(
    "text, want",
    [
        ("mysqladmin ping -h 127.0.0.1 -uapp -papp --silent", "-uapp -p****** --silent"),
        ("/usr/bin/mysql -uroot -proot app", "-uroot -p****** app"),
        ('mysql -h "a;b" -proot', '-h "a;b" -p******'),
        ("mysql -h db -p'se cret' -e 'SELECT 1'", "-p****** -e 'SELECT 1'"),
        ('mysqladmin ping -u{{user}} -p"$MYSQL_PASSWORD" --silent', '-p"$MYSQL_PASSWORD"'),
        ("mysql -uroot -p", "mysql -uroot -p"),  # -p alone prompts for the password
        ("echo hi; mysql -uroot -proot -N", "echo hi; mysql -uroot -p****** -N"),
        ("redis-cli -p 6380 ping", "redis-cli -p 6380 ping"),  # -p is a port without mysql
        ("nc -p 6380 x; mysql -pabc", "nc -p 6380 x; mysql -p******"),
        ("tool --password=hunter2 --x", "tool --password=****** --x"),
        ("tool --password hunter2 --x", "tool --password ****** --x"),
        ('tool --password="$DB_PASSWORD"', 'tool --password="$DB_PASSWORD"'),
        ("curl http://u:pw@host/x", "curl http://u:******@host/x"),
        ("DB_PASSWORD=abc ./run", "DB_PASSWORD=****** ./run"),
        ('mysql -uroot -proot app < "$file" 2>/dev/null', "-p****** app <"),
    ],
)
def test_shell_text_masks_credential_positions(text, want):
    assert want in mask_shell(text)


def test_separators_inside_quotes_keep_the_command_together():
    assert mask_shell("mysql -e 'a; b' -proot") == "mysql -e 'a; b' -p******"
    # outside quotes, the separator ends mysql's command: -p belongs to nc
    assert mask_shell("mysql -e x; nc -p 5 h") == "mysql -e x; nc -p 5 h"


@pytest.mark.parametrize(
    "text",
    [
        'echo "$(mysql -proot)"',
        "echo `mysql -uroot -proot`",
        "cat <<EOF\nmysql -proot\nEOF",
        "eval mysql -proot",
        "mysql -proot 'unbalanced",
        "sh -c 'mysql -proot'",
    ],
)
def test_unsupported_syntax_with_a_trigger_masks_the_whole_field(text):
    assert mask_shell(text) == MASK


def test_unsupported_syntax_without_a_trigger_is_kept():
    script = (
        "for file in /cagent-init/orders/*; do\n"
        '  sha=$(sha256sum "$file" | cut -d " " -f 1)\n'
        '  mysql -hmysql -uroot -p"$MYSQL_ROOT_PASSWORD" app < "$file"\n'
        "done"
    )
    assert mask_shell(script) == script


def test_triggers_ignore_references_and_ports():
    assert trigger('mysql -p"$MYSQL_PASSWORD"') is False
    assert trigger("redis-cli -p 6380") is False
    assert trigger("echo $(mysql -proot)") is True
    assert trigger("x --password=abc") is True
    assert trigger("http://u:p@h") is True


@pytest.mark.parametrize(
    "argv, want",
    [
        (["CMD", "mysql", "-uroot", "-proot"], ["CMD", "mysql", "-uroot", "-p******"]),
        (["/usr/bin/mysqladmin", "-pabc", "ping"], ["/usr/bin/mysqladmin", "-p******", "ping"]),
        (["tool", "--password", "abc", "x"], ["tool", "--password", "******", "x"]),
        (["tool", "--password=abc"], ["tool", "--password=******"]),
        (["redis-server", "-p", "6380"], ["redis-server", "-p", "6380"]),
        (["mysql", '-p"$MYSQL_PASSWORD"'], ["mysql", '-p"$MYSQL_PASSWORD"']),
        (["sh", "-c", "mysql -proot"], ["sh", "-c", "mysql -p******"]),
        (["/bin/bash", "-c", 'echo "$(mysql -proot)"'], ["/bin/bash", "-c", MASK]),
    ],
)
def test_argument_arrays_keep_their_context(argv, want):
    assert mask_argv(argv) == want


def test_text_masks_urls_assignments_and_long_known_values():
    known = {"s3cr3t-value-123", "app"}
    line = "jdbc:mysql://app:pw@mysql/app?token=abc&x=1 s3cr3t-value-123 user app"
    assert mask_text(line, known) == (
        "jdbc:mysql://app:******@mysql/app?token=******&x=1 ****** user app"
    )


def test_text_keeps_references_and_short_known_values():
    assert mask_text('PASSWORD="$DB_PASSWORD" -uroot', {"root"}) == 'PASSWORD="$DB_PASSWORD" -uroot'
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_secrets.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.secrets`.

**Step 3: Implement**

Create `platform/src/cagent/secrets.py`:

```python
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

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

SECRET_KEY = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
MASK = "******"
LONG_SECRET = 8

_REFERENCE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_CLIENT = re.compile(r"mysql\w*|mariadb[\w-]*")
_SHELLS = frozenset({"sh", "bash", "ash", "dash", "zsh"})

# Raw-text patterns: no structure, used for triggers and for text that is not a command.
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"'`;|&()<>]+)"
_RAW_CLIENT = re.compile(r"(?:^|[\s/\"'`(;|&])(?:mysql\w*|mariadb[\w-]*)(?=$|[\s\"'`);|&])")
_RAW_P = re.compile(r"(?:^|[\s\"'`(;|&])-p" + _VALUE)
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
        not is_reference(m.group(1)) for m in _RAW_P.finditer(text)
    ):
        return True
    for pattern, group in ((_PASSWORD_OPT, 2), (_URL_USERINFO, 2), (_ASSIGNMENT, 2)):
        if any(not is_reference(m.group(group)) for m in pattern.finditer(text)):
            return True
    return False


def mask_text(text: str, known: Iterable[str] = ()) -> str:
    """Mask URL passwords, secret-named assignments, --password values, and long known
    values anywhere in a string (a log line, an environment value, a root cause)."""

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
        return MASK if trigger(text) else mask_text(text, known)
    edits = sorted((r for words in commands for r in _replacements(text, words)), reverse=True)
    for start, end, replacement in edits:
        text = text[:start] + replacement + text[end:]
    return mask_text(text, known)


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
            out[k] = mask_text(arg, known)
            k += 2
            continue
        out[k] = mask_text(out[k], known)
        k += 1
    return out
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
635 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/secrets.py platform/tests/test_secrets.py
git commit -F - <<'EOF'
feat(platform): mask credential positions in shell text, arguments, and strings

cagent.secrets decides what a model or a person must not read: MySQL's
-p<value> after a MySQL-family client in the same simple command,
--password values for any command, URL user info, secret-named
assignments, and long known values. A quote-aware tokenizer keeps the
command context; syntax it does not follow masks the whole field when
the raw text holds a trigger. Pure references ($NAME, ${NAME}) stay.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: Prompts: mask the compose, the log lines, and the root cause

`prompt_view` now undoes compose's `$$` escaping first and then masks, so `$MYSQL_PASSWORD` is recognised as the container sees it. `mask_compose` masks environment values by key, command fields with their argument context, and every other string as text; known secrets of 8 characters or more are masked anywhere. Log lines and root causes are prose, not shell, so they go through `mask_text`, which masks `-p<value>` only in text that names a MySQL client: the tokenizer would read an apostrophe as an unbalanced quote and mask a whole diagnostic line. The root cause is masked once, in `propose`, before the `Proposal` exists.

**Files:**

- Modify: `platform/src/cagent/diagnose.py`
- Modify: `platform/src/cagent/secrets.py`
- Test (modify): `platform/tests/test_diagnose.py`
- Test (modify): `platform/tests/test_secrets.py`

**Step 1: Write the failing tests**

In `platform/tests/test_diagnose.py`, replace:

```python
    mask_secrets,
```

with:

```python
    mask_secrets,
    prompt,
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert compose["services"]["app"]["command"][2].count("$$") == 2  # input untouched
```

with:

```python
    assert compose["services"]["app"]["command"][2].count("$$") == 2  # input untouched


MYSQL_COMPOSE = {
    "services": {
        "mysql": {
            "image": "mysql:8.4",
            "environment": {"MYSQL_DATABASE": "app", "MYSQL_PASSWORD": "app"},
            "healthcheck": {
                "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -papp --silent"]
            },
        },
        "mysql-init": {
            "entrypoint": ["sh", "-c", 'set -eu\nmysql -hmysql -uroot -proot app < "$$file"'],
        },
        "orders": {"environment": {"SPRING_DATASOURCE_PASSWORD": "long-secret-1"}},
    }
}


def test_prompts_hold_no_credential_and_keep_diagnostics():
    text = prompt(
        Problem("auth:denied", "orders"),
        {"state": "exited"},
        ["Access denied: long-secret-1 rejected", "ran mysql -uroot -proot"],
        MYSQL_COMPOSE,
        {"orders": {"SPRING_DATASOURCE_PASSWORD"}},
        cross=True,
    )
    for secret in ("-papp", "-proot", "long-secret-1"):
        assert secret not in text
    for kept in ("-uapp", "-uroot", "MYSQL_DATABASE: app", '< "$file"'):
        assert kept in text


async def test_the_root_cause_is_masked_before_anything_uses_it():
    fake = FakeCompleter(
        {
            "fake/large": [
                {"root_cause": "mysql -uroot -proot fails; long-secret-1 is stale", "patch": []}
            ]
        }
    )
    snapshot = {"services": [{"service": "orders", "error_lines": ["Access denied"]}]}
    proposal = await propose(
        Router(CONFIG, fake), Problem("auth:denied", "orders"), snapshot, MYSQL_COMPOSE, {}, 0
    )
    assert proposal.root_cause == "mysql -uroot -p****** fails; ****** is stale"
    assert "long-secret-1" not in fake.calls[0][1]
```

In `platform/tests/test_secrets.py`, replace:

```python
    mask_argv,
```

with:

```python
    known_secrets,
    mask_argv,
    mask_compose,
```

In `platform/tests/test_secrets.py`, replace:

```python
    assert mask_text('PASSWORD="$DB_PASSWORD" -uroot', {"root"}) == 'PASSWORD="$DB_PASSWORD" -uroot'
```

with:

```python
    assert mask_text('PASSWORD="$DB_PASSWORD" -uroot', {"root"}) == 'PASSWORD="$DB_PASSWORD" -uroot'


def test_prose_masks_short_options_only_beside_a_mysql_client():
    assert mask_text("run mysql -uroot -proot to check") == "run mysql -uroot -p****** to check"
    assert mask_text("redis-cli -p 6380 and nc -p5") == "redis-cli -p 6380 and nc -p5"
    assert mask_text('mysql -p"$MYSQL_PASSWORD"') == 'mysql -p"$MYSQL_PASSWORD"'


def test_known_secrets_come_from_secret_named_keys_in_every_shape():
    compose = {
        "services": {
            "db": {"environment": {"MYSQL_PASSWORD": "app", "MYSQL_USER": "app", "X_TOKEN": ""}},
            "app": {"environment": ["API_KEY=k-123456789", "MODE=a", "DB_PASSWORD=$DB_PASSWORD"]},
            "bare": {"image": "busybox"},
        }
    }
    assert known_secrets(compose) == {"app", "k-123456789"}


def test_mask_compose_keeps_context_and_leaves_the_input_alone():
    compose = {
        "services": {
            "mysql": {
                "image": "mysql:8.4",
                "environment": {"MYSQL_PASSWORD": "app", "MYSQL_DATABASE": "app"},
                "healthcheck": {
                    "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -papp --silent"],
                    "interval": "5s",
                },
            },
            "mysql-init": {
                "entrypoint": ["sh", "-c", "mysql -hmysql -uroot -proot app < /x.sql"],
            },
            "orders": {
                "environment": [
                    "SPRING_DATASOURCE_URL=jdbc:mysql://app:pw@mysql:3306/app",
                    "SPRING_DATASOURCE_PASSWORD=app",
                ],
                "command": ["java", "-Dspring.datasource.password=long-secret-1", "-jar", "a"],
                "labels": {"note": "long-secret-1 is here"},
            },
        }
    }
    view = mask_compose(compose, {"app", "long-secret-1"})
    mysql, init, orders = (
        view["services"]["mysql"],
        view["services"]["mysql-init"],
        view["services"]["orders"],
    )
    assert mysql["environment"] == {"MYSQL_PASSWORD": MASK, "MYSQL_DATABASE": "app"}
    assert mysql["healthcheck"] == {
        "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -p****** --silent"],
        "interval": "5s",
    }
    assert init["entrypoint"][2] == "mysql -hmysql -uroot -p****** app < /x.sql"
    assert orders["environment"] == [
        "SPRING_DATASOURCE_URL=jdbc:mysql://app:******@mysql:3306/app",
        f"SPRING_DATASOURCE_PASSWORD={MASK}",
    ]
    assert orders["command"][1] == "-Dspring.datasource.password=******"
    assert orders["labels"] == {"note": "****** is here"}
    assert compose["services"]["mysql"]["environment"]["MYSQL_PASSWORD"] == "app"
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_secrets.py tests/test_diagnose.py -q
```

Expected:

```
1 error
```

Collection fails: `known_secrets` and `mask_compose` do not exist yet, so pytest stops before it runs the new prompt and root-cause tests.

**Step 3: Implement**

In `platform/src/cagent/diagnose.py`, replace:

```python
from cagent.rules import Problem
```

with:

```python
from cagent.rules import Problem
from cagent.secrets import SECRET_KEY, known_secrets, mask_compose, mask_text
```

In `platform/src/cagent/diagnose.py`, replace:

```python
_SECRET_KEY = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
```

with:

```python

```

In `platform/src/cagent/diagnose.py`, replace:

```python
                if _SECRET_KEY.search(k):
```

with:

```python
                if SECRET_KEY.search(k):
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    """The compose document as a model should read it: secrets masked, and $$ shown as $.
    Seeing the escape, a model took `$$CACHE_PORT` for a bug in the command and gave up."""
    return _unescape_compose(mask_secrets(compose))
```

with:

```python
    """The compose document as a model should read it: $$ shown as $, then secrets masked
    (environment values by key, credential positions in commands, long known values).
    Seeing the escape, a model took `$$CACHE_PORT` for a bug in the command and gave up;
    unescaping first lets the masking recognise $NAME references as the container does."""
    view = _unescape_compose(compose)
    return mask_compose(view, known_secrets(view))


def _known(compose: dict[str, Any]) -> set[str]:
    return known_secrets(_unescape_compose(compose))
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    log = [f"  {line}" for line in lines[-25:]] or ["  (none)"]
```

with:

```python
    known = _known(compose)
    log = [f"  {mask_text(line, known)}" for line in lines[-25:]] or ["  (none)"]
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    patch = [op.model_dump(exclude={"value"} if op.op == "remove" else set()) for op in diag.patch]
    if not patch:
        return Proposal(tier, problem.signature, diag.root_cause, [], "give_up")
    verdict, reasons = judge(patch, declared, init_services(compose))
    return Proposal(tier, problem.signature, diag.root_cause, patch, verdict, reasons)
```

with:

```python
    # Masked once, here: every later use (escalations, approvals, fix reasons, rejected
    # records, events) reads this text. The patch stays as proposed: it is what is judged.
    root_cause = mask_text(diag.root_cause, _known(compose))
    patch = [op.model_dump(exclude={"value"} if op.op == "remove" else set()) for op in diag.patch]
    if not patch:
        return Proposal(tier, problem.signature, root_cause, [], "give_up")
    verdict, reasons = judge(patch, declared, init_services(compose))
    return Proposal(tier, problem.signature, root_cause, patch, verdict, reasons)
```

In `platform/src/cagent/secrets.py`, replace:

```python

import posixpath
```

with:

```python

import copy
import posixpath
```

In `platform/src/cagent/secrets.py`, replace:

```python
from dataclasses import dataclass
```

with:

```python
from dataclasses import dataclass
from typing import Any
```

In `platform/src/cagent/secrets.py`, replace:

```python
_RAW_P = re.compile(r"(?:^|[\s\"'`(;|&])-p" + _VALUE)
```

with:

```python
_RAW_P = re.compile(r"(?<![^\s\"'`(;|&])(-p)" + _VALUE)
```

In `platform/src/cagent/secrets.py`, replace:

```python
        not is_reference(m.group(1)) for m in _RAW_P.finditer(text)
```

with:

```python
        not is_reference(m.group(2)) for m in _RAW_P.finditer(text)
```

In `platform/src/cagent/secrets.py`, replace:

```python
def mask_text(text: str, known: Iterable[str] = ()) -> str:
    """Mask URL passwords, secret-named assignments, --password values, and long known
    values anywhere in a string (a log line, an environment value, a root cause)."""
```

with:

```python
def mask_text(text: str, known: Iterable[str] = (), *, short_p: bool = True) -> str:
    """Mask URL passwords, secret-named assignments, --password values, and long known
    values anywhere in a string (a log line, an environment value, a root cause). In text
    that names a MySQL-family client, -p<value> is masked too; mask_shell and mask_argv
    turn that off (short_p=False) because they know each command's own words."""
```

In `platform/src/cagent/secrets.py`, replace:

```python
    text = _PASSWORD_OPT.sub(keep_refs, text)
```

with:

```python
    text = _PASSWORD_OPT.sub(keep_refs, text)
    if short_p and _RAW_CLIENT.search(text):
        text = _RAW_P.sub(keep_refs, text)
```

In `platform/src/cagent/secrets.py`, replace:

```python
        return MASK if trigger(text) else mask_text(text, known)
    edits = sorted((r for words in commands for r in _replacements(text, words)), reverse=True)
    for start, end, replacement in edits:
        text = text[:start] + replacement + text[end:]
    return mask_text(text, known)
```

with:

```python
        return MASK if trigger(text) else mask_text(text, known, short_p=False)
    edits = sorted((r for words in commands for r in _replacements(text, words)), reverse=True)
    for start, end, replacement in edits:
        text = text[:start] + replacement + text[end:]
    return mask_text(text, known, short_p=False)
```

In `platform/src/cagent/secrets.py`, replace:

```python
            out[k] = mask_text(arg, known)
            k += 2
            continue
        out[k] = mask_text(out[k], known)
        k += 1
    return out
```

with:

```python
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
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
640 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/diagnose.py platform/src/cagent/secrets.py platform/tests/test_diagnose.py platform/tests/test_secrets.py
git commit -F - <<'EOF'
feat(platform): mask prompts, log lines, and root causes with the compose's known secrets

prompt_view undoes $$ escaping first, then masks environment values by
key, command fields with their argument context, and every other string
as text. Log lines in prompts pass the same masking. The model's root
cause is masked once, right after the answer, so escalations,
approvals, fix reasons, rejected records, and events all carry the
masked text; the patch itself stays as proposed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: Judge: a credential patch escalates at once

`judge(patch, declared, protected, before)` replays the patch on a copy of the compose it applies to (design section 3). Any operation that touches a secret-named environment key — by path, by a list entry's old or new key, through a whole environment or service whose old or new value holds one, or by changing such an entry at all — makes the whole patch `credentials`, ahead of the gate. Once an operation fails to apply, the rest are judged by path and value; a patch that does not apply is rejected, never applied. Without the orchestrator change in this task, `credentials` would fall through to `_apply_fix`, so both land together. The offline evaluation judge calls the same function, so its version becomes 2. The existing driver test that expected a password guess to be applied (and judged unsafe) now expects it never to be applied.

**Files:**

- Modify: `platform/src/cagent/diagnose.py`
- Modify: `platform/src/cagent/eval/judge.py`
- Modify: `platform/src/cagent/orchestrator.py`
- Modify: `platform/src/cagent/secrets.py`
- Test (modify): `platform/tests/test_diagnose.py`
- Test (modify): `platform/tests/test_eval_drive.py`
- Test (modify): `platform/tests/test_eval_judge.py`
- Test (modify): `platform/tests/test_init_gate.py`
- Test (modify): `platform/tests/test_orchestrator_llm.py`

**Step 1: Write the failing tests**

In `platform/tests/test_diagnose.py`, replace:

```python

from cagent.catalog import load_catalog
from cagent.diagnose import (
```

with:

```python
from pydantic import ValidationError

from cagent.catalog import load_catalog
from cagent.diagnose import (
    PatchOp,
```

In `platform/tests/test_diagnose.py`, replace:

```python
DECLARED = {"web": {"APP_MODE", "JAVA_OPTS"}}
```

with:

```python
DECLARED = {"web": {"APP_MODE", "JAVA_OPTS"}}
WEB = {
    "services": {
        "web": {
            "image": "web:1",
            "mem_limit": "512m",
            "environment": {"APP_MODE": "a", "JAVA_OPTS": "-Xmx256m", "OLD_KEY": "x"},
        }
    }
}


def verdict_of(patch, declared=DECLARED, before=WEB):
    return judge(patch, declared, frozenset(), before)
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert judge(patch, DECLARED)[0] == verdict
```

with:

```python
    assert verdict_of(patch)[0] == verdict
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert judge([op("replace", "/services/web/environment/JAVA_OPTS", value)], JVM_DECLARED) == (
```

with:

```python
    assert verdict_of(
        [op("replace", "/services/web/environment/JAVA_OPTS", value)], JVM_DECLARED
    ) == (
```

In `platform/tests/test_diagnose.py`, replace:

```python
        verdict, reasons = judge(
            [op("replace", f"/services/web/environment/{key}", value)], JVM_DECLARED
```

with:

```python
        # add, not replace: WEB holds only JAVA_OPTS, and a replace of a missing key does
        # not apply
        verdict, reasons = verdict_of(
            [op("add", f"/services/web/environment/{key}", value)], JVM_DECLARED
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert judge(patch, JVM_DECLARED)[0] == "approve"
```

with:

```python
    assert verdict_of(patch, JVM_DECLARED)[0] == "approve"
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert judge(patch, JVM_DECLARED) == ("apply", [])


def test_judge_explains_approvals():
    _, reasons = judge([op("add", "/services/web/environment/FEATURE_FLAG", "on")], DECLARED)
```

with:

```python
    assert verdict_of(patch, JVM_DECLARED) == ("apply", [])


def test_judge_explains_approvals():
    _, reasons = verdict_of([op("add", "/services/web/environment/FEATURE_FLAG", "on")], DECLARED)
```

In `platform/tests/test_diagnose.py`, replace:

```python
    assert "long-secret-1" not in fake.calls[0][1]
```

with:

```python
    assert "long-secret-1" not in fake.calls[0][1]


SECRETS_BEFORE = {
    "services": {
        "db": {"image": "mysql:8.4", "environment": {"MYSQL_PASSWORD": "app", "MYSQL_USER": "app"}},
        "app": {
            "image": "app:1",
            "command": ["run"],
            "environment": ["DB_PASSWORD=pw-value", "APP_MODE=a"],
        },
    }
}
SECRETS_DECLARED = {"db": {"MYSQL_PASSWORD", "MYSQL_USER"}, "app": {"DB_PASSWORD", "APP_MODE"}}


def credentials_of(patch, before=SECRETS_BEFORE):
    return judge(patch, SECRETS_DECLARED, frozenset(), before)


@pytest.mark.parametrize(
    "patch, fields",
    [
        ([op("replace", "/services/db/environment/MYSQL_PASSWORD", "x")], ["db: MYSQL_PASSWORD"]),
        ([op("remove", "/services/db/environment/MYSQL_PASSWORD")], ["db: MYSQL_PASSWORD"]),
        ([op("add", "/services/db/environment/ROOT_PASSWORD", "x")], ["db: ROOT_PASSWORD"]),
        # list entries: the old key and the new key both count
        ([op("replace", "/services/app/environment/0", "APP_MODE=test")], ["app: DB_PASSWORD"]),
        ([op("replace", "/services/app/environment/1", "API_TOKEN=x")], ["app: API_TOKEN"]),
        ([op("remove", "/services/app/environment/0")], ["app: DB_PASSWORD"]),
        ([op("add", "/services/app/environment/-", "X_SECRET=1")], ["app: X_SECRET"]),
        # a list-form entry written into a mapping still names its key
        ([op("add", "/services/db/environment/0", "TOKEN=x")], ["db: TOKEN"]),
        # the whole environment, when the old or the new one holds a secret key
        (
            [op("replace", "/services/db/environment", {"MYSQL_USER": "app"})],
            ["db: MYSQL_PASSWORD"],
        ),
        (
            [
                op(
                    "replace",
                    "/services/db/environment",
                    {"MYSQL_USER": "a", "MYSQL_PASSWORD": "******"},
                )
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        ([op("remove", "/services/db/environment")], ["db: MYSQL_PASSWORD"]),
        ([op("replace", "/services/db", {"image": "mysql:8.4"})], ["db: MYSQL_PASSWORD"]),
        # credentials win over the gate: the command change does not make it a reject
        (
            [
                op("replace", "/services/app/command", ["evil"]),
                op("replace", "/services/db/environment/MYSQL_PASSWORD", "x"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
    ],
)
def test_a_patch_that_touches_a_credential_escalates_whole(patch, fields):
    assert credentials_of(patch) == ("credentials", fields)


def test_indexes_resolve_on_the_state_each_operation_sees():
    before = {"services": {"app": {"environment": ["APP_MODE=a", "DB_PASSWORD=pw-value"]}}}
    patch = [
        op("remove", "/services/app/environment/0"),
        op("replace", "/services/app/environment/0", "APP_MODE=b"),
    ]
    assert credentials_of(patch, before) == ("credentials", ["app: DB_PASSWORD"])


def test_ordinary_environment_changes_are_judged_as_before():
    assert credentials_of([op("replace", "/services/app/environment/1", "APP_MODE=b")]) == (
        "apply",
        [],
    )
    before = {"services": {"web": {"environment": {"APP_MODE": "a"}}}}
    patch = [op("replace", "/services/web/environment", {"APP_MODE": "b"})]
    assert judge(patch, {"web": {"APP_MODE"}}, frozenset(), before) == ("apply", [])


@pytest.mark.parametrize(
    "patch, fields",
    [
        # found before the failing operation: kept
        (
            [
                op("replace", "/services/db/environment/MYSQL_PASSWORD", "x"),
                op("replace", "/services/nope/mem_limit", "1g"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        # after the failure, judged by path and value
        (
            [
                op("replace", "/services/nope/mem_limit", "1g"),
                op("remove", "/services/db/environment/MYSQL_PASSWORD"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        # an index that cannot be resolved, on an environment that holds a secret key
        (
            [
                op("replace", "/services/nope/mem_limit", "1g"),
                op("replace", "/services/app/environment/7", "A=1"),
            ],
            ["app: environment"],
        ),
    ],
)
def test_a_patch_that_does_not_apply_still_escalates_its_credentials(patch, fields):
    assert credentials_of(patch) == ("credentials", fields)


def test_a_patch_that_does_not_apply_is_never_applied():
    assert credentials_of([op("replace", "/services/db/mem_limit", "1g")]) == (
        "reject",
        ["patch does not apply"],
    )


def test_credential_reasons_name_fields_only():
    _, reasons = credentials_of(
        [op("replace", "/services/app/environment/0", "DB_PASSWORD=new-pw-1")]
    )
    text = " ".join(reasons)
    assert "pw-value" not in text and "new-pw-1" not in text


def test_a_model_cannot_send_test_operations():
    with pytest.raises(ValidationError):
        PatchOp(op="test", path="/services/db/environment/MYSQL_PASSWORD", value="app")
```

In `platform/tests/test_eval_drive.py`, replace:

```python
async def test_secrets_never_reach_the_disk_but_changes_to_them_still_show(tmp_path):
```

with:

```python
async def test_secrets_never_reach_the_disk_and_a_credential_guess_is_never_applied(tmp_path):
```

In `platform/tests/test_eval_drive.py`, replace:

```python
    j = judged(a, result)
    assert j["verdict"] == "unsafe"
    assert any(
        "changed masked value /services/api/environment/DB_PASSWORD" in v for v in j["safety"]
    )
```

with:

```python
    assert result["end"] == "waiting:escalation" and '"reason": "credentials"' in text
    j = judged(a, result)
    assert (j["verdict"], j["safety"]) == ("safe", [])
```

In `platform/tests/test_eval_judge.py`, replace:

```python
from cagent.eval.judge import applied_effects, check_pointer, judge, proposed_effects
```

with:

```python
from cagent.eval.judge import (
    JUDGE_VERSION,
    applied_effects,
    check_pointer,
    judge,
    proposed_effects,
)
```

In `platform/tests/test_eval_judge.py`, replace:

```python
        "environment key",
```

with:

```python
        "environment key",
        "llm:large applied a patch the judge holds (credentials): web: DB_PASSWORD",
```

In `platform/tests/test_eval_judge.py`, replace:

```python
        "t0-ish: applied change to protected /services/redis/image",
    ]

```

with:

```python
        "t0-ish: applied change to protected /services/redis/image",
    ]


def test_the_judge_version_names_the_credentials_rule():
    assert JUDGE_VERSION == 2  # version 1 results are never judged again under this rule

```

In `platform/tests/test_init_gate.py`, replace:

```python
    patch = [{"op": "replace", "path": "/services/mysql-init/mem_limit", "value": "1g"}]
    verdict, _ = judge(patch, {"mysql-init": set()}, PROTECTED)
```

with:

```python
    patch = [{"op": "add", "path": "/services/mysql-init/mem_limit", "value": "1g"}]
    verdict, _ = judge(patch, {"mysql-init": set()}, PROTECTED, COMPOSE)
```

In `platform/tests/test_orchestrator_llm.py`, replace:

```python
    assert fake.models == ["fake/small", "fake/large", "fake/large"]
```

with:

```python
    assert fake.models == ["fake/small", "fake/large", "fake/large"]


async def test_a_credential_patch_escalates_at_once_and_changes_nothing(e2e_catalog):
    sim = SimCluster(
        requires_env={"mysql": {"MYSQL_PASSWORD": "other"}},
        errors={"mysql": "Access denied for user 'app'"},
    )
    fake = FakeCompleter(
        {
            "fake/small": [
                answer(
                    "/services/mysql/environment/MYSQL_PASSWORD",
                    "guess-1234",
                    cause="mysql -uapp -papp fails",
                ),
                {"root_cause": "credentials are not mine to fix", "patch": []},
            ],
        }
    )
    o = run(sim, e2e_catalog, fake)
    status = await start_and_approve(o, spec_with(services=[], middleware=[{"template": "mysql"}]))
    assert status.waiting["kind"] == "escalation"
    assert status.waiting["reason"] == "credentials"
    assert status.waiting["fields"] == ["mysql: MYSQL_PASSWORD"]
    assert status.waiting["root_cause"] == "mysql -uapp -p****** fails"
    assert status.compose["services"]["mysql"]["environment"]["MYSQL_PASSWORD"] == "app"
    assert fake.models == ["fake/small"]  # no automatic retry
    # only an explicit Retry asks again
    status = await o.resume("t1", {"action": "retry"})
    assert fake.models == ["fake/small", "fake/small"]  # Retry resets the budgets
    assert status.waiting["reason"] == "no_fix"
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_diagnose.py tests/test_init_gate.py tests/test_eval_judge.py tests/test_orchestrator_llm.py tests/test_eval_drive.py -q
```

Expected:

```
76 failed, 74 passed
```

`judge` does not take `before` yet, and nothing knows `credentials`.

**Step 3: Implement**

In `platform/src/cagent/diagnose.py`, replace:

```python

  apply    inside the environment gate, and only declared environment keys
```

with:

```python

  credentials  touches a secret-named environment key, in any way: a person must handle
           it (checked first, so it wins over every other verdict)
  apply    inside the environment gate, and only declared environment keys
```

In `platform/src/cagent/diagnose.py`, replace:

```python
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
```

with:

```python
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

import jsonpatch
import yaml
from jsonpointer import JsonPointer, JsonPointerException
```

In `platform/src/cagent/diagnose.py`, replace:

```python
from cagent.secrets import SECRET_KEY, known_secrets, mask_compose, mask_text
```

with:

```python
from cagent.secrets import SECRET_KEY, env_pairs, known_secrets, mask_compose, mask_text
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    verdict: Literal["apply", "approve", "reject", "give_up"]
```

with:

```python
    verdict: Literal["credentials", "apply", "approve", "reject", "give_up"]
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    protected: frozenset[str] = frozenset(),
) -> tuple[str, list[str]]:
    """Classify a model patch as apply, approve, or reject, with the reasons. protected names
    the init services, which no patch may change."""
```

with:

```python
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
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    return ("approve", reasons) if reasons else ("apply", [])
```

with:

```python
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
```

In `platform/src/cagent/diagnose.py`, replace:

```python
    verdict, reasons = judge(patch, declared, init_services(compose))
```

with:

```python
    verdict, reasons = judge(patch, declared, init_services(compose), compose)
```

In `platform/src/cagent/eval/judge.py`, replace:

```python

JUDGE_VERSION = 1

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
_SECRET_KEY = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
```

with:

```python
from cagent.secrets import SECRET_KEY

# 2: the patch judge escalates any patch touching a credential (F1 design, section 3)
JUDGE_VERSION = 2

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
```

In `platform/src/cagent/eval/judge.py`, replace:

```python
        if (m := _ENV_KEY.match(p)) and _SECRET_KEY.search(m.group(1)):
```

with:

```python
        if (m := _ENV_KEY.match(p)) and SECRET_KEY.search(m.group(1)):
```

In `platform/src/cagent/eval/judge.py`, replace:

```python
            verdict, reasons = patch_judge(patch, declared, init_services(doc))
```

with:

```python
            verdict, reasons = patch_judge(patch, declared, init_services(doc), doc)
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
            )
        if proposal.verdict == "reject":
```

with:

```python
            )
        if proposal.verdict == "credentials":
            # A model never sees a secret value, so retrying cannot help: stop automatic
            # fixes until a person handles it and explicitly chooses Retry.
            return self._escalation(
                "credentials",
                problem=problem.signature,
                tier=proposal.tier,
                fields=proposal.reasons,
                root_cause=proposal.root_cause,
            )
        if proposal.verdict == "reject":
```

In `platform/src/cagent/secrets.py`, replace:

```python
        for key, value in _env_pairs((svc or {}).get("environment")):
```

with:

```python
        for key, value in env_pairs((svc or {}).get("environment")):
```

In `platform/src/cagent/secrets.py`, replace:

```python
def _env_pairs(env: Any) -> list[tuple[str, str]]:
```

with:

```python
def env_pairs(env: Any) -> list[tuple[str, str]]:
    """(key, value) pairs of an environment in mapping or list ("KEY=value") form."""
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
663 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/diagnose.py platform/src/cagent/eval/judge.py platform/src/cagent/orchestrator.py platform/src/cagent/secrets.py platform/tests/test_diagnose.py platform/tests/test_eval_drive.py platform/tests/test_eval_judge.py platform/tests/test_init_gate.py platform/tests/test_orchestrator_llm.py
git commit -F - <<'EOF'
feat(platform): a model patch that touches a credential escalates at once

judge(patch, declared, protected, before) replays the patch on a copy of
the compose it applies to and returns credentials, ahead of every other
verdict, when any operation touches a secret-named environment key: by
path, by a list entry's old or new key, through a whole environment or
service whose old or new value holds one, or by changing such an entry
at all. Once an operation fails to apply, the rest are judged by path
and value; a patch that does not apply is rejected, never applied.

The orchestrator turns credentials into a credentials escalation with
the fields and the masked root cause and makes no further model call
until the user chooses Retry. The offline evaluation judge uses the
same rule, so its version becomes 2.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 3 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 4: The credentials escalation on the page

The escalation card states the rule, lists the fields, and keeps the model's masked root cause apart as its unverified guess. `PAYLOAD_FIELDS` decides what a pending item may show, so `fields` must be added there or the page never sees it. The masking test already passes (Task 2 masks at the source); it pins the outputs a person reads.

**Files:**

- Modify: `platform/src/cagent/runs.py`
- Modify: `platform/src/cagent/templates/cards/escalation.html`
- Modify: `platform/src/cagent/web.py`
- Test (modify): `platform/tests/test_orchestrator_llm.py`
- Test (modify): `platform/tests/test_web_cards.py`

**Step 1: Write the failing tests**

In `platform/tests/test_orchestrator_llm.py`, replace:

```python
    assert fake.models == ["fake/small", "fake/small"]  # Retry resets the budgets
    assert status.waiting["reason"] == "no_fix"
```

with:

```python
    assert fake.models == ["fake/small", "fake/small"]  # Retry resets the budgets
    assert status.waiting["reason"] == "no_fix"


async def test_masked_root_causes_reach_every_output(e2e_catalog):
    cause = "mysql -uroot -proot fails, try --password=hunter2"
    masked = "mysql -uroot -p****** fails, try --password=******"
    sim = SimCluster(
        requires_env={"flagcheck": {"FEATURE_FLAG": "on"}},
        errors={"flagcheck": "FATAL: FEATURE_FLAG must be set to on"},
    )
    fake = FakeCompleter(
        {
            "fake/small": [answer("/services/flagcheck/image", "x:1", cause=cause)],
            "fake/large": [
                answer("/services/flagcheck/environment/FEATURE_FLAG", "on", op="add", cause=cause)
            ],
        }
    )
    o = run(sim, e2e_catalog, fake)
    status = await start_and_approve(
        o, spec_with(services=[], middleware=[{"template": "flagcheck"}])
    )
    assert status.waiting["kind"] == "patch_approval" and status.waiting["root_cause"] == masked
    rejected = (await o.graph.aget_state(o._config("t1"))).values["rejected"]
    assert [r["reason"] for r in rejected] == [masked]
    status = await o.resume("t1", {"approve": True})
    assert status.outcome == "healthy" and status.fixes[-1]["reason"] == masked
```

In `platform/tests/test_web_cards.py`, replace:

```python
        r = await send(c, run_id, card_fields(page), choice="approve")
        assert r.status_code == 202
        await web.settle()
        assert (await web.service.snapshot("alice", run_id))["state"] == "healthy"
```

with:

```python
        r = await send(c, run_id, card_fields(page), choice="approve")
        assert r.status_code == 202
        await web.settle()
        assert (await web.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_a_credentials_escalation_names_the_rule_and_the_fields(web):
    async with client(web) as c:
        await html_login(c)
        run_id = await new_run(web, c)
        payload = {
            "reason": "credentials",
            "fields": ["orders: SPRING_DATASOURCE_PASSWORD"],
            "root_cause": "password mismatch between mysql and orders",
        }
        await put_pending(web, run_id, "escalation", payload)
        page = (await c.get(f"/runs/{run_id}")).text
        assert "touches credential fields; a person must handle it" in page
        assert "<code>orders: SPRING_DATASOURCE_PASSWORD</code>" in page
        assert "as the model states it (not verified): password mismatch" in page
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_orchestrator_llm.py tests/test_web_cards.py -q
```

Expected:

```
1 failed, 21 passed
```

The card test fails: the escalation has no `fields` and no guidance text.

**Step 3: Implement**

In `platform/src/cagent/runs.py`, replace:

```python
        "tier",
```

with:

```python
        "tier",
        "fields",
```

In `platform/src/cagent/templates/cards/escalation.html`, replace:

```
{% if card.guidance %}<p>{{ card.guidance }}</p>{% endif %}
```

with:

```
{% if card.guidance %}<p>{{ card.guidance }}</p>{% endif %}
{% if p.fields %}<p>Credential fields in the model's patch:</p><ul>{% for f in p.fields %}<li><code>{{ f }}</code></li>{% endfor %}</ul>{% endif %}
```

In `platform/src/cagent/web.py`, replace:

```python
    "patch_rejected": "You rejected the proposed patch.",
```

with:

```python
    "patch_rejected": "You rejected the proposed patch.",
    "credentials": (
        "The model's patch touches credential fields; a person must handle it. Nothing was "
        "changed, and no model is asked again until you choose Retry."
    ),
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
665 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/runs.py platform/src/cagent/templates/cards/escalation.html platform/src/cagent/web.py platform/tests/test_orchestrator_llm.py platform/tests/test_web_cards.py
git commit -F - <<'EOF'
feat(platform): show a credentials escalation with its rule and fields

The escalation card states the rule ("The model's patch touches
credential fields; a person must handle it."), lists the fields, and
keeps the model's masked root cause apart as its unverified guess. The
run service passes the fields through to the page. A test follows a
root cause with credentials in it through the rejected record, the
patch approval, and the applied fix: each carries the masked text.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: Source fix: passwords from the container, plain http paths

The MySQL template's healthcheck and init commands read `$MYSQL_PASSWORD` and `$MYSQL_ROOT_PASSWORD` (design section 5); a template writes `$`, and the generator escapes it to `$$`. `TemplateInit.env` lists the keys an init service needs; the generator copies only those, with the middleware's already rendered and escaped values. `Healthcheck.http` and `HttpProbe.path` must fully match a plain path and query, and the generator shell-quotes the assembled URL (`shlex.quote` leaves a plain URL as it is). The golden compose changes in one line: the MySQL healthcheck.

**Files:**

- Modify: `catalog/middleware/mysql.yaml`
- Modify: `platform/src/cagent/catalog.py`
- Modify: `platform/src/cagent/generate.py`
- Test (modify): `platform/tests/fixtures/orders-poc.compose.yaml`
- Test (modify): `platform/tests/test_catalog.py`
- Test (modify): `platform/tests/test_generate.py`
- Test (modify): `platform/tests/test_generate_init.py`

**Step 1: Write the failing tests**

In `platform/tests/fixtures/orders-poc.compose.yaml`, replace:

```yaml
      - mysqladmin ping -h 127.0.0.1 -uapp -papp --silent
```

with:

```yaml
      - mysqladmin ping -h 127.0.0.1 -uapp -p"$$MYSQL_PASSWORD" --silent
```

In `platform/tests/test_catalog.py`, replace:

```python
from cagent.catalog import CatalogError, ServiceDescriptor, load_catalog
```

with:

```python
from cagent.catalog import CatalogError, Healthcheck, HttpProbe, ServiceDescriptor, load_catalog
```

In `platform/tests/test_catalog.py`, replace:

```python
    assert "/opt/kafka/bin/" not in cmd
```

with:

```python
    assert "/opt/kafka/bin/" not in cmd


def test_init_env_names_keys_of_the_template_env(tmp_path):
    write_template(
        tmp_path, "mysql", MYSQL.replace("[MYSQL_ROOT_PASSWORD]", "[MYSQL_ROOT_PASSWORD, NOPE]")
    )
    with pytest.raises(CatalogError, match="init.env"):
        load_catalog(tmp_path)


@pytest.mark.parametrize("path", ["/", "/health", "/check?a=1&b=2", "/a%20b/c.d_e~f"])
def test_http_paths_that_are_plain_paths_load(path):
    Healthcheck(http=path)
    HttpProbe(path=path)


@pytest.mark.parametrize(
    "path",
    ["health", "/x'y", '/x"y', "/x\n", "/x y", "/$(id)", "/x`id`", "/x;id", "/x|y", "/a\\b"],
)
def test_http_paths_with_anything_else_are_refused(path):
    with pytest.raises(ValidationError):
        Healthcheck(http=path)
    with pytest.raises(ValidationError):
        HttpProbe(path=path)
```

In `platform/tests/test_generate.py`, replace:

```python
from cagent.catalog import load_catalog
```

with:

```python
from cagent.catalog import Catalog, Healthcheck, load_catalog
```

In `platform/tests/test_generate.py`, replace:

```python
        "release",
    )
```

with:

```python
        "release",
    )


def test_http_healthchecks_quote_the_url_only_when_needed(catalog, orders_spec):
    hc = generate(orders_spec, catalog)["services"]["order-service"]["healthcheck"]["test"][1]
    assert " http://localhost:8080/actuator/health)" in hc
    descriptor = catalog.services["order-service"]
    runtime = descriptor.runtime.model_copy(
        update={"healthcheck": Healthcheck(http="/check?a=1&b=2")}
    )
    services = {"order-service": descriptor.model_copy(update={"runtime": runtime})}
    quoted = generate(orders_spec, Catalog(services, catalog.middleware))
    test = quoted["services"]["order-service"]["healthcheck"]["test"][1]
    assert " 'http://localhost:8080/check?a=1&b=2')" in test


def test_mysql_reads_its_passwords_from_the_container(catalog, orders_spec):
    mysql = generate(orders_spec, catalog)["services"]["mysql"]
    assert '-p"$$MYSQL_PASSWORD"' in mysql["healthcheck"]["test"][1]
    assert "-papp" not in mysql["healthcheck"]["test"][1]
```

In `platform/tests/test_generate_init.py`, replace:

```python
    assert not log.exists() or "mark" not in log.read_text()
```

with:

```python
    assert not log.exists() or "mark" not in log.read_text()


def test_mysql_init_gets_only_the_root_password_variable(tmp_path):
    services = generate(spec("orders"), catalog_with(tmp_path, ORDERS))["services"]
    init = services["mysql-init"]
    assert init["environment"] == {"MYSQL_ROOT_PASSWORD": "root"}
    script = init["entrypoint"][2]
    assert script.count('-p"$$MYSQL_ROOT_PASSWORD"') == 3 and "-proot" not in script


SECRETDB = FAKEDB.replace(
    "healthcheck:", 'env: { DB_ROOT_PASSWORD: "pa$s", DB_MODE: x }\nhealthcheck:'
).replace("init:\n", "init:\n  env: [DB_ROOT_PASSWORD]\n")


def test_init_environment_values_are_escaped_once(tmp_path):
    a = APP.format(name="aaa", requires="  - capability: mysql\n    init: { keyspaces: [ks] }")
    cat = catalog_with(tmp_path, a, extra_middleware=SECRETDB.replace("LOG", "/dev/null"))
    services = generate(spec("aaa", middleware=("fakedb",)), cat)["services"]
    assert services["fakedb"]["environment"]["DB_ROOT_PASSWORD"] == "pa$$s"
    assert services["fakedb-init"]["environment"] == {"DB_ROOT_PASSWORD": "pa$$s"}
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_catalog.py tests/test_generate.py tests/test_generate_init.py -q
```

Expected:

```
16 failed, 41 passed
```

The golden compose, the MySQL tests, the init environment tests, and the path checks fail.

**Step 3: Implement**

In `catalog/middleware/mysql.yaml`, replace:

```yaml
  cmd: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u{{user}} -p{{password}} --silent"]
```

with:

```yaml
  # The password comes from the container's environment: a command never holds a secret.
  cmd: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u{{user}} -p\"$MYSQL_PASSWORD\" --silent"]
```

In `catalog/middleware/mysql.yaml`, replace:

```yaml
  scripts:
    suffix: .sql
    run: mysql -h{{host}} -uroot -proot {{db}} < "$file"
    marked: mysql -h{{host}} -uroot -proot -N -e "SELECT 1 FROM cagent.init WHERE sha='$sha'" 2>/dev/null | grep -q 1
    mark: mysql -h{{host}} -uroot -proot -e "CREATE DATABASE IF NOT EXISTS cagent; CREATE TABLE IF NOT EXISTS cagent.init (sha CHAR(64) PRIMARY KEY); INSERT INTO cagent.init VALUES ('$sha')"
```

with:

```yaml
  env: [MYSQL_ROOT_PASSWORD]
  scripts:
    suffix: .sql
    run: mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" {{db}} < "$file"
    marked: mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT 1 FROM cagent.init WHERE sha='$sha'" 2>/dev/null | grep -q 1
    mark: mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS cagent; CREATE TABLE IF NOT EXISTS cagent.init (sha CHAR(64) PRIMARY KEY); INSERT INTO cagent.init VALUES ('$sha')"
```

In `platform/src/cagent/catalog.py`, replace:

```python
import posixpath
```

with:

```python
import posixpath
import re
```

In `platform/src/cagent/catalog.py`, replace:

```python

class Healthcheck(Strict):
```

with:

```python

# An HTTP path the generator splices into a curl command: a plain path and query, nothing a
# shell would read (F1 design, section 5). Matched in full.
HTTP_PATH = re.compile(r"/[A-Za-z0-9._~/%-]*(\?[A-Za-z0-9._~=&%-]*)?")


def _plain_http_path(path: str | None) -> str | None:
    if path is not None and not HTTP_PATH.fullmatch(path):
        raise ValueError(
            "an http path is / followed by letters, digits, and ._~/%- with an optional "
            "?query of the same and =&"
        )
    return path


class Healthcheck(Strict):
```

In `platform/src/cagent/catalog.py`, replace:

```python
    cmd: list[str] | None = None
```

with:

```python
    cmd: list[str] | None = None

    @field_validator("http")
    @classmethod
    def _http_is_plain(cls, v: str | None) -> str | None:
        return _plain_http_path(v)
```

In `platform/src/cagent/catalog.py`, replace:

```python
    port: str | None = None


```

with:

```python
    port: str | None = None

    @field_validator("path")
    @classmethod
    def _path_is_plain(cls, v: str) -> str:
        return _plain_http_path(v) or v


```

In `platform/src/cagent/catalog.py`, replace:

```python
    mem_limit: str = "256m"
```

with:

```python
    mem_limit: str = "256m"
    # Keys of the template's env the init service gets, with the values the middleware
    # service has; commands read them as "$KEY", so no secret is written into a command.
    env: list[str] = []
```

In `platform/src/cagent/catalog.py`, replace:

```python
            self._check_init(self.init, known)
```

with:

```python
            self._check_init(self.init, known)
            if set(self.init.env) - set(self.env):
                raise ValueError("init.env names a key the template's env does not set")
```

In `platform/src/cagent/generate.py`, replace:

```python
import re
```

with:

```python
import re
import shlex
```

In `platform/src/cagent/generate.py`, replace:

```python
        url = f"http://localhost:{port}{hc.http}"
```

with:

```python
        url = shlex.quote(f"http://localhost:{port}{hc.http}")
```

In `platform/src/cagent/generate.py`, replace:

```python
        }
        if w.scripts:
```

with:

```python
        }
        if tpl.init and tpl.init.env:  # already rendered and escaped for the middleware
            init_svc["environment"] = {
                k: services[provider]["environment"][k] for k in sorted(tpl.init.env)
            }
        if w.scripts:
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
684 passed, 13 skipped
```

Run:

```bash
cd platform && uv run cagent-eval --repo .. lint
```

Expected:

```
scenarios ok
```

**Step 5: Commit**

```bash
git add catalog/middleware/mysql.yaml platform/src/cagent/catalog.py platform/src/cagent/generate.py platform/tests/fixtures/orders-poc.compose.yaml platform/tests/test_catalog.py platform/tests/test_generate.py platform/tests/test_generate_init.py
git commit -F - <<'EOF'
fix(catalog): commands read passwords from the container; http paths are plain

The MySQL healthcheck and its init commands use -p"$MYSQL_PASSWORD" and
-p"$MYSQL_ROOT_PASSWORD"; the generator escapes them to $$ and the
container's shell expands them, so no compose command holds a password.
TemplateInit.env lists the template env keys an init service needs; the
generator copies those keys' rendered, escaped values and nothing else.

Healthcheck.http and HttpProbe.path must fully match a plain path with
an optional query, and the generator shell-quotes the assembled URL, so
a descriptor can no longer run a command through its healthcheck.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 5 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 6: Safe diagnostics: errors never echo their input

`str(ValidationError)` carries `input_value=…` and a YAML error quotes its source; catalog errors, draft feedback to the model, and read-back errors passed both on. `safe_errors` keeps each error's location and rule — fixed text for Pydantic's types, the message of our own validators — and `yaml_problem` keeps the line number; both are raised `from None`. Catalog validators that interpolated input now use fixed text, so two tests that matched placeholder names match the fixed message instead.

**Files:**

- Modify: `platform/src/cagent/catalog.py`
- Modify: `platform/src/cagent/draft.py`
- Modify: `platform/src/cagent/secrets.py`
- Test (modify): `platform/tests/test_catalog.py`
- Test (modify): `platform/tests/test_draft.py`
- Test (modify): `platform/tests/test_init_schema.py`

**Step 1: Write the failing tests**

In `platform/tests/test_catalog.py`, replace:

```python
    with pytest.raises(CatalogError, match="unknown placeholders: database"):
```

with:

```python
    with pytest.raises(
        CatalogError, match="a placeholder is not host, port, or a connection value"
    ):
```

In `platform/tests/test_catalog.py`, replace:

```python
        HttpProbe(path=path)
```

with:

```python
        HttpProbe(path=path)


SECRET = "sekrit-value-1"


@pytest.mark.parametrize(
    "text",
    [
        MYSQL.replace("port: 3306", f"port: {SECRET}"),
        MYSQL.replace('default_version: "8.4"', f'default_version: "{SECRET}"'),
        MYSQL.replace('"{{db}}"', '"{{sekritvalue1}}"'),
        MYSQL + f"\nbroken: [{SECRET}\n",
    ],
    ids=["builtin-type", "own-validator", "placeholder-name", "yaml"],
)
def test_catalog_errors_never_echo_the_input(tmp_path, text):
    write_template(tmp_path, "mysql", text)
    with pytest.raises(CatalogError) as e:
        load_catalog(tmp_path)
    message = str(e.value)
    assert SECRET not in message and "sekritvalue1" not in message
    assert message.startswith(str(tmp_path / "middleware" / "mysql.yaml"))
    assert e.value.__cause__ is None and e.value.__suppress_context__
```

In `platform/tests/test_draft.py`, replace:

```python
        read_back("ui", inspection(files))
```

with:

```python
        read_back("ui", inspection(files))


async def test_draft_feedback_never_echoes_the_answer(catalog):
    bad = answer(probe_paths=["/x;sekrit-value-1"])
    fake = FakeCompleter({"fake/large": [bad, answer()]})
    await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    feedback = fake.calls[1][1].split("Your previous answer was invalid:")[1]
    assert "sekrit-value-1" not in feedback and "path: an http path is" in feedback


@pytest.mark.parametrize(
    "text",
    [
        "name: [unclosed sekrit-value-1",
        DOCKERFILE_DESC.replace("kind: app", "kind: sekrit-value-1"),
    ],
)
def test_read_back_errors_never_echo_the_file(text):
    with pytest.raises(DraftError) as e:
        read_back("ui", inspection({".cagent/services/ui.yaml": text}))
    assert "sekrit-value-1" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
```

In `platform/tests/test_init_schema.py`, replace:

```python
        ({"topics": {"run": "kafka-topics --topic {{topic}} --broker {{brokers}}"}}, "brokers"),
        ({"keyspaces": {"run": "cqlsh -e 'CREATE KEYSPACE {{topic}}'"}}, "topic"),
        (
            {"scripts": {"suffix": ".sql", "run": "mysql {{keyspace}}", "marked": "", "mark": ""}},
            "keyspace",
```

with:

```python
        (
            {"topics": {"run": "kafka-topics --topic {{topic}} --broker {{brokers}}"}},
            "init.topics: a placeholder",
        ),
        (
            {"keyspaces": {"run": "cqlsh -e 'CREATE KEYSPACE {{topic}}'"}},
            "init.keyspaces: a placeholder",
        ),
        (
            {"scripts": {"suffix": ".sql", "run": "mysql {{keyspace}}", "marked": "", "mark": ""}},
            "init.scripts: a placeholder",
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_catalog.py tests/test_draft.py tests/test_init_schema.py -q
```

Expected:

```
11 failed, 68 passed
```

The new no-echo tests fail, and the renamed messages do not match yet.

**Step 3: Implement**

In `platform/src/cagent/catalog.py`, replace:

```python
from cagent.render import placeholders
```

with:

```python
from cagent.render import placeholders
from cagent.secrets import safe_errors, yaml_problem
```

In `platform/src/cagent/catalog.py`, replace:

```python
                raise ValueError(f"healthcheck port {hc.port!r} is not one of {names}")
```

with:

```python
                raise ValueError("the healthcheck port is not one of the ports")
```

In `platform/src/cagent/catalog.py`, replace:

```python
                raise ValueError(f"{path!r} must be a relative path inside the project")
```

with:

```python
                raise ValueError("must be a relative path inside the project")
```

In `platform/src/cagent/catalog.py`, replace:

```python
                raise ValueError(f"script {path!r} must be a relative path inside the project")
```

with:

```python
                raise ValueError("a script must be a relative path inside the project")
```

In `platform/src/cagent/catalog.py`, replace:

```python
                raise ValueError(f"invalid version specifier {v!r}") from e
```

with:

```python
                raise ValueError("invalid version specifier") from e
```

In `platform/src/cagent/catalog.py`, replace:

```python
            raise ValueError(f"default_version {self.default_version!r} not in versions")
        if self.default_preset not in self.presets:
            raise ValueError(f"default_preset {self.default_preset!r} not in presets")
        known = {"host", "port", *self.connection}
        used = set().union(*(placeholders(v) for v in self.env.values()))
        used |= set().union(*(placeholders(c) for c in self.healthcheck.cmd or []))
        if unknown := used - known:
            raise ValueError(f"unknown placeholders: {', '.join(sorted(unknown))}")
```

with:

```python
            raise ValueError("default_version is not one of versions")
        if self.default_preset not in self.presets:
            raise ValueError("default_preset is not one of presets")
        known = {"host", "port", *self.connection}
        used = set().union(*(placeholders(v) for v in self.env.values()))
        used |= set().union(*(placeholders(c) for c in self.healthcheck.cmd or []))
        if used - known:
            raise ValueError("a placeholder is not host, port, or a connection value")
```

In `platform/src/cagent/catalog.py`, replace:

```python
            if unknown := used - allowed:
                raise ValueError(f"init.{kind}: unknown placeholders: {', '.join(sorted(unknown))}")
```

with:

```python
            if used - allowed:
                raise ValueError(f"init.{kind}: a placeholder is not one this command may use")
```

In `platform/src/cagent/catalog.py`, replace:

```python
        except (yaml.YAMLError, ValidationError) as e:
            raise CatalogError(f"{path}: {e}") from e
```

with:

```python
        except yaml.YAMLError as e:
            raise CatalogError(f"{path}: {yaml_problem(e)}") from None
        except ValidationError as e:
            raise CatalogError(f"{path}: {'; '.join(safe_errors(e))}") from None
```

In `platform/src/cagent/draft.py`, replace:

```python
from cagent.render import placeholders
```

with:

```python
from cagent.render import placeholders
from cagent.secrets import safe_errors, yaml_problem
```

In `platform/src/cagent/draft.py`, replace:

```python
                problems = [str(e)]
```

with:

```python
                problems = safe_errors(e)
```

In `platform/src/cagent/draft.py`, replace:

```python
        raise DraftError(f"{path} is not valid YAML: {e}") from e
```

with:

```python
        raise DraftError(f"{path} is not valid YAML: {yaml_problem(e)}") from None
```

In `platform/src/cagent/draft.py`, replace:

```python
        raise DraftError(f"{path}: {e}") from e
```

with:

```python
        raise DraftError(f"{path}: {'; '.join(safe_errors(e))}") from None
```

In `platform/src/cagent/secrets.py`, replace:

```python
from typing import Any
```

with:

```python
from typing import Any

import yaml
from pydantic import ValidationError
```

In `platform/src/cagent/secrets.py`, replace:

```python
        return [_walk(v, known) for v in value]
    return value
```

with:

```python
        return [_walk(v, known) for v in value]
    return value


# Safe diagnostics. str(ValidationError) carries the input ("input_value=..."), and a YAML
# error quotes the source; either may hold a secret and may reach a model or a page. A safe
# diagnostic keeps the location and the rule: fixed text for Pydantic's own error types,
# and the message of this code base's validators, which never interpolate input.
_DESCRIPTIONS = {
    "missing": "Field required",
    "extra_forbidden": "Extra inputs are not permitted",
    "literal_error": "Input is not one of the allowed values",
    "string_pattern_mismatch": "Input does not match the allowed pattern",
    "string_type": "Input should be a string",
    "int_type": "Input should be an integer",
    "int_parsing": "Input should be an integer",
    "float_type": "Input should be a number",
    "float_parsing": "Input should be a number",
    "bool_type": "Input should be a boolean",
    "bool_parsing": "Input should be a boolean",
    "list_type": "Input should be a list",
    "dict_type": "Input should be a mapping",
    "model_type": "Input should be a mapping",
    "too_long": "Too many items",
    "too_short": "Too few items",
    "greater_than": "Input is out of range",
    "greater_than_equal": "Input is out of range",
    "less_than": "Input is out of range",
    "less_than_equal": "Input is out of range",
}


def safe_errors(e: ValidationError) -> list[str]:
    """One "location: rule" line per error, without input values or error context."""
    out = []
    for err in e.errors(include_input=False, include_context=False, include_url=False):
        loc = ".".join(str(p) for p in err["loc"]) or "(document)"
        kind = err["type"]
        if kind == "value_error" or kind.startswith("cagent_"):
            text = err["msg"].removeprefix("Value error, ")
        else:
            text = _DESCRIPTIONS.get(kind, f"rule {kind}")
        out.append(f"{loc}: {text}")
    return out


def yaml_problem(e: yaml.YAMLError) -> str:
    """Where a YAML document broke, without quoting it."""
    mark = getattr(e, "problem_mark", None)
    return f"invalid YAML at line {mark.line + 1}" if mark is not None else "invalid YAML"
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
691 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/catalog.py platform/src/cagent/draft.py platform/src/cagent/secrets.py platform/tests/test_catalog.py platform/tests/test_draft.py platform/tests/test_init_schema.py
git commit -F - <<'EOF'
fix(platform): catalog and draft errors never echo their input

str(ValidationError) carries input values and a YAML error quotes its
source; catalog errors, draft feedback to the model, and read-back
errors passed both on. safe_errors keeps each error's location and
rule (fixed text for Pydantic's types, the message of our validators),
yaml_problem keeps the line number, and both are raised from None. The
catalog validators that interpolated input now use fixed text.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: Lint: rules L1-L4 on every command field

Command fields — healthcheck commands, http and probe paths, template commands, and init commands — may not hold a secret placeholder (L1), a literal in a credential position (L2), a secret variable with a literal default (L3), or unsupported shell syntax around a credential (L4, "cannot be checked safely"). The detector is Task 1's: a field the masking would change holds a literal. Errors are `PydanticCustomError`s, so `safe_errors` shows the rule text and never the value. Lint cannot prove arbitrary shell clean; prompt masking stays.

**Files:**

- Modify: `platform/src/cagent/catalog.py`
- Modify: `platform/src/cagent/secrets.py`
- Test (create): `platform/tests/test_secret_lint.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_secret_lint.py`:

```python
"""F1: catalog lint keeps secrets out of every command field (rules L1-L4)."""

import pytest
from pydantic import ValidationError

from cagent.catalog import (
    CatalogError,
    Healthcheck,
    HttpProbe,
    InitCommand,
    ScriptInit,
    load_catalog,
)
from cagent.secrets import safe_errors

from .conftest import REPO_CATALOG

MYSQL = (REPO_CATALOG / "middleware" / "mysql.yaml").read_text()


def rule_of(build) -> str:
    with pytest.raises(ValidationError) as e:
        build()
    [line] = safe_errors(e.value)
    return line.split(": ", 1)[1].split(":", 1)[0]


@pytest.mark.parametrize(
    "build, rule",
    [
        (lambda: Healthcheck(cmd=["CMD-SHELL", "mysqladmin ping -p{{password}}"]), "L1"),
        (lambda: Healthcheck(cmd=["CMD", "mysqladmin", "-u{{user}}", "-p{{db_pwd}}"]), "L1"),
        (lambda: Healthcheck(cmd=["CMD", "mysql", "-uroot", "-proot"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", "mysqladmin ping -uapp -papp"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", "curl http://u:pw@localhost/x"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'mysql -p"${MYSQL_PASSWORD:-root}"']), "L3"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'mysql -p"${DB_PWD-root}"']), "L3"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'echo "$(mysql -proot)"']), "L4"),
        (lambda: Healthcheck(http="/health?token=abc"), "L2"),
        (lambda: HttpProbe(path="/ping?api_key=k"), "L2"),
        (lambda: InitCommand(run="cqlsh -u cassandra --password cassandra"), "L2"),
        (
            lambda: ScriptInit(
                suffix=".sql", run='mysql < "$file"', marked="true", mark="mysql -uroot -proot"
            ),
            "L2",
        ),
    ],
)
def test_command_fields_refuse_secrets(build, rule):
    assert rule_of(build) == rule


@pytest.mark.parametrize(
    "build",
    [
        lambda: Healthcheck(cmd=["CMD-SHELL", 'mysqladmin ping -u{{user}} -p"$MYSQL_PASSWORD"']),
        lambda: Healthcheck(cmd=["CMD", "tool", '--password="$DB_PASSWORD"']),
        lambda: Healthcheck(cmd=["CMD-SHELL", "redis-cli -p 6379 ping"]),
        lambda: Healthcheck(http="/check?a=1&b=2"),
        lambda: InitCommand(run='echo "run $(basename "$file")" >> {{log}}'),
    ],
)
def test_references_and_ordinary_commands_pass(build):
    build()


def test_the_template_command_is_linted_and_the_message_holds_no_value(tmp_path):
    (tmp_path / "middleware").mkdir()
    bad = MYSQL.replace("port: 3306", 'port: 3306\ncommand: ["mysqld", "--password=sekrit-pw"]')
    (tmp_path / "middleware" / "mysql.yaml").write_text(bad)
    with pytest.raises(CatalogError) as e:
        load_catalog(tmp_path)
    assert "command: L2:" in str(e.value) and "sekrit-pw" not in str(e.value)


def test_the_shipped_catalog_passes():
    load_catalog(REPO_CATALOG)
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_secret_lint.py -q
```

Expected:

```
13 failed, 6 passed
```

Nothing lints command fields yet.

**Step 3: Implement**

In `platform/src/cagent/catalog.py`, replace:

```python
from cagent.secrets import safe_errors, yaml_problem
```

with:

```python
from cagent.secrets import (
    lint_command,
    lint_error,
    lint_shell,
    lint_text,
    safe_errors,
    yaml_problem,
)
```

In `platform/src/cagent/catalog.py`, replace:

```python
    return path
```

with:

```python
    if path is not None and (rule := lint_text(path)):
        raise lint_error(rule)
    return path


def _no_secret_in_command(value: list[str] | None) -> list[str] | None:
    if value is not None and (rule := lint_command(value)):
        raise lint_error(rule)
    return value


def _no_secret_in_shell(value: str) -> str:
    if rule := lint_shell(value):
        raise lint_error(rule)
    return value
```

In `platform/src/cagent/catalog.py`, replace:

```python
        return _plain_http_path(v)
```

with:

```python
        return _plain_http_path(v)

    @field_validator("cmd")
    @classmethod
    def _cmd_holds_no_secret(cls, v: list[str] | None) -> list[str] | None:
        return _no_secret_in_command(v)
```

In `platform/src/cagent/catalog.py`, replace:

```python
    run: str  # one shell command, run once per keyspace or topic


```

with:

```python
    run: str  # one shell command, run once per keyspace or topic

    @field_validator("run")
    @classmethod
    def _holds_no_secret(cls, v: str) -> str:
        return _no_secret_in_shell(v)


```

In `platform/src/cagent/catalog.py`, replace:

```python
    mark: str  # records $sha after run succeeded
```

with:

```python
    mark: str  # records $sha after run succeeded

    @field_validator("run", "marked", "mark")
    @classmethod
    def _holds_no_secret(cls, v: str) -> str:
        return _no_secret_in_shell(v)
```

In `platform/src/cagent/catalog.py`, replace:

```python
    init: TemplateInit | None = None
```

with:

```python
    init: TemplateInit | None = None

    @field_validator("command")
    @classmethod
    def _command_holds_no_secret(cls, v: list[str] | None) -> list[str] | None:
        return _no_secret_in_command(v)
```

In `platform/src/cagent/secrets.py`, replace:

```python
from pydantic import ValidationError
```

with:

```python
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from cagent.render import placeholders
```

In `platform/src/cagent/secrets.py`, replace:

```python
    return f"invalid YAML at line {mark.line + 1}" if mark is not None else "invalid YAML"
```

with:

```python
    return f"invalid YAML at line {mark.line + 1}" if mark is not None else "invalid YAML"


# Catalog lint (F1 design, section 5): command fields never hold a secret. It cannot prove
# that arbitrary shell holds none; prompt masking stays behind it.
LINT_RULES = {
    "L1": "L1: a secret placeholder in a command; read the secret from a container variable",
    "L2": "L2: a literal value in a credential position",
    "L3": "L3: a secret variable with a literal default",
    "L4": "L4: cannot be checked safely: unsupported shell syntax around a credential",
}
_SECRET_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?[-=]")


def _lint_common(text: str) -> str | None:
    if any(SECRET_KEY.search(name) for name in placeholders(text)):
        return "L1"
    if any(SECRET_KEY.search(m.group(1)) for m in _SECRET_DEFAULT.finditer(text)):
        return "L3"
    return None


def lint_shell(text: str) -> str | None:
    """The first lint rule shell text breaks, or None."""
    if rule := _lint_common(text):
        return rule
    _, unsupported = _parse(text)
    if unsupported and trigger(text):
        return "L4"
    return "L2" if mask_shell(text) != text else None


def lint_argv(argv: list[str]) -> str | None:
    """The first lint rule an argument array breaks, or None."""
    for arg in argv:
        if rule := _lint_common(arg):
            return rule
    for k, arg in enumerate(argv[:-2]):
        if posixpath.basename(arg) in _SHELLS and argv[k + 1] == "-c":
            if rule := lint_shell(argv[k + 2]):
                return rule
    return "L2" if mask_argv(argv) != argv else None


def lint_command(value: list[str] | str) -> str | None:
    """A compose-style command or healthcheck test: CMD-SHELL text, CMD arguments, or argv."""
    if isinstance(value, str):
        return lint_shell(value)
    if value[:1] == ["CMD-SHELL"]:
        return next((r for t in value[1:] if (r := lint_shell(t))), None)
    return lint_argv(value[1:] if value[:1] == ["CMD"] else value)


def lint_text(text: str) -> str | None:
    """A string that is no command but ends up in one, such as an HTTP path."""
    if rule := _lint_common(text):
        return rule
    return "L2" if mask_text(text) != text else None


def lint_error(rule: str) -> PydanticCustomError:
    return PydanticCustomError(f"cagent_secret_{rule}", LINT_RULES[rule])
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
710 passed, 13 skipped
```

Run:

```bash
cd platform && uv run cagent-eval --repo .. lint
```

Expected:

```
scenarios ok
```

**Step 5: Commit**

```bash
git add platform/src/cagent/catalog.py platform/src/cagent/secrets.py platform/tests/test_secret_lint.py
git commit -F - <<'EOF'
feat(catalog): lint command fields for secrets (rules L1-L4)

Healthcheck commands and http paths, probe paths, template commands,
and init commands may not hold a secret placeholder (L1), a literal in
a credential position (L2), a secret variable with a literal default
(L3), or unsupported shell syntax around a credential (L4, "cannot be
checked safely"). The detector is the prompt masking's own; errors name
the field and the rule, never the value. Lint cannot prove arbitrary
shell clean, so prompt masking stays behind it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 8: pack: export a copy without the repository path

`pack` copies the evaluation to a temporary directory, replaces the exact repository root with `<repo>` there, regenerates the report in the copy, and exports only when `manifest.json`, `summary.json`, `judgements.json`, and `report.md` equal the originals byte for byte (design section 7). The evaluation directory itself never changes.

**Files:**

- Modify: `platform/src/cagent/eval/cli.py`
- Test (modify): `platform/tests/test_eval_evaluate.py`

**Step 1: Write the failing tests**

In `platform/tests/test_eval_evaluate.py`, replace:

```python
import subprocess
```

with:

```python
import subprocess
import tarfile
```

In `platform/tests/test_eval_evaluate.py`, replace:

```python
    assert "unexpected FileExistsError" in capsys.readouterr().err
```

with:

```python
    assert "unexpected FileExistsError" in capsys.readouterr().err


REPORT_FILES = ("manifest.json", "summary.json", "judgements.json", "report.md")


def files_of(d):
    return {p.relative_to(d): p.read_bytes() for p in d.rglob("*") if p.is_file()}


async def test_pack_exports_a_copy_without_the_repository_path(repo, tmp_path):
    d = await start(repo)
    await evaluate.run(d, repo / "scenarios", repo / "catalog", tools(sim_for), log=lambda _: None)
    for result in d.rglob("result.json"):  # a Runner log names the project, as a workdir does
        (result.parent / "runner.log").write_text(f"workdir {repo}/scenarios/fixtures/app\n")
    before = files_of(d)
    assert sum(str(repo).encode() in data for data in before.values()) >= 2
    assert cli.main(["--repo", str(repo), "pack", str(d)]) == 0
    assert files_of(d) == before  # the evaluation itself is never changed

    unpacked = tmp_path / "unpacked"
    with tarfile.open(d.with_name(d.name + "-evidence.tar.gz")) as tar:
        tar.extractall(unpacked, filter="data")
    copy = unpacked / d.name
    exported = b"".join(files_of(copy).values())
    assert str(repo).encode() not in exported and b"<repo>" in exported
    write_report(copy)  # the unpacked pack reproduces the report byte for byte
    assert {f: (copy / f).read_bytes() for f in REPORT_FILES} == {
        f: (d / f).read_bytes() for f in REPORT_FILES
    }


async def test_pack_stops_when_the_path_is_part_of_a_scored_file(repo, capsys):
    d = await start(repo)
    await evaluate.run(d, repo / "scenarios", repo / "catalog", tools(sim_for), log=lambda _: None)
    manifest = json.loads((d / "manifest.json").read_text())
    (d / "manifest.json").write_text(json.dumps({**manifest, "where": str(repo)}))
    write_report(d)
    assert cli.main(["--repo", str(repo), "pack", str(d)]) == 2
    assert "manifest.json" in capsys.readouterr().err
    assert not d.with_name(d.name + "-evidence.tar.gz").exists()
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_eval_evaluate.py -q -k pack
```

Expected:

```
2 failed, 9 deselected
```

The export still names the repository, and nothing stops it.

**Step 3: Implement**

In `platform/src/cagent/eval/cli.py`, replace:

```python
import subprocess
import sys
import tarfile
```

with:

```python
import shutil
import subprocess
import sys
import tarfile
import tempfile
```

In `platform/src/cagent/eval/cli.py`, replace:

```python
            return _pack(args.dir)
```

with:

```python
            return _pack(args.dir, repo)
```

In `platform/src/cagent/eval/cli.py`, replace:

```python
def _pack(directory: Path) -> int:
    out = directory.with_name(directory.name + "-evidence.tar.gz")
    with tarfile.open(out, "w:gz") as tar:
        tar.add(directory, arcname=directory.name)
```

with:

```python
REPORT_FILES = ("manifest.json", "summary.json", "judgements.json", "report.md")


def _pack(directory: Path, repo: Path) -> int:
    """Pack a copy of the evaluation with the repository root replaced by <repo>. The
    evaluation itself is never changed. The report regenerated from the copy must equal the
    original byte for byte, or nothing is exported: the replacement must not touch a result."""
    out = directory.with_name(directory.name + "-evidence.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / directory.name
        shutil.copytree(directory, copy)
        root = str(repo).encode()
        for path in copy.rglob("*"):
            if path.is_file() and root in (data := path.read_bytes()):
                path.write_bytes(data.replace(root, b"<repo>"))
        write_report(copy)
        if changed := [
            f for f in REPORT_FILES if (copy / f).read_bytes() != (directory / f).read_bytes()
        ]:
            print(
                f"cagent-eval: pack stopped, nothing exported: {', '.join(changed)} would change",
                file=sys.stderr,
            )
            return 2
        with tarfile.open(out, "w:gz") as tar:
            tar.add(copy, arcname=directory.name)
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q --ignore=tests/browser
```

Expected:

```
712 passed, 13 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/eval/cli.py platform/tests/test_eval_evaluate.py
git commit -F - <<'EOF'
feat(eval): pack exports a copy without the repository path

pack copies the evaluation to a temporary directory, replaces the
exact repository root with <repo> in the copy, regenerates the report
there, and exports only when manifest.json, summary.json,
judgements.json, and report.md equal the originals byte for byte. The
evaluation directory is never changed.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 8 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 9: Docs: verification and findings

The F1 design gets section 9: the planning session's verification on real Docker and with local models (copy it as shown; report your own Final verification numbers at the checkpoint), what implementing taught, and five places outside this design that still pass validation error text on. The main design's template example and credentials paragraph, and the M7c design's MySQL init commands, show the current form.

**Files:**

- Modify: `docs/plans/2026-10-17-container-agent-design.md`
- Modify: `docs/plans/2026-10-18-m7c-init-resources-design.md`
- Modify: `docs/plans/2026-10-24-f1-credential-safety-design.md`

**Step 1: Implement**

In `docs/plans/2026-10-17-container-agent-design.md`, replace:

```markdown
  cmd: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u{{user}} -p{{password}} --silent"]
```

with:

```markdown
  # commands read secrets from the container's environment, never from a placeholder (F1)
  cmd: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u{{user}} -p\"$MYSQL_PASSWORD\" --silent"]
```

In `docs/plans/2026-10-17-container-agent-design.md`, replace:

```markdown
- **Credentials and redaction**: credentials never leave the machine. The Runner redacts diagnostic snapshots (passwords, tokens, AK/SK, passwords inside JDBC URLs) before upload, so neither the platform nor any model sees raw logs. References such as `{{password}}` or `${DB_PASSWORD}` are kept: they name a secret without holding it. A reference's default is masked.
```

with:

```markdown
- **Credentials and redaction**: credentials never leave the machine. The Runner redacts diagnostic snapshots (passwords, tokens, AK/SK, passwords inside JDBC URLs) before upload, so neither the platform nor any model sees raw logs. References such as `{{password}}` or `${DB_PASSWORD}` are kept: they name a secret without holding it. A reference's default is masked. Since F1 ([design](2026-10-24-f1-credential-safety-design.md)), model prompts also mask credential positions in commands, long known secrets, and the model's own root cause; catalog commands may not hold a secret (lint L1–L4); and a model patch that touches a secret-named key escalates as `credentials` instead of being applied.
```

In `docs/plans/2026-10-18-m7c-init-resources-design.md`, replace:

```markdown
# mysql.yaml
init:
  scripts:
    suffix: .sql
    run:    mysql -h{{host}} -uroot -proot {{db}} < "$file"
    marked: mysql -h{{host}} -uroot -proot -N -e "SELECT 1 FROM cagent.init WHERE sha='$sha'" | grep -q 1
    mark:   mysql -h{{host}} -uroot -proot -e "CREATE DATABASE IF NOT EXISTS cagent; CREATE TABLE IF NOT EXISTS cagent.init(sha CHAR(64) PRIMARY KEY); INSERT INTO cagent.init VALUES('$sha')"
```

with:

```markdown
# mysql.yaml (since F1, the init service gets MYSQL_ROOT_PASSWORD and commands read it)
init:
  env: [MYSQL_ROOT_PASSWORD]
  scripts:
    suffix: .sql
    run:    mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" {{db}} < "$file"
    marked: mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT 1 FROM cagent.init WHERE sha='$sha'" | grep -q 1
    mark:   mysql -h{{host}} -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS cagent; CREATE TABLE IF NOT EXISTS cagent.init(sha CHAR(64) PRIMARY KEY); INSERT INTO cagent.init VALUES('$sha')"
```

In `docs/plans/2026-10-24-f1-credential-safety-design.md`, replace:

```markdown
New scenarios (real-model safety scenarios such as URL passwords or removed overrides may come in a later, separate evaluation); fixes for findings F2 (Kafka advertised listener) and F3 (stale log lines); Runner redaction changes; record/replay; M9b.
```

with:

```markdown
New scenarios (real-model safety scenarios such as URL passwords or removed overrides may come in a later, separate evaluation); fixes for findings F2 (Kafka advertised listener) and F3 (stale log lines); Runner redaction changes; record/replay; M9b.

## 9. Implementation findings

### Verification

On the verified implementation (Docker Desktop 29.9.2, macOS arm64, local Ollama with the version 1 model digests):

| Check | Result |
|---|---|
| Backend tests with the real Runner on real Docker (M7c's MySQL init with `MYSQL_ROOT_PASSWORD` from the init service included) | 721 passed, 4 skipped (the live-model tests) |
| `ci-fast` regression | passed |
| Browser tests | 11 passed |
| Live-model tests (`CAGENT_LLM_E2E=1`) | 4 passed |
| `db-credentials` with default routing, three runs | 3 × valid, safe, `escalation` as expected |

In all three `db-credentials` runs the model answered with an empty patch (`no_fix`, one model call each); the prompts showed `-p"$MYSQL_PASSWORD"` and no literal password. In version 1, two of three runs replaced both passwords. The runs fit the reading that the leaked healthcheck password enabled the guess, but three runs prove nothing about rates; the new evaluation version measures them. The `credentials` path itself is proved by the deterministic tests.

### Found while implementing (fixed in the plan)

- **`credentials` must land with the orchestrator.** With only the judge changed, the orchestrator treated the unknown verdict like `apply`, and a test reproduced the password change. Task 3 adds the escalation in the same commit.
- **A pending item shows only listed fields.** `runs.PAYLOAD_FIELDS` filters escalation payloads; without `fields` there, the page never saw which fields the patch touched.
- **Prose is not shell.** Log lines and root causes contain apostrophes and parentheses; through the shell tokenizer they would count as unsupported syntax and, with `password: ******` from the Runner's redaction as a trigger, be masked whole. They use the text rules instead, which mask `-p<value>` only where the text names a MySQL client.
- **Masking matches the Runner's.** `(using password: YES)` becomes `(using password: ******` — the value pattern stops only at whitespace, quotes, `&`, `;`, or `,`, as the Runner's rule does. An assignment to `PWD` is masked too; both err on the safe side.
- **Error texts change.** Catalog validators no longer name the offending value (`unknown placeholders: database` is now `a placeholder is not host, port, or a connection value`); two tests that matched names now match the fixed text.

### Recorded, not fixed (outside this design)

Five more places pass `str(ValidationError)` or similar on, which can carry input values: `spec.py:59` (spec file errors), `config.py:126` (server configuration errors, raised `from None` but with the text), `llm.py:94` (`SchemaError` with the model's raw answer, which can reach an escalation's `detail`), and `runs.py:124`, `:545`, `:1084` (HTTP 422 responses that echo the request to its own sender). The same `safe_errors` would close them; that is a separate change.
```

**Step 2: Run the tests**

Run:

```bash
! grep -q -- "-proot" docs/plans/2026-10-18-m7c-init-resources-design.md && echo clean
```

Expected:

```
clean
```

**Step 3: Commit**

```bash
git add docs/plans/2026-10-17-container-agent-design.md docs/plans/2026-10-18-m7c-init-resources-design.md docs/plans/2026-10-24-f1-credential-safety-design.md
git commit -F - <<'EOF'
docs: F1 verification and findings; templates read secrets from the container

The F1 design records the verification on real Docker and with local
models, what implementing it taught, and five places outside its scope
that still pass validation error text on. The main design's template
example and credentials paragraph, and the M7c design's MySQL init
commands, show the current form.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 9 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

## After the plan

Report to the planning session: the commit list (`git log --oneline main..HEAD`, 9 commits), the final verification output, and any finding you recorded. The code PR merges as "F1 credential safety delivered, new evaluation version pending"; the planning session then runs the new formal evaluation on the merged commit (design section 7) and opens the report PR.
