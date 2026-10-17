from cagent.matching import match

from .conftest import spec_with


def providers(bindings):
    return {(b.consumer, b.requirement.capability): b.provider for b in bindings}


def test_each_requirement_binds_to_its_provider(catalog, orders_spec):
    bindings, problems = match(orders_spec, catalog)
    assert problems == []
    assert providers(bindings) == {
        ("order-service", "mysql"): "mysql",
        ("order-service", "kafka"): "kafka",
    }


def test_version_mismatch_is_reported(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql", "version": "8.0"},
            {"template": "kafka"},
        ]
    )
    # Tighten the requirement so 8.0 no longer satisfies it.
    desc = catalog.services["order-service"]
    req = desc.requires[0].model_copy(update={"version": ">=8.4"})
    catalog.services["order-service"] = desc.model_copy(
        update={"requires": [req, desc.requires[1]]}
    )
    _, problems = match(spec, catalog)
    assert problems == ["order-service/mysql: needs version >=8.4; mysql is 8.0"]


def test_missing_provider_is_reported(catalog):
    _, problems = match(spec_with(middleware=[{"template": "mysql"}]), catalog)
    assert problems == ["order-service/kafka: no middleware in the spec provides kafka"]


def test_two_providers_are_ambiguous(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ]
    )
    _, problems = match(spec, catalog)
    assert problems == [
        "order-service/mysql: ambiguous, candidates mysql, mysql-b; "
        "set bindings['order-service/mysql']"
    ]


def test_explicit_binding_resolves_ambiguity(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ],
        bindings={"order-service/mysql": "mysql-b"},
    )
    bindings, problems = match(spec, catalog)
    assert problems == []
    assert providers(bindings)[("order-service", "mysql")] == "mysql-b"


def test_binding_to_wrong_capability_is_reported(catalog):
    spec = spec_with(bindings={"order-service/mysql": "kafka"})
    _, problems = match(spec, catalog)
    assert problems == ["order-service/mysql: binding names 'kafka', which does not provide it"]
