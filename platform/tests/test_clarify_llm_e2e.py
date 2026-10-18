"""M7b's model calls with real local models: a purpose sentence becomes a spec that generates
after answering only the questions that need a person, and a Spring Boot module gets a runtime
that binds its placeholders to middleware.

Skipped unless CAGENT_LLM_E2E=1 (needs the models in platform/models.yaml served by a local
Ollama). CI does not run it. On failure, each test prints the exact prompt first.
"""

import os

import pytest

from cagent.clarify import apply_answer, extract, extract_prompt
from cagent.draft import draft, draft_prompt, pick, skeletons
from cagent.generate import GenerationError, generate
from cagent.llm import LiteLLMCompleter, Router, load_model_config

from .conftest import REPO
from .test_draft import SHOP

pytestmark = pytest.mark.skipif(
    os.environ.get("CAGENT_LLM_E2E") != "1",
    reason="set CAGENT_LLM_E2E=1 (needs local Ollama models)",
)

PURPOSE = (
    "I want to check that order-service 1.4.2 stores orders in MySQL 8.0 and publishes an "
    "event to Kafka for each one. Creating an order should return 201."
)


def router() -> Router:
    return Router(load_model_config(REPO / "platform" / "models.yaml"), LiteLLMCompleter())


async def test_a_purpose_sentence_becomes_a_spec(catalog):
    print(extract_prompt(PURPOSE, catalog))
    spec = await extract(router(), PURPOSE, catalog)
    print(spec.model_dump_json(indent=2))
    assert [(s.name, s.source.kind, s.source.ref) for s in spec.services] == [
        ("order-service", "release", "1.4.2")
    ]
    assert {m.template: m.version for m in spec.middleware}.get("mysql") == "8.0"
    assert spec.checks
    asked = []
    for _ in range(10):
        try:
            generate(spec, catalog)
            break
        except GenerationError as e:
            gap = e.gaps[0]
            asked.append(gap.kind)
            spec = apply_answer(spec, gap, "dev" if gap.kind == "missing_env" else None)
    generate(spec, catalog)
    # Only the value no catalog can know is asked: the Spring profile.
    assert asked.count("missing_env") == 1 and "unknown_template" not in asked, asked


async def test_a_spring_boot_module_gets_a_runtime(catalog):
    sk = pick(skeletons(SHOP), "orders-api")
    print(draft_prompt(sk, SHOP["files"], catalog))
    d = await draft(router(), "orders-api", SHOP, catalog)
    print(d.text)
    binds = {r.capability: r.bind for r in d.descriptor.requires}
    assert binds["mysql"]["DB_URL"] == "jdbc:mysql://{{host}}:{{port}}/{{db}}"
    assert binds["mysql"]["DB_PASSWORD"] == "{{password}}"
    assert binds["kafka"]["KAFKA"] == "{{host}}:{{port}}"
    assert [p.container for p in d.descriptor.runtime.ports] == [8081]
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
