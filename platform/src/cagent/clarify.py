"""Clarify: a purpose sentence becomes a ClusterSpec (M7b design 5).

A model reads the purpose once and drafts the spec from catalog names. After that, every
question comes from a structured generator gap, with its options and default, and every
answer is a plain value checked here: no model reads the user's answers.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from cagent.catalog import Catalog
from cagent.gaps import ANSWERABLE, Gap
from cagent.llm import Router
from cagent.spec import ClusterSpec

_ASK = {
    "unknown_template": "Which template should {target} use?",
    "unsupported_version": "Which version should {target} use?",
    "unknown_preset": "Which preset should {target} use?",
    "no_source": "Which source should {target} build from?",
    "missing_env": "What value should {target} get for {key}?",
    "unknown_port": "Which port should {target} publish instead of {key}?",
    "binding": "Which instance should {target} use?",
    "version_mismatch": "Which version should {key} use?",
    "no_provider": "Which middleware should provide it?",
}


class AnswerError(ValueError):
    """The answer does not close the gap; ask again with this message."""


def question(gap: Gap) -> str:
    return f"{gap.message}. {_ASK[gap.kind].format(target=gap.target, key=gap.key)}"


def _middleware(doc: dict[str, Any], instance: str) -> dict[str, Any]:
    return next(m for m in doc["middleware"] if (m.get("name") or m["template"]) == instance)


def _service(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in doc["services"] if s["name"] == name)


def apply_answer(spec: ClusterSpec, gap: Gap, answer: str | None) -> ClusterSpec:
    """Return a new spec with the answer applied. None takes the gap's default."""
    if gap.kind not in ANSWERABLE or (gap.kind != "missing_env" and not gap.options):
        raise AnswerError(f"{gap.message}: this gap cannot be answered here")
    value = gap.default if answer is None else answer.strip()
    if not value:
        raise AnswerError("an answer is required")
    if gap.options and value not in gap.options:
        raise AnswerError(f"choose one of {', '.join(gap.options)}")

    doc = spec.model_dump()
    match gap.kind:
        case "unknown_template":
            _middleware(doc, gap.target)["template"] = value
        case "unsupported_version":
            _middleware(doc, gap.target)["version"] = value
        case "version_mismatch":
            _middleware(doc, gap.key)["version"] = value
        case "unknown_preset":
            _middleware(doc, gap.target)["preset"] = value
        case "no_source":
            _service(doc, gap.target)["source"]["kind"] = value
        case "missing_env":
            _service(doc, gap.target)["env"][gap.key] = value
        case "unknown_port":
            publish = _service(doc, gap.target)["publish"]
            publish[value] = publish.pop(gap.key)
        case "binding":
            doc["bindings"][gap.target] = value
        case "no_provider":
            taken = {s["name"] for s in doc["services"]} | {
                m.get("name") or m["template"] for m in doc["middleware"]
            }
            name, n = value, 1
            while name in taken:
                n += 1
                name = f"{value}-{n}"
            doc["middleware"].append({"template": value, "name": None if n == 1 else name})
    return ClusterSpec.model_validate(doc)


class _Source(BaseModel):
    kind: Literal["release", "snapshot", "ci", "workdir"]
    ref: str = Field(min_length=1)  # an empty ref is a schema error, so the model retries


class _Service(BaseModel):
    name: str
    source: _Source


class _Middleware(BaseModel):
    template: str
    version: str | None = None


class Extraction(BaseModel):
    """The model's draft. Names are normalized and checked after it answers."""

    name: str
    services: list[_Service] = []
    middleware: list[_Middleware] = []
    checks: list[str] = []


EXTRACT_SYSTEM = (
    "You turn a developer's description of a throwaway test cluster into a cluster spec. "
    "Use catalog names when the text means them, and keep any other name as written. "
    "A service built from a local directory has source kind workdir with the directory as "
    "ref; a released version is kind release with the version as ref; a snapshot version is "
    "kind snapshot. List only the middleware the text asks for: what services need is added "
    "later. checks are the acceptance checks the text states, one sentence each. "
    "Answer only with JSON matching the schema."
)


def _slug(text: str) -> str:
    """A name matching the catalog's pattern: lowercase letters, digits, - and _."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-_")
    return slug or "cluster"


def extract_prompt(purpose: str, catalog: Catalog) -> str:
    kinds = ("release", "snapshot", "ci", "workdir")
    services = [
        f"- {d.name}: sources {', '.join(k for k in kinds if getattr(d.sources, k))}"
        for d in sorted(catalog.services.values(), key=lambda d: d.name)
    ]
    templates = [
        f"- {t.name}: provides {t.provides.capability}, versions {', '.join(t.versions)}"
        for t in sorted(catalog.middleware.values(), key=lambda t: t.name)
    ]
    return "\n".join(
        [
            "Services in the catalog:",
            *(services or ["- (none)"]),
            "Middleware templates:",
            *(templates or ["- (none)"]),
            "Purpose:",
            purpose,
        ]
    )


async def extract(router: Router, purpose: str, catalog: Catalog) -> ClusterSpec:
    """Draft a spec from the purpose with one call on the top tier (design 4, step 1).
    Unknown names stay, so the generator reports them as gaps to ask about."""
    draft, _ = await router.ask(
        EXTRACT_SYSTEM,
        extract_prompt(purpose, catalog),
        Extraction,
        start=router.config.order[-1],
        think=False,
    )
    services: dict[str, dict[str, Any]] = {}
    middleware: dict[str, dict[str, Any]] = {}
    for m in draft.middleware:
        template = _slug(m.template)
        if template not in catalog.services:
            middleware.setdefault(template, {"template": template, "version": m.version})
    for svc in draft.services:
        name = _slug(svc.name)
        if name in catalog.middleware and name not in catalog.services:
            # Models listed MySQL or Kafka as a service too: it is the template.
            middleware.setdefault(name, {"template": name, "version": None})
            continue
        services.setdefault(name, {"name": name, "source": svc.source.model_dump()})
    return ClusterSpec.model_validate(
        {
            "name": _slug(draft.name),
            "purpose": purpose,
            "services": list(services.values()),
            "middleware": list(middleware.values()),
            "checks": [c for c in draft.checks if c.strip()],
        }
    )
