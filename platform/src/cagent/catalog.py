"""Service descriptors and middleware templates, and loading them from catalog directories."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cagent.render import placeholders

# Names we accept for compose projects, services, and catalog entries.
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
Name = Annotated[str, Field(pattern=NAME_PATTERN)]
PortNumber = Annotated[int, Field(gt=0, lt=65536)]


class Strict(BaseModel):
    """Base model that rejects unknown keys, so a typo in a catalog file fails loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Port(Strict):
    name: str
    container: PortNumber


class Healthcheck(Strict):
    """Either http (a path checked with curl on a named port) or cmd (a compose test)."""

    http: str | None = None
    port: str | None = None  # port name for http; defaults to the first port
    expect: int = 200
    cmd: list[str] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "Healthcheck":
        if (self.http is None) == (self.cmd is None):
            raise ValueError("healthcheck needs exactly one of http or cmd")
        return self


class Env(Strict):
    required: list[str] = []
    defaults: dict[str, str] = {}


class Runtime(Strict):
    ports: list[Port] = []
    env: Env = Env()
    healthcheck: Healthcheck | None = None

    @model_validator(mode="after")
    def _http_port_exists(self) -> "Runtime":
        hc = self.healthcheck
        if hc is not None and hc.http is not None:
            names = [p.name for p in self.ports]
            if not names:
                raise ValueError("http healthcheck needs at least one port")
            if hc.port is not None and hc.port not in names:
                raise ValueError(f"healthcheck port {hc.port!r} is not one of {names}")
        return self


class NexusSource(Strict):
    nexus: str


class CiSource(Strict):
    jenkins: str
    artifact: str


class WorkdirSource(Strict):
    build: str
    output: str


class Sources(Strict):
    release: NexusSource | None = None
    snapshot: NexusSource | None = None
    ci: CiSource | None = None
    workdir: WorkdirSource | None = None


class Requirement(Strict):
    capability: str
    version: str | None = None  # PEP 440 specifier, e.g. ">=8.0"
    bind: dict[str, str] = {}  # env var -> template over {{host}}, {{port}}, provider connection

    @field_validator("version")
    @classmethod
    def _valid_specifier(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                SpecifierSet(v)
            except InvalidSpecifier as e:
                raise ValueError(f"invalid version specifier {v!r}") from e
        return v


class HttpProbe(Strict):
    path: str
    expect: int = 200
    port: str | None = None


class Probe(Strict):
    http: HttpProbe


class ServiceDescriptor(Strict):
    """An application's `.cagent/service.yaml`."""

    name: Name
    kind: Literal["app"]
    stack: str
    sources: Sources
    runtime: Runtime
    requires: list[Requirement] = []
    probes: list[Probe] = []


class Provides(Strict):
    capability: str


class Preset(Strict):
    mem_limit: str
    cpus: float


class MiddlewareTemplate(Strict):
    """A platform-maintained middleware definition."""

    name: Name
    kind: Literal["middleware"]
    provides: Provides
    image: str  # "{version}" is replaced by the chosen version
    command: list[str] | None = None  # overrides the image's default command
    versions: list[str]
    default_version: str
    port: PortNumber
    connection: dict[str, str] = {}  # values bind templates may use besides host and port
    env: dict[str, str] = {}
    healthcheck: Healthcheck
    presets: dict[str, Preset]
    default_preset: str = "tiny"

    @model_validator(mode="after")
    def _consistent(self) -> "MiddlewareTemplate":
        if "{version}" not in self.image:
            raise ValueError("image must contain {version}")
        if self.default_version not in self.versions:
            raise ValueError(f"default_version {self.default_version!r} not in versions")
        if self.default_preset not in self.presets:
            raise ValueError(f"default_preset {self.default_preset!r} not in presets")
        known = {"host", "port", *self.connection}
        used = set().union(*(placeholders(v) for v in self.env.values()))
        used |= set().union(*(placeholders(c) for c in self.healthcheck.cmd or []))
        if unknown := used - known:
            raise ValueError(f"unknown placeholders: {', '.join(sorted(unknown))}")
        return self


class CatalogError(Exception):
    """A catalog file is missing, malformed, or duplicates another entry."""


@dataclass(frozen=True)
class Catalog:
    services: dict[str, ServiceDescriptor]
    middleware: dict[str, MiddlewareTemplate]


def load_catalog(*roots: Path) -> Catalog:
    """Load services/*.yaml and middleware/*.yaml from each root. Names must be unique."""
    services: dict[str, ServiceDescriptor] = {}
    middleware: dict[str, MiddlewareTemplate] = {}
    for root in roots:
        if not root.is_dir():
            raise CatalogError(f"{root}: not a directory")
        _load_dir(root / "services", ServiceDescriptor, services)
        _load_dir(root / "middleware", MiddlewareTemplate, middleware)
    return Catalog(services, middleware)


def _load_dir[M: Strict](directory: Path, model: type[M], into: dict[str, M]) -> None:
    for path in sorted(directory.glob("*.yaml")):
        try:
            item = model.model_validate(yaml.safe_load(path.read_text()))
        except (yaml.YAMLError, ValidationError) as e:
            raise CatalogError(f"{path}: {e}") from e
        if item.name in into:
            raise CatalogError(f"{path}: duplicate name {item.name!r}")
        into[item.name] = item
