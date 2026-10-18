"""Gaps: what stops a ClusterSpec from generating, each with the answers that would close it.

The generator reports every gap at once (design 6.3); Clarify asks about one at a time
(M7b design 5.1). str(gap) is the generator's plain message, for the CLI and escalations.
"""

from dataclasses import asdict, dataclass
from typing import Any

# Gaps a person can close by answering a question. unregistered and bind_render cannot be:
# the first needs a descriptor, the second is a catalog bug.
ANSWERABLE = frozenset(
    {
        "unknown_template",
        "unsupported_version",
        "unknown_preset",
        "no_source",
        "missing_env",
        "unknown_port",
        "binding",
        "version_mismatch",
        "no_provider",
    }
)


@dataclass(frozen=True)
class Gap:
    kind: str
    target: str  # a service, a middleware instance, or "consumer/capability"
    message: str
    options: tuple[str, ...] = ()  # the valid answers; empty means a free value
    default: str | None = None
    # missing_env: the variable; unknown_port: the port name; version_mismatch: the provider
    # instance; bind_render: the variable
    key: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "options": list(self.options)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Gap":
        return cls(**{**d, "options": tuple(d.get("options", ()))})
