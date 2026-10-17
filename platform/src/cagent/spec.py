"""ClusterSpec: the structured intent that conversation fills in and the generator consumes."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from cagent.catalog import Name, PortNumber, Strict


class Source(Strict):
    kind: Literal["release", "snapshot", "ci", "workdir"]
    ref: str = Field(min_length=1)  # version, CI build, or working-directory path


class ServiceRef(Strict):
    name: Name
    source: Source
    env: dict[str, str] = {}
    publish: dict[str, PortNumber] = {}  # port name -> host port


class MiddlewareRef(Strict):
    template: str
    name: Name | None = None  # instance name; defaults to the template name
    version: str | None = None
    preset: str | None = None
    publish: PortNumber | None = None  # host port for the template's port

    @property
    def instance(self) -> str:
        return self.name or self.template


class ClusterSpec(Strict):
    name: Name
    purpose: str
    services: list[ServiceRef] = []
    middleware: list[MiddlewareRef] = []
    bindings: dict[str, str] = {}  # "consumer/capability" -> middleware instance
    checks: list[str] = []  # L4 user-defined checks, free text until M6

    @model_validator(mode="after")
    def _unique_names(self) -> "ClusterSpec":
        names = [s.name for s in self.services] + [m.instance for m in self.middleware]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"duplicate names: {', '.join(dupes)}")
        return self


class SpecError(Exception):
    """A spec file is malformed."""


def load_spec(path: Path) -> ClusterSpec:
    try:
        return ClusterSpec.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise SpecError(f"{path}: {e}") from e
