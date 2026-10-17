from pathlib import Path

import pytest

from cagent.catalog import Catalog, load_catalog
from cagent.spec import ClusterSpec, load_spec

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
REPO_CATALOG = REPO / "catalog"
FIXTURE_CATALOG = FIXTURES / "catalog"


@pytest.fixture
def catalog() -> Catalog:
    """Shipped middleware templates plus the sample order-service descriptor."""
    return load_catalog(REPO_CATALOG, FIXTURE_CATALOG)


@pytest.fixture
def orders_spec() -> ClusterSpec:
    return load_spec(FIXTURES / "orders-poc.spec.yaml")


def spec_with(**overrides) -> ClusterSpec:
    """The orders-poc spec with top-level fields replaced, for focused tests."""
    base = load_spec(FIXTURES / "orders-poc.spec.yaml").model_dump()
    return ClusterSpec.model_validate({**base, **overrides})
