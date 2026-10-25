from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cagent.catalog import CatalogError, Healthcheck, HttpProbe, ServiceDescriptor, load_catalog

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
    with pytest.raises(
        CatalogError, match="a placeholder is not host, port, or a connection value"
    ):
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


def test_kafka_healthcheck_starts_no_jvm(catalog):
    """Each /opt/kafka/bin tool starts a JVM: at the template's 0.5 CPU one took 2.7-6.6 s,
    past the 3 s healthcheck timeout, so Kafka never turned healthy on CI."""
    cmd = " ".join(catalog.middleware["kafka"].healthcheck.cmd)
    assert "/opt/kafka/bin/" not in cmd


def test_init_env_names_keys_of_the_template_env(tmp_path):
    write_template(
        tmp_path, "mysql", MYSQL.replace("[MYSQL_ROOT_PASSWORD]", "[MYSQL_ROOT_PASSWORD, NOPE]")
    )
    with pytest.raises(CatalogError, match="init.env"):
        load_catalog(tmp_path)


@pytest.mark.parametrize("path", ["/", "/health", "/check?a=1&b=2", "/a%20b/c.d_e~f"])
def test_http_paths_that_are_plain_paths_load(path):
    Healthcheck(http=path)
    HttpProbe(path=path)


@pytest.mark.parametrize(
    "path",
    ["health", "/x'y", '/x"y', "/x\n", "/x y", "/$(id)", "/x`id`", "/x;id", "/x|y", "/a\\b"],
)
def test_http_paths_with_anything_else_are_refused(path):
    with pytest.raises(ValidationError):
        Healthcheck(http=path)
    with pytest.raises(ValidationError):
        HttpProbe(path=path)


SECRET = "sekrit-value-1"


@pytest.mark.parametrize(
    "text",
    [
        MYSQL.replace("port: 3306", f"port: {SECRET}"),
        MYSQL.replace('default_version: "8.4"', f'default_version: "{SECRET}"'),
        MYSQL.replace('"{{db}}"', '"{{sekritvalue1}}"'),
        MYSQL + f"\nbroken: [{SECRET}\n",
    ],
    ids=["builtin-type", "own-validator", "placeholder-name", "yaml"],
)
def test_catalog_errors_never_echo_the_input(tmp_path, text):
    write_template(tmp_path, "mysql", text)
    with pytest.raises(CatalogError) as e:
        load_catalog(tmp_path)
    message = str(e.value)
    assert SECRET not in message and "sekritvalue1" not in message
    assert message.startswith(str(tmp_path / "middleware" / "mysql.yaml"))
    assert e.value.__cause__ is None and e.value.__suppress_context__
