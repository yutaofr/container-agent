from pathlib import Path

import pytest

from cagent.catalog import Catalog, load_catalog

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
REPO_CATALOG = REPO / "catalog"
FIXTURE_CATALOG = FIXTURES / "catalog"


@pytest.fixture
def catalog() -> Catalog:
    """Shipped middleware templates plus the sample order-service descriptor."""
    return load_catalog(REPO_CATALOG, FIXTURE_CATALOG)
