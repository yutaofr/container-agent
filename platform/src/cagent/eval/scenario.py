"""Fault scenarios: what one declares, how it loads, and the checks before any run (lint).

A scenario lives in `scenarios/<id>/scenario.yaml`, with an optional `catalog/` layer of
uniquely named templates. Faults are injected before generation only: through the spec, the
catalog layers, fixture projects, and built-in host preconditions (design sections 4 and 5).

Placeholders such as `${taken}` name a value the harness binds (a held host port). They are
typed and positional: a placeholder is a whole string, allowed only where a port or an
expected value goes, and it becomes an integer. Nothing is substituted in text, and nothing
reads the environment.
"""

import copy
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import jsonpatch
import yaml
from pydantic import ConfigDict, Field, ValidationError, model_validator

from cagent.catalog import Catalog, CatalogError, Name, ServiceDescriptor, Strict, load_catalog
from cagent.generate import GenerationError, generate
from cagent.spec import ClusterSpec

PLACEHOLDER = re.compile(r"\$\{([a-z][a-z0-9_]*)\}")
Scalar = str | int | float | bool | None

_VALUE_OPS = ("equals", "not_equals", "publishes", "not_publishes")
_OPS = (*_VALUE_OPS, "exists")

# Where a placeholder may stand, as paths of keys and list indexes ("*" matches any).
_PLACEHOLDER_AT = [
    ("spec", "middleware", "*", "publish"),
    ("spec", "services", "*", "publish", "*"),
    *(("expect", part, "*", op) for part in ("config", "after") for op in _VALUE_OPS),
]
_SKIP_DIRS = {"fixtures", "sets"}  # directories under the root that are not scenarios
_SKIP_HASH = {"target", ".git", "__pycache__"}


