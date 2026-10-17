import os

import pytest

from cagent.generate import GenerationError, generate, image_tag, to_yaml
from cagent.spec import Source

from .conftest import FIXTURES, spec_with

GOLDEN = FIXTURES / "orders-poc.compose.yaml"


def test_orders_poc_matches_golden(catalog, orders_spec):
    """Regenerate with UPDATE_GOLDEN=1 after an intended change, then review the diff."""
    text = to_yaml(generate(orders_spec, catalog))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text()


def test_app_service_is_wired_to_its_providers(catalog, orders_spec):
    app = generate(orders_spec, catalog)["services"]["order-service"]
    assert app["environment"]["SPRING_DATASOURCE_URL"] == "jdbc:mysql://mysql:3306/app"
    assert app["environment"]["SPRING_KAFKA_BOOTSTRAP_SERVERS"] == "kafka:9092"
    assert app["environment"]["SPRING_PROFILES_ACTIVE"] == "dev"
    assert app["depends_on"] == {
        "kafka": {"condition": "service_healthy"},
        "mysql": {"condition": "service_healthy"},
    }
    assert app["ports"] == ["18080:8080"]


def test_http_healthcheck_escapes_dollar(catalog, orders_spec):
    app = generate(orders_spec, catalog)["services"]["order-service"]
    assert "$$(curl" in app["healthcheck"]["test"][1]


def test_every_problem_is_reported_at_once(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "release", "ref": "1.4.2"},
                "publish": {"grpc": 9090},
            },
            {"name": "billing", "source": {"kind": "release", "ref": "2.0"}},
        ],
        middleware=[{"template": "mysql", "version": "5.7"}, {"template": "kafka"}],
    )
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    assert err.value.problems == [
        "mysql: mysql 5.7 unsupported; use 8.0, 8.4",
        "order-service/mysql: needs version >=8.0; mysql is 5.7",
        "order-service: missing required env SPRING_PROFILES_ACTIVE",
        "order-service: cannot publish unknown port grpc; ports are http",
        "billing: not registered in the catalog",
    ]


@pytest.mark.parametrize(
    "kind, ref, want",
    [
        ("release", "1.4.2", "cagent/svc:release-1.4.2"),
        ("snapshot", "2.0-SNAPSHOT", "cagent/svc:snapshot-2.0-SNAPSHOT"),
        ("ci", "order-service/main#123", "cagent/svc:ci-order-service-main-123"),
        ("workdir", "/Users/dev/order-service", "cagent/svc:workdir-0b8f2b1c3a0d"),
    ],
)
def test_image_tag(kind, ref, want):
    got = image_tag("svc", Source(kind=kind, ref=ref))
    if kind == "workdir":
        assert got.startswith("cagent/svc:workdir-") and len(got) == len(want)
    else:
        assert got == want
