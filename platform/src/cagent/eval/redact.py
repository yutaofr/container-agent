"""Redaction for everything the harness writes to disk.

Text gets the Runner's rules (runner/internal/redact) plus every secret value the harness has
seen. Secret values become tags, `<masked:…>`, made with an HMAC key that lives only in memory
for one evaluation: two redacted values are equal exactly when the originals were, so a judge
reading the files later still sees whether a patch changed a password, without ever seeing it.
"""

import hashlib
import hmac
import re
from typing import Any

from cagent.diagnose import mask_secrets

MASK = "******"
MIN_TEXT_SECRET = 6  # what the Runner leaves in place of a secret it finds in text

_TEMPLATE = re.compile(r"^\{\{\w+\}\}$")
_PLACEHOLDER = re.compile(r"^\$\{([\w.-]+)(:.*)?\}$")
_SECRET_NAME = re.compile(r"(?i)password|passwd|pwd|secret|token|api_?key|access_?key")
_POINTER_KEY = re.compile(r"/environment/([^/]+)$")

_RULES: list[tuple[re.Pattern[str], str | None]] = [
    (
        re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
        f"-----PRIVATE KEY {MASK}-----",
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)"), None),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), rf"\g<1>{MASK}"),
    (
        re.compile(
            r"(?i)(\"?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
            r"[\w.-]*\"?\s*[:=]\s*\"?)([^\"&;,\s]+)"
        ),
        None,
    ),
]


def _mask(value: str) -> str:
    """The Runner's mask: keep a reference's name, mask a value."""
    if _TEMPLATE.match(value):
        return value
    if m := _PLACEHOLDER.match(value):
        return value if m.group(2) is None else "${" + m.group(1) + ":" + MASK + "}"
    return MASK


def _masked_value(m: re.Match[str]) -> str:
    """Keep the groups around the value (group 2) and mask the value."""
    tail = m.group(3) if m.re.groups >= 3 else ""
    return m.group(1) + _mask(m.group(2)) + tail


class Redactor:
    def __init__(self, key: bytes, secrets: list[str] | tuple[str, ...] = ()):
        self._key = key
        self._secrets: set[str] = set()
        for s in secrets:
            self.add(s)

    def add(self, value: str) -> None:
        """Remember a secret for text. Short values (`root`, `app`: throwaway template
        credentials) are masked only where a key names them: replacing them as words would
        rewrite ordinary log and prompt text and break evidence matching."""
        if len(value) >= MIN_TEXT_SECRET and not value.startswith("<masked:"):
            self._secrets.add(value)

    def learn(self, compose: dict[str, Any] | None) -> None:
        """Remember the values a compose document keeps under secret-looking keys."""
        if not isinstance(compose, dict):
            return
        masked = mask_secrets(compose)
        for name, svc in (compose.get("services") or {}).items():
            env = svc.get("environment")
            if not isinstance(env, dict):
                continue
            for k, v in env.items():
                if masked["services"][name]["environment"][k] != v and v is not None:
                    self.add(str(v))

    def tag(self, value: Any) -> str:
        digest = hmac.new(self._key, str(value).encode(), hashlib.sha256).hexdigest()
        return f"<masked:{digest[:12]}>"

    def text(self, s: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in s:  # whole words only, and never inside a tag
                s = re.sub(rf"(?<![\w:]){re.escape(secret)}(?!\w)", self.tag(secret), s)
        for rx, repl in _RULES:
            if repl is not None:
                s = rx.sub(repl, s)
            else:
                s = rx.sub(_masked_value, s)
        return s

    def value(self, obj: Any, key: str | None = None) -> Any:
        """A redacted copy: a scalar under a secret-looking key becomes a tag; a patch
        operation on such a key has its value tagged; every string gets text()."""
        if isinstance(obj, dict):
            out = {}
            path_key = None
            if isinstance(obj.get("path"), str) and (m := _POINTER_KEY.search(obj["path"])):
                path_key = m.group(1)
            for k, v in obj.items():
                if k == "value" and path_key and _SECRET_NAME.search(path_key):
                    out[k] = self._secret(v)
                elif isinstance(k, str) and _SECRET_NAME.search(k):
                    out[k] = self._secret(v)
                else:
                    out[k] = self.value(v)
            return out
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def _secret(self, v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return self.value(v)
        if v is None or (isinstance(v, str) and v.startswith("<masked:")):
            return v
        return self.tag(v)
