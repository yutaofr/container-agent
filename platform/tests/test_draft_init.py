"""M7c: drafts declare init scripts by code and topics from the model, checked by code."""

from cagent.draft import draft
from cagent.llm import Router

from .fakellm import CONFIG, FakeCompleter
from .test_draft import AGGREGATOR, APP_YML, BOOT_POM, PLAIN_POM, answer, inspection

# M7b's configuration plus one topic, named by a placeholder with a default.
YML = APP_YML + "  kafka:\n    topic:\n      orders: ${ORDERS_TOPIC:orders-events}\n"
FILES = {
    "pom.xml": AGGREGATOR,
    "api/pom.xml": BOOT_POM.format(name="orders-api"),
    "api/src/main/resources/application.yml": YML,
    "lib/pom.xml": PLAIN_POM.format(name="lib"),
    "worker/pom.xml": PLAIN_POM.format(name="worker"),
}
RESOURCES = [
    "api/src/main/resources/db/init/02-seed.sql",
    "api/src/main/resources/db/init/01-schema.sql",
    "api/src/main/resources/db/migration/V1__init.sql",
    "cql/keyspace.cql",
    "worker/db/init/worker.sql",
]
SHOP = inspection(FILES, modules=["api", "lib", "worker"], resources=RESOURCES)
WITH_CASSANDRA = {"capability": "cassandra", "bind": {"CASSANDRA_DC": "{{datacenter}}"}}


NO_TOPICS = {"topics": []}


async def drafted(catalog, *answers, project=SHOP):
    fake = FakeCompleter({"fake/large": list(answers)})
    return await draft(Router(CONFIG, fake), "orders-api", project, catalog), fake


def by_capability(d):
    return {r.capability: r.init for r in d.descriptor.requires}


async def test_scripts_are_mapped_by_code_in_path_order(catalog):
    d, _ = await drafted(catalog, answer(), NO_TOPICS)
    init = by_capability(d)
    assert init["mysql"].scripts == [
        "api/src/main/resources/db/init/01-schema.sql",
        "api/src/main/resources/db/init/02-seed.sql",
    ]
    assert "Flyway or Liquibase migrations run when the application starts" in d.note
    assert "cql/keyspace.cql" in d.note and "requires no capability that runs .cql" in d.note
    assert "worker" not in str(d.descriptor.model_dump())


async def test_a_cql_script_goes_to_cassandra_when_the_draft_requires_it(catalog):
    a = answer()
    a["requires"].append(WITH_CASSANDRA)
    d, _ = await drafted(catalog, a, NO_TOPICS)
    assert by_capability(d)["cassandra"].scripts == ["cql/keyspace.cql"]


async def test_scripts_spring_runs_itself_are_left_out(catalog):
    files = {
        **FILES,
        "api/src/main/resources/application.properties": "spring.sql.init.mode=always\n",
    }
    project = inspection(
        files, modules=["api", "lib", "worker"], resources=["api/src/main/resources/schema.sql"]
    )
    d, _ = await drafted(catalog, answer(), NO_TOPICS, project=project)
    assert by_capability(d)["mysql"] is None
    assert "api/src/main/resources/schema.sql" in d.note and "runs it itself" in d.note


async def test_topics_are_asked_apart_from_the_runtime(catalog):
    d, fake = await drafted(
        catalog, answer(), {"topics": [{"name": "orders-events", "partitions": 3}]}
    )
    topics = by_capability(d)["kafka"].topics
    assert [(t.name, t.partitions) for t in topics] == [("orders-events", 3)]
    runtime_prompt, topics_prompt = fake.calls[0][1], fake.calls[1][1]
    assert "topic" not in runtime_prompt.split("Capabilities you may require")[1]
    assert "app.kafka.topic.orders: ${ORDERS_TOPIC:orders-events}" in topics_prompt
    assert fake.thinks == [False, False]
    assert "Topics created in kafka: orders-events" in d.note


async def test_an_unresolved_topic_name_is_asked_again(catalog):
    bad = {"topics": [{"name": "${ORDERS_TOPIC:orders-events}"}]}
    d, fake = await drafted(catalog, answer(), bad, {"topics": [{"name": "orders-events"}]})
    assert "is not a valid Kafka topic name" in fake.calls[2][1]
    assert by_capability(d)["kafka"].topics[0].partitions == 1


async def test_a_name_still_invalid_is_left_out_and_the_draft_kept(catalog):
    bad = {"topics": [{"name": "orders events"}, {"name": "audit"}]}
    d, _ = await drafted(catalog, answer(), bad, bad)
    assert [t.name for t in by_capability(d)["kafka"].topics] == ["audit"]
    assert "Left out topic 'orders events'" in d.note


async def test_a_failed_topics_call_leaves_topics_to_the_user(catalog):
    broken = {"topics": "orders-events"}  # not the schema: the router gives up after a retry
    d, _ = await drafted(catalog, answer(), broken, broken)
    assert by_capability(d)["kafka"] is None
    assert "Topics were not drafted" in d.note


async def test_no_topics_call_without_a_capability_that_creates_topics(catalog):
    a = answer()
    a["requires"] = [r for r in a["requires"] if r["capability"] != "kafka"]
    a["env_required"] = ["KAFKA"]
    d, fake = await drafted(catalog, a)
    assert len(fake.calls) == 1 and "kafka" not in by_capability(d)
