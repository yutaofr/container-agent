"""M7c: the generator adds one one-shot init service per middleware with init work."""

import subprocess
from pathlib import Path

import pytest

from cagent import generate as gen
from cagent.catalog import load_catalog
from cagent.generate import GenerationError, generate
from cagent.spec import ClusterSpec

from .conftest import REPO_CATALOG

APP = """name: {name}
kind: app
stack: java-springboot
sources:
  release: {{ nexus: "com.acme:{name}:{{version}}:jar" }}
runtime: {{}}
requires:
{requires}
"""

ORDERS = APP.format(
    name="orders",
    requires="""  - capability: mysql
    init: { scripts: [db/schema.sql, db/seed.sql] }
  - capability: kafka
    init: { topics: [{ name: orders-events, partitions: 3 }, { name: audit }] }""",
)
BILLING = APP.format(
    name="billing",
    requires="""  - capability: mysql
    init: { scripts: [db/billing.sql] }
  - capability: kafka
    init: { topics: [{ name: audit, partitions: 2 }] }""",
)
PLAIN = APP.format(name="plain", requires="  - capability: mysql")


def catalog_with(tmp_path: Path, *descriptors: str, extra_middleware: str | None = None):
    (tmp_path / "services").mkdir(parents=True)
    for i, text in enumerate(descriptors):
        (tmp_path / "services" / f"s{i}.yaml").write_text(text)
    if extra_middleware:
        (tmp_path / "middleware").mkdir(parents=True)
        (tmp_path / "middleware" / "fakedb.yaml").write_text(extra_middleware)
    return load_catalog(REPO_CATALOG, tmp_path)


def spec(*services: str, middleware=("mysql", "kafka")) -> ClusterSpec:
    return ClusterSpec.model_validate(
        {
            "name": "demo",
            "purpose": "test",
            "services": [
                {"name": s, "source": {"kind": "release", "ref": "1.0"}} for s in services
            ],
            "middleware": [{"template": m} for m in middleware],
        }
    )


def test_init_service_runs_after_its_middleware_and_before_its_consumers(tmp_path):
    compose = generate(
        spec("orders", "billing", "plain"), catalog_with(tmp_path, ORDERS, BILLING, PLAIN)
    )
    svcs = compose["services"]
    init = svcs["mysql-init"]
    assert init["image"] == svcs["mysql"]["image"]
    assert init["depends_on"] == {"mysql": {"condition": "service_healthy"}}
    assert init["volumes"] == [
        "./init/billing/mysql:/cagent-init/billing:ro",
        "./init/orders/mysql:/cagent-init/orders:ro",
    ]
    assert init["labels"] == {"cagent.role": "init"}
    assert init["restart"] == "no"
    assert init["mem_limit"] == "256m"
    assert svcs["kafka-init"]["mem_limit"] == "512m"
    assert "volumes" not in svcs["kafka-init"]
    assert svcs["orders"]["depends_on"] == {
        "kafka": {"condition": "service_healthy"},
        "kafka-init": {"condition": "service_completed_successfully"},
        "mysql": {"condition": "service_healthy"},
        "mysql-init": {"condition": "service_completed_successfully"},
    }
    assert svcs["plain"]["depends_on"] == {"mysql": {"condition": "service_healthy"}}


def test_topics_are_created_once_with_the_most_partitions_asked(tmp_path):
    compose = generate(spec("orders", "billing"), catalog_with(tmp_path, ORDERS, BILLING))
    script = compose["services"]["kafka-init"]["entrypoint"][2]
    assert script.count("--topic audit ") == 1
    assert "--topic audit --partitions 2 " in script
    assert "--topic orders-events --partitions 3 " in script
    assert "--bootstrap-server kafka:9092" in script


def test_dollars_reach_the_shell_escaped(tmp_path):
    script = generate(spec("orders"), catalog_with(tmp_path, ORDERS))["services"]["mysql-init"][
        "entrypoint"
    ][2]
    assert "$$sha" in script and "$$file" in script
    assert "$sha" not in script.replace("$$", "")


