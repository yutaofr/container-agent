"""platform.yaml: where the platform listens, who may use it, and which Runners exist.

Secrets are never in the file: each token is read from the environment variable it names.
Loading fails on anything that would make access ambiguous or unsafe: a missing or short
token, two equal tokens anywhere, a user naming an unknown Runner, or a web origin that
does not match how cookies will be sent."""

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MIN_TOKEN = 32  # characters; tokens are random strings, not passwords


class ConfigError(Exception):
    """platform.yaml or the environment is not usable."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Runner(_Strict):
    token_env: str


class _Hub(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765
    runners: dict[str, _Runner]


class _Web(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    origin: str
    secure_cookies: bool = False
    dev_http: bool = False  # plain http, allowed only on this machine
    session_ttl_s: float = Field(default=12 * 3600, gt=0)
    idle_ttl_s: float = Field(default=3600, gt=0)


class _User(_Strict):
    token_env: str
    runners: list[str]


class _Db(_Strict):
    path: str


class _File(_Strict):
    db: _Db
    catalog: list[str]
    models: str | None = None
    hub: _Hub
    web: _Web
    users: dict[str, _User]


@dataclass(frozen=True)
class PlatformConfig:
    db_path: Path
    catalog: list[Path]
    models: Path | None
    hub_host: str
    hub_port: int
    runner_tokens: dict[str, str]  # token -> runner id
    web_host: str
    web_port: int
    origin: str
    secure_cookies: bool
    session_ttl_s: float
    idle_ttl_s: float
    user_tokens: dict[str, str]  # user -> token
    user_runners: dict[str, set[str]]


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_origin(origin: str) -> str:
    """An origin is exactly scheme://host[:port]."""
    parts = urlsplit(origin)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigError(f"web.origin {origin!r} must be exactly scheme://host[:port]")
    return origin


def load_config(path: str | Path, environ: Mapping[str, str] = os.environ) -> PlatformConfig:
    path = Path(path)
    try:
        raw = _File.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"{path}: {e}") from None
    base = path.parent

    seen: dict[str, str] = {}

    def token(owner: str, var: str) -> str:
        value = environ.get(var, "")
        if not value:
            raise ConfigError(f"{owner}: environment variable {var} is missing or empty")
        if len(value) < MIN_TOKEN:
            raise ConfigError(f"{owner}: the token in {var} is shorter than {MIN_TOKEN} characters")
        if value in seen:
            raise ConfigError(f"{owner} and {seen[value]} have the same token")
        seen[value] = owner
        return value

    runner_tokens = {token(f"runner {r}", c.token_env): r for r, c in raw.hub.runners.items()}
    user_tokens = {u: token(f"user {u}", c.token_env) for u, c in raw.users.items()}
    for user, c in raw.users.items():
        if unknown := set(c.runners) - set(raw.hub.runners):
            raise ConfigError(f"user {user}: unknown runners {sorted(unknown)}")

    web = raw.web
    origin = check_origin(web.origin)
    scheme = urlsplit(origin).scheme
    if scheme == "https" and not web.secure_cookies:
        raise ConfigError("an https origin needs web.secure_cookies: true")
    if scheme == "http":
        if web.secure_cookies:
            raise ConfigError("web.secure_cookies needs an https origin")
        if not (web.dev_http and _loopback(urlsplit(origin).hostname) and _loopback(web.host)):
            raise ConfigError(
                "plain http is only for local development: set web.dev_http and keep the "
                "origin and web.host on loopback; otherwise serve https"
            )
    return PlatformConfig(
        db_path=(base / raw.db.path),
        catalog=[base / c for c in raw.catalog],
        models=(base / raw.models) if raw.models else None,
        hub_host=raw.hub.host,
        hub_port=raw.hub.port,
        runner_tokens=runner_tokens,
        web_host=web.host,
        web_port=web.port,
        origin=origin,
        secure_cookies=web.secure_cookies,
        session_ttl_s=web.session_ttl_s,
        idle_ttl_s=web.idle_ttl_s,
        user_tokens=user_tokens,
        user_runners={u: set(c.runners) for u, c in raw.users.items()},
    )
