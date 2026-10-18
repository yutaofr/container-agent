from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cagent.catalog import CatalogError, ServiceDescriptor, load_catalog

from .conftest import FIXTURE_CATALOG, REPO_CATALOG

MYSQL = (REPO_CATALOG / "middleware" / "mysql.yaml").read_text()


def write_template(root: Path, name: str, text: str) -> None:
    (root / "middleware").mkdir(parents=True, exist_ok=True)
    (root / "middleware" / f"{name}.yaml").write_text(text)


def test_shipped_catalog_loads(catalog):
    assert set(catalog.middleware) == {"mysql", "redis", "kafka", "cassandra"}
    assert set(catalog.services) == {"order-service"}
    assert catalog.middleware["kafka"].provides.capability == "kafka"
    assert catalog.services["order-service"].requires[0].version == ">=8.0"


def test_unknown_key_is_rejected(tmp_path):
    write_template(tmp_path, "mysql", MYSQL.replace("image:", "imgae:"))
    with pytest.raises(CatalogError, match="imgae"):
        load_catalog(tmp_path)


def test_duplicate_name_across_roots_is_rejected(tmp_path):
    write_template(tmp_path, "my-mysql", MYSQL)
    with pytest.raises(CatalogError, match="duplicate name 'mysql'"):
        load_catalog(REPO_CATALOG, tmp_path)


def test_template_placeholders_must_be_known(tmp_path):
    write_template(tmp_path, "mysql", MYSQL.replace('"{{db}}"', '"{{database}}"'))
    with pytest.raises(CatalogError, match="unknown placeholders: database"):
        load_catalog(tmp_path)


def test_healthcheck_needs_exactly_one_kind(tmp_path):
    bad = MYSQL.replace('  cmd: ["CMD-SHELL"', '  http: /health\n  cmd: ["CMD-SHELL"')
    write_template(tmp_path, "mysql", bad)
    with pytest.raises(CatalogError, match="exactly one of http or cmd"):
        load_catalog(tmp_path)


def test_invalid_version_specifier_is_rejected(tmp_path):
    text = (FIXTURE_CATALOG / "services" / "order-service.yaml").read_text()
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "order-service.yaml").write_text(text.replace(">=8.0", "at least 8"))
    with pytest.raises(CatalogError, match="invalid version specifier"):
        load_catalog(tmp_path)


def test_missing_root_is_rejected(tmp_path):
    with pytest.raises(CatalogError, match="not a directory"):
        load_catalog(tmp_path / "nope")


DESCRIPTORS = Path(__file__).parent / "fixtures" / "descriptors"


@pytest.mark.parametrize("path", sorted(DESCRIPTORS.glob("valid/*.yaml")), ids=lambda p: p.name)
def test_shared_valid_descriptors(path):
    ServiceDescriptor.model_validate(yaml.safe_load(path.read_text()))


# The Runner's workdir tests read the same files, so both sides agree on workdir sources.
@pytest.mark.parametrize("path", sorted(DESCRIPTORS.glob("invalid/*.yaml")), ids=lambda p: p.name)
def test_shared_invalid_descriptors(path):
    with pytest.raises(ValidationError):
        ServiceDescriptor.model_validate(yaml.safe_load(path.read_text()))


def test_dockerfile_stack_builds_only_from_a_workdir():
    doc = yaml.safe_load((DESCRIPTORS / "valid" / "dockerfile.yaml").read_text())
    doc["sources"]["release"] = {"nexus": "com.acme:demo:{version}:jar"}
    with pytest.raises(ValidationError, match="stack dockerfile builds only from a workdir"):
        ServiceDescriptor.model_validate(doc)