class OccupiedPort(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: Name = Field(alias="as")


class HostCondition(Strict):
    """A built-in precondition on the host; v1 knows only a held port."""

    occupied_port: OccupiedPort


class Baseline(Strict):
    """The healthy variant: a JSON Patch on the spec. Host conditions never apply to it."""

    spec_patch: list[dict[str, Any]] = []


class Approval(Strict):
    """The only human input a run gets: the initial deployment approval."""

    initial: Literal["approve"] = "approve"
    allow: list[str] = []


class PointerCheck(Strict):
    """One expectation on a compose document. publishes and not_publishes read a ports list
    and compare host ports as integers, so a held port needs no text substitution."""

    pointer: str
    equals: Scalar = None
    not_equals: Scalar = None
    exists: bool | None = None
    publishes: int | str | None = None  # a str only while it is a placeholder
    not_publishes: int | str | None = None

    @model_validator(mode="after")
    def _one(self) -> "PointerCheck":
        if not self.pointer.startswith("/"):
            raise ValueError(f"pointer {self.pointer!r} must start with /")
        if len(set(_OPS) & self.model_fields_set) != 1:
            raise ValueError(f"a pointer check needs exactly one of {', '.join(_OPS)}")
        return self

    @property
    def op(self) -> str:
        return next(k for k in _OPS if k in self.model_fields_set)

    @property
    def value(self) -> Any:
        return getattr(self, self.op)


class RuntimeCheck(Strict):
    """Evidence in one service record of one call: a signature, a line, or both. A line
    alone names its service, for faults whose first symptom has no stable signature."""

    signature: str | None = Field(default=None, pattern=r"^[a-z]+:[^@\s]+@[a-z0-9][a-z0-9_-]*$")
    service: Name | None = None
    line: str | None = None

    @model_validator(mode="after")
    def _names_a_service(self) -> "RuntimeCheck":
        if self.signature is None and (self.service is None or self.line is None):
            raise ValueError("a runtime check needs a signature, or a service and a line")
        if self.signature is not None and self.service is not None:
            raise ValueError("a runtime check names its service once: in signature or service")
        return self

    @property
    def target(self) -> str:
        """The service the evidence must come from."""
        return self.service or self.signature.rpartition("@")[2]

    @property
    def label(self) -> str:
        what = self.signature or f"@{self.service}"
        return what + (f" / {self.line!r}" if self.line else "")


class HttpCheck(Strict):
    port: int = Field(gt=0, lt=65536)
    path: str = Field(pattern=r"^/")
    expect: int = 200


class TcpCheck(Strict):
    port: int = Field(gt=0, lt=65536)


class Check(Strict):
    """A business check, in the Runner verify probe's own shape."""

    service: Name
    http: HttpCheck | None = None
    tcp: TcpCheck | None = None

    @model_validator(mode="after")
    def _one(self) -> "Check":
        if (self.http is None) == (self.tcp is None):
            raise ValueError("a check needs exactly one of http or tcp")
        return self

    def probe(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class NeverCheck(Strict):
    """A protected field no patch may affect, in proposed or applied patches."""

    scope: Literal["proposed", "applied"]
    field: str = Field(pattern=r"^/services(/[^/]+)+$")


class Expect(Strict):
    config: list[PointerCheck] = []
    runtime: list[RuntimeCheck] = Field(min_length=1)
    outcome: Literal["healthy", "patch_approval", "escalation"]
    escalation_reasons: list[str] = []  # empty: any reason
    approval_reason: str | None = None  # a substring of one patch-approval reason
    accept_rounds: int | None = Field(default=None, ge=0)
    after: list[PointerCheck] = []
    checks: list[Check] = []
    never: list[NeverCheck] = []

    @model_validator(mode="after")
    def _fits_outcome(self) -> "Expect":
        if self.outcome == "healthy" and self.accept_rounds is None:
            raise ValueError("outcome healthy needs accept_rounds")
        if self.outcome != "healthy" and self.checks:
            raise ValueError("checks run only after healthy")
        if self.outcome != "escalation" and self.escalation_reasons:
            raise ValueError("escalation_reasons need outcome escalation")
        if self.outcome != "patch_approval" and self.approval_reason is not None:
            raise ValueError("approval_reason needs outcome patch_approval")
        return self


class Scenario(Strict):
    id: Name
    kind: Literal["repair", "safety", "combo"]
    category: Name
    platforms: Literal["any"] | list[str] = "any"  # e.g. ["linux/arm64"]
    spec: dict[str, Any]  # a ClusterSpec without its name
    host: list[HostCondition] = []
    baseline: Baseline
    approval: Approval = Approval()
    expect: Expect
    hints: list[str] = []  # scenario-specific text that must never reach a prompt

    @model_validator(mode="after")
    def _consistent(self) -> "Scenario":
        if "name" in self.spec:
            raise ValueError("spec must not set name: the harness names each cluster")
        if self.kind == "repair" and self.expect.outcome != "healthy":
            raise ValueError("a repair scenario expects outcome healthy")
        if self.kind == "safety" and self.expect.outcome == "healthy":
            raise ValueError("a safety scenario expects a stop, not healthy")
        names = [h.occupied_port.name for h in self.host]
        if len(names) != len(set(names)):
            raise ValueError("host condition names must be unique")
        return self

    @property
    def placeholders(self) -> list[str]:
        """Names the host conditions bind."""
        return [h.occupied_port.name for h in self.host]


class ScenarioSet(Strict):
    purpose: str
    scenarios: list[Name] = Field(min_length=1)


class ScenarioError(Exception):
    """A scenario or set file is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class Resolved:
    """A scenario with its placeholders bound: what one attempt runs and expects."""

    spec: ClusterSpec
    expect: Expect


@dataclass(frozen=True)
class LoadedScenario:
    scenario: Scenario
    dir: Path
    root: Path  # the scenarios directory
    catalog: Catalog

    @property
    def id(self) -> str:
        return self.scenario.id

    def resolve(self, name: str, values: dict[str, int]) -> Resolved:
        """The fault variant, with the harness's cluster name and bound values."""
        raw = self.scenario.model_dump(mode="json", by_alias=True, exclude_unset=True)
        raw = _substitute(raw, values)
        spec = self._spec(raw["spec"], name)
        return Resolved(spec, Expect.model_validate(raw["expect"]))

    def baseline(self, name: str, values: dict[str, int]) -> ClusterSpec:
        """The healthy variant: spec_patch applied; values stand in for held ports."""
        raw = self.scenario.model_dump(mode="json", by_alias=True, exclude_unset=True)
        spec = jsonpatch.apply_patch(copy.deepcopy(raw["spec"]), self.scenario.baseline.spec_patch)
        return self._spec(_substitute({"spec": spec}, values)["spec"], name)

    def _spec(self, raw: dict[str, Any], name: str) -> ClusterSpec:
        raw = copy.deepcopy(raw)
        for svc in raw.get("services") or []:
            source = svc.get("source") or {}
            if source.get("kind") == "workdir":
                source["ref"] = str(self.workdir(source["ref"]))
        return ClusterSpec.model_validate({"name": name, **raw})

    def workdir(self, ref: str) -> Path:
        """A fixture project: a path relative to the scenarios root, inside fixtures/."""
        path = (self.root / ref).resolve()
        fixtures = (self.root / "fixtures").resolve()
        if Path(ref).is_absolute() or not path.is_relative_to(fixtures) or not path.is_dir():
            raise ScenarioError(f"{self.id}: workdir {ref!r} must be a directory in fixtures/")
        return path

    def workdirs(self) -> list[Path]:
        return [
            self.workdir(s["source"]["ref"])
            for s in self.scenario.spec.get("services") or []
            if (s.get("source") or {}).get("kind") == "workdir"
        ]

    def descriptors(self) -> dict[str, ServiceDescriptor]:
        """Every descriptor the fixture projects carry in `.cagent/services/`, as the
        orchestrator reads them back; the baseline may use another one than the fault."""
        out = {}
        for project in self.workdirs():
            for path in sorted((project / ".cagent" / "services").glob("*.yaml")):
                try:
                    d = ServiceDescriptor.model_validate(_read_yaml(path))
                except ValidationError as e:
                    raise ScenarioError(f"{path}: {e}") from e
                out[d.name] = d
        return out

    def images(self) -> set[str]:
        """Images the scenario runs or builds from: every non-built image of its fault and
        baseline compose, and the FROM images of its fixture Dockerfiles."""
        catalog = Catalog({**self.catalog.services, **self.descriptors()}, self.catalog.middleware)
        values = {n: 20000 + i for i, n in enumerate(self.scenario.placeholders)}
        out = set()
        for spec in (self.resolve("images", values).spec, self.baseline("images", values)):
            for svc in generate(spec, catalog)["services"].values():
                if not str(svc.get("image", "")).startswith("cagent/"):
                    out.add(svc["image"])
        for project in self.workdirs():
            for d in self.descriptors().values():
                w = d.sources.workdir
                if w is None or w.dockerfile is None or not (project / w.dockerfile).is_file():
                    continue
                stages = set()
                for line in (project / w.dockerfile).read_text().splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].upper() == "FROM":
                        if parts[1] not in stages:
                            out.add(parts[1])
                        if len(parts) >= 4 and parts[2].upper() == "AS":
                            stages.add(parts[3])
        return out

    def files(self) -> dict[str, str]:
        """SHA-256 of every file the scenario depends on outside the repository catalog:
        its own directory, the shared fixture catalog, and its fixture projects."""
        dirs = [self.dir, self.root / "fixtures" / "catalog", *self.workdirs()]
        out: dict[str, str] = {}
        for d in dirs:
            for path in sorted(_walk(d)):
                rel = path.relative_to(self.root.parent).as_posix()
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out


def _walk(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.name in _SKIP_HASH:
            continue
        if path.is_dir():
            yield from _walk(path)
        elif path.is_file():
            yield path


def _placeholders(value: Any, path: tuple = ()) -> Iterator[tuple[tuple, str]]:
    """(path, name) of every string that holds a placeholder anywhere in value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _placeholders(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _placeholders(v, (*path, i))
    elif isinstance(value, str):
        for m in PLACEHOLDER.finditer(value):
            yield path, m.group(1) if PLACEHOLDER.fullmatch(value) else f"!{m.group(1)}"


def _allowed(path: tuple) -> bool:
    return any(
        len(p) == len(path) and all(a == "*" or a == b for a, b in zip(p, path, strict=True))
        for p in _PLACEHOLDER_AT
    )


def _substitute(value: Any, values: dict[str, int]) -> Any:
    """Replace every whole-string placeholder with its integer value."""
    if isinstance(value, dict):
        return {k: _substitute(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, values) for v in value]
    if isinstance(value, str) and (m := PLACEHOLDER.fullmatch(value)):
        if m.group(1) not in values:
            raise ScenarioError(f"no value for placeholder {value}")
        return values[m.group(1)]
    return value


def bound_expect(scenario: Scenario, values: dict[str, int]) -> Expect:
    """The scenario's expectations with the attempt's bound values."""
    raw = scenario.expect.model_dump(mode="json", exclude_unset=True)
    return Expect.model_validate(_substitute(raw, values))


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ScenarioError(f"{path}: {e}") from e


def load_scenario(directory: Path, repo_catalog: Path) -> LoadedScenario:
    """Load scenarios/<id>/ with its catalog: the repository's, the shared fixture layer,
    and the scenario's own layer, which may only add names."""
    path = directory / "scenario.yaml"
    try:
        scenario = Scenario.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ScenarioError(f"{path}: {e}") from e
    if scenario.id != directory.name:
        raise ScenarioError(f"{path}: id {scenario.id!r} differs from its directory")
    root = directory.parent
    layers = [d for d in (root / "fixtures" / "catalog", directory / "catalog") if d.is_dir()]
    try:
        catalog = load_catalog(repo_catalog, *layers)
    except CatalogError as e:
        raise ScenarioError(f"{scenario.id}: {e}") from e
    return LoadedScenario(scenario, directory, root, catalog)


def scenario_dirs(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if d.is_dir() and d.name not in _SKIP_DIRS)


def load_sets(root: Path) -> dict[str, ScenarioSet]:
    sets = {}
    for path in sorted((root / "sets").glob("*.yaml")):
        try:
            sets[path.stem] = ScenarioSet.model_validate(_read_yaml(path))
        except ValidationError as e:
            raise ScenarioError(f"{path}: {e}") from e
    return sets


def lint_scenario(loaded: LoadedScenario) -> list[str]:
    """Problems with one loaded scenario; empty when it can run."""
    s, problems = loaded.scenario, []
    raw = s.model_dump(mode="json", by_alias=True, exclude_unset=True)
    used = set()
    for path, name in _placeholders(raw):
        if name.startswith("!") or not _allowed(path):
            where = "/".join(str(p) for p in path)
            problems.append(
                f"placeholder at {where}: only a whole value at a port or expected value"
            )
        used.add(name.lstrip("!"))
    declared = set(s.placeholders)
    if missing := sorted(used - declared):
        problems.append(f"undeclared placeholders: {', '.join(missing)}")
    if unused := sorted(declared - used):
        problems.append(f"host conditions never used: {', '.join(unused)}")
    if not s.host and not s.baseline.spec_patch:
        problems.append("no fault: the baseline equals the scenario")
    if problems:
        return problems
    values = {n: 20000 + i for i, n in enumerate(s.placeholders)}
    try:
        catalog = loaded.catalog
        if descriptors := loaded.descriptors():
            catalog = Catalog({**catalog.services, **descriptors}, catalog.middleware)
        for label, spec in (
            ("scenario", loaded.resolve("lint", values).spec),
            ("baseline", loaded.baseline("lint", values)),
        ):
            try:
                generate(spec, catalog)
            except GenerationError as e:
                problems.append(f"{label} does not generate: {'; '.join(e.problems)}")
    except (ScenarioError, ValidationError, OSError, jsonpatch.JsonPatchException) as e:
        problems.append(str(e))
    return problems


def lint(root: Path, repo_catalog: Path) -> list[str]:
    """Every problem in the scenario library: scenarios, catalog layers, and sets."""
    problems, ids = [], set()
    for d in scenario_dirs(root):
        try:
            loaded = load_scenario(d, repo_catalog)
        except ScenarioError as e:
            problems.append(str(e))
            continue
        ids.add(loaded.id)
        problems += [f"{loaded.id}: {p}" for p in lint_scenario(loaded)]
    try:
        sets = load_sets(root)
    except ScenarioError as e:
        return [*problems, str(e)]
    for name, st in sets.items():
        if dupes := sorted({i for i in st.scenarios if st.scenarios.count(i) > 1}):
            problems.append(f"set {name}: listed twice: {', '.join(dupes)}")
        if unknown := sorted(set(st.scenarios) - ids):
            problems.append(f"set {name}: unknown scenarios: {', '.join(unknown)}")
    return problems
