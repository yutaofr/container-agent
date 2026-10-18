from cagent.matching import match

from .conftest import spec_with


def providers(bindings):
    return {(b.consumer, b.requirement.capability): b.provider for b in bindings}


def test_each_requirement_binds_to_its_provider(catalog, orders_spec):
    bindings, gaps = match(orders_spec, catalog)
    assert gaps == []
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
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == ["order-service/mysql: needs version >=8.4; mysql is 8.0"]
    assert (gaps[0].kind, gaps[0].key, gaps[0].options, gaps[0].default) == (
        "version_mismatch",
        "mysql",
        ("8.4",),
        "8.4",
    )


def test_missing_provider_is_reported(catalog):
    _, gaps = match(spec_with(middleware=[{"template": "mysql"}]), catalog)
    assert [str(g) for g in gaps] == [
        "order-service/kafka: no middleware in the spec provides kafka"
    ]
    assert (gaps[0].kind, gaps[0].options, gaps[0].default) == ("no_provider", ("kafka",), "kafka")


def test_two_providers_are_ambiguous(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ]
    )
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == [
        "order-service/mysql: ambiguous, candidates mysql, mysql-b; "
        "set bindings['order-service/mysql']"
    ]
    assert (gaps[0].kind, gaps[0].options, gaps[0].default) == (
        "binding",
        ("mysql", "mysql-b"),
        "mysql",
    )


def test_explicit_binding_resolves_ambiguity(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ],
        bindings={"order-service/mysql": "mysql-b"},
    )
    bindings, gaps = match(spec, catalog)
    assert gaps == []
    assert providers(bindings)[("order-service", "mysql")] == "mysql-b"


def test_binding_to_wrong_capability_is_reported(catalog):
    spec = spec_with(bindings={"order-service/mysql": "kafka"})
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == [
        "order-service/mysql: binding names 'kafka', which does not provide it"
    ]
    assert (gaps[0].kind, gaps[0].options) == ("binding", ("mysql",))
