import os

import pytest

from cagent.catalog import load_catalog
from cagent.generate import GenerationError, generate, image_tag, to_yaml
from cagent.spec import Source

from .conftest import FIXTURES, REPO_CATALOG, spec_with

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


def test_template_command_is_emitted():
    catalog = load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")
    spec = spec_with(services=[], middleware=[{"template": "memhog"}])
    memhog = generate(spec, catalog)["services"]["memhog"]
    assert memhog["command"][:2] == ["sh", "-c"] and "httpd" in memhog["command"][2]
    assert (
        "command"
        not in generate(spec_with(services=[], middleware=[{"template": "redis"}]), catalog)[
            "services"
        ]["redis"]
    )


def test_gaps_carry_the_answers_that_would_close_them(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "ci", "ref": "main#1"},
                "publish": {"grpc": 9090},
            },
            {"name": "billing", "source": {"kind": "workdir", "ref": "/src/billing"}},
        ],
        middleware=[
            {"template": "mysq"},
            {"template": "kafka", "version": "2.0", "preset": "huge"},
            {"template": "redis", "preset": "huge"},
        ],
    )
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    gaps = {(g.kind, g.target, g.key): g for g in err.value.gaps}
    assert gaps[("unknown_template", "mysq", None)].options == ("kafka", "mysql", "redis")
    assert gaps[("unknown_template", "mysq", None)].default == "mysql"
    assert gaps[("unsupported_version", "kafka", None)].options == ("3.9.0", "4.0.0")
    assert gaps[("unsupported_version", "kafka", None)].default == "3.9.0"
    assert gaps[("unknown_preset", "redis", None)].options == ("tiny", "standard")
    assert gaps[("no_provider", "order-service/mysql", None)].options == ("mysql",)
    assert gaps[("missing_env", "order-service", "SPRING_PROFILES_ACTIVE")].options == ()
    assert gaps[("unknown_port", "order-service", "grpc")].default == "http"
    assert ("unregistered", "billing", None) in gaps
    # ci is declared by order-service, so the ci source is fine; no no_source gap.
    assert not any(k == "no_source" for k, _, _ in gaps)
    assert err.value.problems == [str(g) for g in err.value.gaps]


def test_undeclared_source_kind_offers_the_declared_ones(catalog):
    desc = catalog.services["order-service"]
    sources = desc.sources.model_copy(update={"ci": None, "workdir": None})
    catalog.services["order-service"] = desc.model_copy(update={"sources": sources})
    spec = spec_with(services=[{"name": "order-service", "source": {"kind": "ci", "ref": "x"}}])
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    gap = next(g for g in err.value.gaps if g.kind == "no_source")
    assert (gap.target, gap.options, gap.default) == (
        "order-service",
        ("release", "snapshot"),
        "release",
    )
