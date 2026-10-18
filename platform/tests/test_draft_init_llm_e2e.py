"""M7c's model call with a real local model: a Spring Boot module whose configuration names a
Kafka topic gets that topic, by its resolved name, next to the scripts code mapped.

Skipped unless CAGENT_LLM_E2E=1 (needs the models in platform/models.yaml served by a local
Ollama). CI does not run it. On failure, the test prints the exact prompt first.
"""

import os

import pytest

from cagent.draft import draft, draft_prompt, pick, skeletons

from .test_clarify_llm_e2e import router
from .test_draft_init import SHOP

pytestmark = pytest.mark.skipif(
    os.environ.get("CAGENT_LLM_E2E") != "1",
    reason="set CAGENT_LLM_E2E=1 (needs local Ollama models)",
)


async def test_a_topic_in_the_configuration_is_drafted_by_its_resolved_name(catalog):
    sk = pick(skeletons(SHOP), "orders-api")
    print(draft_prompt(sk, SHOP["files"], catalog))
    d = await draft(router(), "orders-api", SHOP, catalog)
    print(d.text, d.note, sep="\n")
    init = {r.capability: r.init for r in d.descriptor.requires}
    assert [t.name for t in init["kafka"].topics] == ["orders-events"]
    assert init["mysql"].scripts == [
        "api/src/main/resources/db/init/01-schema.sql",
        "api/src/main/resources/db/init/02-seed.sql",
    ]