@pytest.mark.parametrize(
    ("requires", "middleware", "match"),
    [
        (
            "  - capability: redis\n    init: { topics: [{ name: t }] }",
            ("redis",),
            "redis cannot create topics",
        ),
        (
            "  - capability: mysql\n    init: { scripts: [schema.cql] }",
            ("mysql",),
            "schema.cql: mysql runs .sql scripts",
        ),
        (
            "  - capability: mysql\n    init: { keyspaces: [k] }",
            ("mysql",),
            "mysql cannot create keyspaces",
        ),
    ],
)
def test_init_a_template_cannot_do_is_a_gap(tmp_path, requires, middleware, match):
    cat = catalog_with(tmp_path, APP.format(name="orders", requires=requires))
    with pytest.raises(GenerationError, match=match) as err:
        generate(spec("orders", middleware=middleware), cat)
    assert {g.kind for g in err.value.gaps} == {"unsupported_init"}


def test_init_service_name_must_be_free(tmp_path):
    taken = APP.format(name="mysql-init", requires="  - capability: kafka")
    cat = catalog_with(tmp_path, ORDERS, taken)
    with pytest.raises(GenerationError, match="mysql-init: name taken"):
        generate(spec("orders", "mysql-init"), cat)


# A template whose commands only write a log, so the generated loop can run in sh here.
FAKEDB = """name: fakedb
kind: middleware
provides: { capability: mysql }
image: "busybox:{version}"
versions: ["1.37"]
default_version: "1.37"
port: 3306
connection: { log: LOG }
healthcheck: { cmd: ["CMD", "true"] }
presets: { tiny: { mem_limit: 64m, cpus: 0.1 } }
init:
  keyspaces: { run: 'echo "keyspace {{keyspace}}" >> {{log}}' }
  scripts:
    suffix: .sql
    run: 'echo "run $(basename "$file") $(cat "$file")" >> {{log}}'
    marked: 'grep -q "^mark $sha" {{log}}'
    mark: 'echo "mark $sha" >> {{log}}'
"""


def test_generated_loop_runs_each_script_once(tmp_path, monkeypatch):
    log, root = tmp_path / "log", tmp_path / "init"
    monkeypatch.setattr(gen, "INIT_ROOT", str(root))
    a = APP.format(
        name="aaa",
        requires="  - capability: mysql\n    init: { keyspaces: [ks], scripts: [x.sql, y.sql] }",
    )
    b = APP.format(name="bbb", requires="  - capability: mysql\n    init: { scripts: [z.sql] }")
    cat = catalog_with(tmp_path / "cat", a, b, extra_middleware=FAKEDB.replace("LOG", str(log)))
    compose = generate(spec("bbb", "aaa", middleware=("fakedb",)), cat)
    # What build would stage: <service>/NN-<file>.
    for rel, body in {
        "aaa/01-x.sql": "one",
        "aaa/02-y.sql": "two",
        "bbb/01-z.sql": "three",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    script = compose["services"]["fakedb-init"]["entrypoint"][2].replace("$$", "$")

    first = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    lines = log.read_text().splitlines()
    assert [ln for ln in lines if not ln.startswith("mark ")] == [
        "keyspace ks",
        "run 01-x.sql one",
        "run 02-y.sql two",
        "run 01-z.sql three",
    ]
    second = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert log.read_text().splitlines()[len(lines) :] == ["keyspace ks"]
    assert second.stdout.count("skip ") == 3


def test_generated_loop_stops_at_a_failing_script(tmp_path, monkeypatch):
    log, root = tmp_path / "log", tmp_path / "init"
    monkeypatch.setattr(gen, "INIT_ROOT", str(root))
    a = APP.format(
        name="aaa", requires="  - capability: mysql\n    init: { scripts: [x.sql, y.sql] }"
    )
    failing = FAKEDB.replace("LOG", str(log)).replace(
        "run: 'echo \"run", 'run: \'grep -qv boom "$file" && echo "run'
    )
    cat = catalog_with(tmp_path / "cat", a, extra_middleware=failing)
    compose = generate(spec("aaa", middleware=("fakedb",)), cat)
    (root / "aaa").mkdir(parents=True)
    (root / "aaa/01-x.sql").write_text("boom")
    (root / "aaa/02-y.sql").write_text("fine")
    script = compose["services"]["fakedb-init"]["entrypoint"][2].replace("$$", "$")
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode != 0
    assert f"cagent init failed: {root}/aaa/01-x.sql" in result.stderr
    assert not log.exists() or "mark" not in log.read_text()
