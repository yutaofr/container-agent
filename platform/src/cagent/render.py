"""Placeholder rendering for catalog templates, and escaping for compose files."""

import re
from collections.abc import Mapping

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class RenderError(ValueError):
    """A template used a placeholder that has no value."""


def placeholders(template: str) -> set[str]:
    """Return the placeholder names used in template."""
    return set(_PLACEHOLDER.findall(template))


def render(template: str, values: Mapping[str, str]) -> str:
    """Replace each {{name}} in template with values[name]."""

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise RenderError(f"unknown placeholder {{{{{key}}}}} in {template!r}")
        return values[key]

    return _PLACEHOLDER.sub(sub, template)


def escape_compose(value: str) -> str:
    """Escape $ so docker compose does not treat it as variable interpolation."""
    return value.replace("$", "$$")
