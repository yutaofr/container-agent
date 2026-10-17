"""The generator must only emit keys that `cagent-runner validate` treats as reviewed."""

import json

from cagent.generate import generate

from .conftest import REPO

ALLOWED = json.loads((REPO / "runner/internal/compose/allowed_fields.json").read_text())


def test_generated_keys_are_on_the_runner_allow_list(catalog, orders_spec):
    compose = generate(orders_spec, catalog)
    assert set(compose) <= set(ALLOWED["top_level"])
    for name, service in compose["services"].items():
        extra = set(service) - set(ALLOWED["service"])
        assert not extra, f"{name} uses keys validate would flag: {sorted(extra)}"
