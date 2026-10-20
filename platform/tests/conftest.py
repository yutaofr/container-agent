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


@pytest.fixture
async def web(tmp_path, catalog):
    """A RunService over SimCluster behind the whole app (API and pages), for HTTP tests."""
    from cagent.api import create_app
    from cagent.auth import Sessions
    from cagent.store import Store

    from .runharness import Harness
    from .simcluster import SimCluster
    from .test_api import ORIGIN, TOKENS

    store = await Store.open(str(tmp_path / "cagent.db"))
    h = Harness(store, SimCluster(), catalog)
    h.sessions = Sessions(TOKENS, ttl_s=3600, idle_ttl_s=600)
    h.app = create_app(h.service, h.sessions, ORIGIN, secure_cookies=False)
    yield h
    await h.service.close()  # stop executions a failed test left running before closing
    await store.close()
