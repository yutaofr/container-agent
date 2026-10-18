"""M7c: init resources in descriptors and middleware templates."""

import pytest
import yaml
from pydantic import ValidationError

from cagent.catalog import MAX_SCRIPTS, MiddlewareTemplate, Requirement

from .conftest import REPO_CATALOG


def test_requirement_declares_scripts_keyspaces_and_topics():
    req = Requirement.model_validate(
        {
            "capability": "cassandra",
            "init": {
                "keyspaces": ["orders"],
                "scripts": ["db/cassandra/schema.cql"],
                "topics": [{"name": "orders-events", "partitions": 3}, {"name": "audit.v1"}],
            },
        }
    )
    assert req.init.keyspaces == ["orders"]
    assert [(t.name, t.partitions) for t in req.init.topics] == [
        ("orders-events", 3),
        ("audit.v1", 1),
    ]


@pytest.mark.parametrize(
    "init",
    [
        {"scripts": ["../shared/schema.sql"]},
        {"scripts": ["/etc/schema.sql"]},
        {"scripts": [""]},
        {"scripts": [f"s{i}.sql" for i in range(MAX_SCRIPTS + 1)]},
        {"topics": [{"name": "orders events"}]},
        {"topics": [{"name": "a;rm -rf /"}]},
        {"topics": [{"name": "orders", "partitions": 0}]},
        {"keyspaces": ["1orders"]},
        {"keyspaces": ["orders-v1"]},
        {"keyspaces": ["k" * 49]},
    ],
)
def test_requirement_init_rejects_unsafe_or_oversized_values(init):
    with pytest.raises(ValidationError):
        Requirement.model_validate({"capability": "mysql", "init": init})


def template(**init) -> dict:
    doc = yaml.safe_load((REPO_CATALOG / "middleware" / "mysql.yaml").read_text())
    doc["init"] = init
    return doc


def test_shipped_templates_declare_their_init_kinds(catalog):
    assert catalog.middleware["mysql"].init.scripts.suffix == ".sql"
    assert catalog.middleware["kafka"].init.topics is not None
    cassandra = catalog.middleware["cassandra"]
    assert cassandra.init.scripts.suffix == ".cql"
    assert cassandra.init.keyspaces is not None
    assert catalog.middleware["redis"].init is None


@pytest.mark.parametrize(
    ("init", "match"),
    [
        ({"topics": {"run": "kafka-topics --topic {{topic}} --broker {{brokers}}"}}, "brokers"),
        ({"keyspaces": {"run": "cqlsh -e 'CREATE KEYSPACE {{topic}}'"}}, "topic"),
        (
            {"scripts": {"suffix": ".sql", "run": "mysql {{keyspace}}", "marked": "", "mark": ""}},
            "keyspace",
        ),
        ({"scripts": {"suffix": "sql", "run": "x", "marked": "x", "mark": "y"}}, "suffix"),
    ],
)
def test_template_init_commands_use_only_known_placeholders(init, match):
    with pytest.raises(ValidationError, match=match):
        MiddlewareTemplate.model_validate(template(**init))


def test_template_init_may_use_connection_values():
    tpl = MiddlewareTemplate.model_validate(
        template(
            scripts={
                "suffix": ".sql",
                "run": 'mysql -h{{host}} -P{{port}} {{db}} < "$file"',
                "marked": "true",
                "mark": "true",
            }
        )
    )
    assert tpl.init.mem_limit == "256m"
