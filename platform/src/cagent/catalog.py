"""Service descriptors and middleware templates, and loading them from catalog directories."""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cagent.render import placeholders
from cagent.secrets import safe_errors, yaml_problem

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
    """Either http (a path checked with curl on a named port) or cmd (a compose test)."""

    http: str | None = None
    port: str | None = None  # port name for http; defaults to the first port
    expect: int = 200
    cmd: list[str] | None = None

    @field_validator("http")
    @classmethod
    def _http_is_plain(cls, v: str | None) -> str | None:
        return _plain_http_path(v)

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
                raise ValueError("the healthcheck port is not one of the ports")
        return self


class NexusSource(Strict):
    nexus: str


class CiSource(Strict):
    jenkins: str
    artifact: str


def _inside(path: str) -> bool:
    """A relative path that stays inside its base lexically (symlinks are the Runner's job)."""
    if not path or posixpath.isabs(path):
        return False
    norm = posixpath.normpath(path)
    return norm != ".." and not norm.startswith("../")


class WorkdirSource(Strict):
    build: str | None = None  # the developer's command, run in the project root
    output: str | None = None  # java-springboot: glob matching exactly one jar
    dockerfile: str | None = None  # dockerfile: path to the project's Dockerfile
    context: str | None = None  # dockerfile: build context, default "."

    @model_validator(mode="after")
    def _paths_inside(self) -> "WorkdirSource":
        for path in (self.dockerfile, self.context):
            if path is not None and not _inside(path):
                raise ValueError("must be a relative path inside the project")
        return self


class Sources(Strict):
    release: NexusSource | None = None
    snapshot: NexusSource | None = None
    ci: CiSource | None = None
    workdir: WorkdirSource | None = None


# Init resources (M7c). Names end up in shell commands and CQL, so the patterns are strict.
MAX_SCRIPTS = 20  # per requirement; the Runner's initscripts package has the same limit
TOPIC_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"  # Kafka's own topic name rule
KEYSPACE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,47}$"  # Cassandra's unquoted identifier rule


class Topic(Strict):
    name: Annotated[str, Field(pattern=TOPIC_PATTERN)]
    partitions: Annotated[int, Field(ge=1, le=1000)] = 1


class RequirementInit(Strict):
    """What a service needs created in its middleware before it starts."""

    # Relative to the project root (workdir) or classpath paths inside the jar (release,
    # snapshot); run in this order.
    scripts: Annotated[list[str], Field(max_length=MAX_SCRIPTS)] = []
    keyspaces: list[Annotated[str, Field(pattern=KEYSPACE_PATTERN)]] = []
    topics: list[Topic] = []

    @field_validator("scripts")
    @classmethod
    def _scripts_inside(cls, scripts: list[str]) -> list[str]:
        for path in scripts:
            if not _inside(path):
                raise ValueError("a script must be a relative path inside the project")
        return scripts


class Requirement(Strict):
    capability: str
    version: str | None = None  # PEP 440 specifier, e.g. ">=8.0"
    bind: dict[str, str] = {}  # env var -> template over {{host}}, {{port}}, provider connection
    init: RequirementInit | None = None

    @field_validator("version")
    @classmethod
    def _valid_specifier(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                SpecifierSet(v)
            except InvalidSpecifier as e:
                raise ValueError("invalid version specifier") from e
        return v


class HttpProbe(Strict):
    path: str
    expect: int = 200
    port: str | None = None

    @field_validator("path")
    @classmethod
    def _path_is_plain(cls, v: str) -> str:
        return _plain_http_path(v) or v


class Probe(Strict):
    http: HttpProbe


class ServiceDescriptor(Strict):
    """An application's `.cagent/service.yaml` or `.cagent/services/<name>.yaml`.

    stack java-springboot images a jar with the Runner's template; dockerfile builds the
    project's own Dockerfile. The Runner's workdir package applies the same workdir rules.
    """

    name: Name
    kind: Literal["app"]
    stack: Literal["java-springboot", "dockerfile"]
    sources: Sources
    runtime: Runtime
    requires: list[Requirement] = []
    probes: list[Probe] = []

    @model_validator(mode="after")
    def _workdir_fits_stack(self) -> "ServiceDescriptor":
        w = self.sources.workdir
        if self.stack == "dockerfile":
            if self.sources.release or self.sources.snapshot or self.sources.ci:
                raise ValueError("stack dockerfile builds only from a workdir source")
            if w is None or w.dockerfile is None:
                raise ValueError("stack dockerfile needs sources.workdir.dockerfile")
        elif w is not None:
            if w.build is None or w.output is None:
                raise ValueError("sources.workdir needs build and output")
            if w.dockerfile is not None or w.context is not None:
                raise ValueError("sources.workdir: dockerfile and context need stack dockerfile")
        return self


class Provides(Strict):
    capability: str


class Preset(Strict):
    mem_limit: str
    cpus: float


class InitCommand(Strict):
    run: str  # one shell command, run once per keyspace or topic


class ScriptInit(Strict):
    """How a template runs one script file ($file) and records it by content ($sha)."""

    suffix: Annotated[str, Field(pattern=r"^\.[a-z0-9]+$")]
    run: str
    marked: str  # exits 0 when $sha is already recorded
    mark: str  # records $sha after run succeeded


class TemplateInit(Strict):
    """The init kinds a template supports. Commands may use {{host}}, {{port}}, and the
    connection values; keyspaces add {{keyspace}}, topics {{topic}} and {{partitions}}."""

    scripts: ScriptInit | None = None
    keyspaces: InitCommand | None = None
    topics: InitCommand | None = None
    mem_limit: str = "256m"
    # Keys of the template's env the init service gets, with the values the middleware
    # service has; commands read them as "$KEY", so no secret is written into a command.
    env: list[str] = []


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
    init: TemplateInit | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "MiddlewareTemplate":
        if "{version}" not in self.image:
            raise ValueError("image must contain {version}")
        if self.default_version not in self.versions:
            raise ValueError("default_version is not one of versions")
        if self.default_preset not in self.presets:
            raise ValueError("default_preset is not one of presets")
        known = {"host", "port", *self.connection}
        used = set().union(*(placeholders(v) for v in self.env.values()))
        used |= set().union(*(placeholders(c) for c in self.healthcheck.cmd or []))
        if used - known:
            raise ValueError("a placeholder is not host, port, or a connection value")
        if self.init is not None:
            self._check_init(self.init, known)
            if set(self.init.env) - set(self.env):
                raise ValueError("init.env names a key the template's env does not set")
        return self

    @staticmethod
    def _check_init(init: TemplateInit, known: set[str]) -> None:
        commands: dict[str, tuple[list[str], set[str]]] = {}
        if init.scripts:
            s = init.scripts
            commands["scripts"] = ([s.run, s.marked, s.mark], known)
        if init.keyspaces:
            commands["keyspaces"] = ([init.keyspaces.run], known | {"keyspace"})
        if init.topics:
            commands["topics"] = ([init.topics.run], known | {"topic", "partitions"})
        for kind, (texts, allowed) in commands.items():
            used = set().union(*(placeholders(t) for t in texts))
            if used - allowed:
                raise ValueError(f"init.{kind}: a placeholder is not one this command may use")


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
        except yaml.YAMLError as e:
            raise CatalogError(f"{path}: {yaml_problem(e)}") from None
        except ValidationError as e:
            raise CatalogError(f"{path}: {'; '.join(safe_errors(e))}") from None
        if item.name in into:
            raise CatalogError(f"{path}: duplicate name {item.name!r}")
        into[item.name] = item
