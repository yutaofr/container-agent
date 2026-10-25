import pytest
import yaml

from cagent.catalog import ServiceDescriptor
from cagent.draft import (
    DraftError,
    draft,
    env_placeholders,
    pick,
    read_back,
    skeletons,
)
from cagent.llm import ModelUnavailable, Router

from .fakellm import CONFIG, FakeCompleter

BOOT_POM = (
    "<project xmlns='http://maven.apache.org/POM/4.0.0'><artifactId>{name}</artifactId>"
    "<build><plugins><plugin><groupId>org.springframework.boot</groupId>"
    "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>"
)
PLAIN_POM = "<project><artifactId>{name}</artifactId></project>"
AGGREGATOR = (
    "<project><artifactId>shop</artifactId><packaging>pom</packaging>"
    "<modules><module>api</module><module>lib</module><module>worker</module></modules>"
    "<build><pluginManagement><plugins><plugin>"
    "<artifactId>spring-boot-maven-plugin</artifactId>"
    "</plugin></plugins></pluginManagement></build></project>"
)
APP_YML = (
    "server:\n  port: 8081\nspring:\n  datasource:\n    url: ${DB_URL}\n"
    "    password: ${DB_PASSWORD}\n  kafka:\n    bootstrap-servers: ${KAFKA:localhost:9092}\n"
    "app:\n  name: ${spring.application.name:orders}\n"
)


def inspection(files, modules=(), resources=(), truncated=()):
    return {
        "modules": list(modules),
        "files": files,
        "resources": list(resources),
        "truncated": list(truncated),
    }


SHOP = inspection(
    {
        "pom.xml": AGGREGATOR,
        "api/pom.xml": BOOT_POM.format(name="orders-api"),
        "api/src/main/resources/application.yml": APP_YML,
        "lib/pom.xml": PLAIN_POM.format(name="lib"),
        "worker/pom.xml": PLAIN_POM.format(name="worker"),
        "worker/Dockerfile": "FROM eclipse-temurin:21-jre\nCOPY target/worker.jar /app.jar\n",
    },
    modules=["api", "lib", "worker"],
    resources=["api/src/main/resources/db/migration/V1__init.sql", "cql/keyspace.cql"],
)


def test_single_spring_boot_project():
    [sk] = skeletons(inspection({"pom.xml": BOOT_POM.format(name="orders")}))
    assert (sk.name, sk.module, sk.stack) == ("orders", ".", "java-springboot")
    assert sk.workdir == {"build": "mvn -q package -DskipTests", "output": "target/*.jar"}


def test_maven_modules_skip_the_aggregator_and_libraries():
    api, worker = skeletons(SHOP)
    assert (api.name, api.stack, api.workdir) == (
        "orders-api",
        "java-springboot",
        {"build": "mvn -q -pl api -am package -DskipTests", "output": "api/target/*.jar"},
    )
    # The worker's Dockerfile copies a jar from target/, so the module is built first.
    assert (worker.name, worker.stack, worker.workdir) == (
        "worker",
        "dockerfile",
        {
            "build": "mvn -q -pl worker -am package -DskipTests",
            "dockerfile": "worker/Dockerfile",
            "context": "worker",
        },
    )


def test_gradle_modules_and_a_self_contained_dockerfile():
    files = {
        "settings.gradle.kts": 'include("web", ":lib:core", "ui")',
        "web/build.gradle.kts": 'plugins { id("org.springframework.boot") version "3.3.0" }',
        "lib/core/build.gradle.kts": "plugins { `java-library` }",
        "ui/package.json": "{}",
        "ui/Dockerfile": "FROM node:22\nCOPY . .\nRUN npm ci\n",
    }
    web, ui = skeletons(inspection(files, modules=["web", "lib/core", "ui"]))
    assert web.workdir == {"build": "./gradlew -q :web:bootJar", "output": "web/build/libs/*.jar"}
    assert (ui.stack, ui.workdir) == (
        "dockerfile",
        {"dockerfile": "ui/Dockerfile", "context": "ui"},
    )


def test_pick_matches_the_service_to_a_module():
    sks = skeletons(SHOP)
    assert pick(sks, "orders-api").module == "api"
    assert pick(sks, "orders_api").module == "api"  # close enough
    with pytest.raises(
        DraftError, match="no deployable module matches billing: orders-api, worker"
    ):
        pick(sks, "billing")
    single = skeletons(inspection({"Dockerfile": "FROM busybox"}))
    assert pick(single, "anything").module == "."
    with pytest.raises(DraftError, match="add a Dockerfile"):
        pick(skeletons(inspection({"package.json": "{}"})), "ui")


def test_env_placeholders_are_upper_case_variables():
    assert env_placeholders(APP_YML) == (["DB_PASSWORD", "DB_URL"], ["KAFKA"])


def answer(**overrides):
    return {
        "ports": [{"name": "http", "container": 8081}],
        "health_path": "/actuator/health",
        "requires": [
            {
                "capability": "mysql",
                "bind": {
                    "DB_URL": "jdbc:mysql://{{host}}:{{port}}/{{db}}",
                    "DB_PASSWORD": "{{password}}",
                },
            },
            {"capability": "kafka", "bind": {"KAFKA": "{{host}}:{{port}}"}},
        ],
        "probe_paths": ["/actuator/health"],
        **overrides,
    }


async def test_draft_combines_the_skeleton_and_the_models_runtime(catalog):
    fake = FakeCompleter({"fake/large": [answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.path == ".cagent/services/orders-api.yaml"
    assert d.descriptor.name == "orders-api" and d.descriptor.stack == "java-springboot"
    assert d.descriptor.sources.workdir.build == "mvn -q -pl api -am package -DskipTests"
    assert d.descriptor.requires[0].bind["DB_PASSWORD"] == "{{password}}"
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
    assert d.descriptor.probes[0].http.path == "/actuator/health"
    assert ServiceDescriptor.model_validate(yaml.safe_load(d.text)) == d.descriptor
    assert d.text.startswith("# Drafted by cagent")
    assert fake.thinks == [False]
    prompt = fake.calls[0][1]
    assert "--- api/src/main/resources/application.yml" in prompt
    assert "worker/pom.xml" not in prompt and "lib/pom.xml" not in prompt
    assert "without a default (set each one): DB_PASSWORD, DB_URL" in prompt
    assert "- mysql: {{host}}, {{port}}, {{db}}, {{password}}, {{user}}" in prompt


@pytest.mark.parametrize(
    "bad, why",
    [
        (answer(requires=[{"capability": "postgres"}]), "no template provides postgres"),
        (
            answer(requires=[{"capability": "mysql", "bind": {"DB_URL": "{{database}}"}}]),
            "mysql bind DB_URL uses unknown {{database}}",
        ),
        (answer(requires=[]), "DB_PASSWORD, DB_URL"),
        # Seen from qwen3.5: bind keyed by the template's values instead of the variables.
        (
            answer(requires=[{"capability": "mysql", "bind": {"host": "{{host}}"}}]),
            "bind keys are environment variables the service reads, such as DB_URL; host is not",
        ),
        (
            answer(env_defaults={"CACHE": "127.0.0.1:6379"}),
            "CACHE=127.0.0.1:6379 points at the container itself",
        ),
    ],
)
async def test_an_invalid_draft_is_retried_with_the_reason(catalog, bad, why):
    fake = FakeCompleter({"fake/large": [bad, answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.requires and len(fake.calls) == 2
    assert why in fake.calls[1][1]


async def test_a_bound_variable_drops_its_default_and_required_entry(catalog):
    copied = answer(env_defaults={"KAFKA": "localhost:9092"}, env_required=["DB_URL", "MODE"])
    fake = FakeCompleter({"fake/large": [copied]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.runtime.env.defaults == {} and d.descriptor.runtime.env.required == ["MODE"]
    assert len(fake.calls) == 1


async def test_a_path_wins_over_a_command_healthcheck(catalog):
    both = answer(health_cmd=["CMD", "curl", "-f", "http://localhost:8081/actuator/health"])
    d = await draft(
        Router(CONFIG, FakeCompleter({"fake/large": [both]})), "orders-api", SHOP, catalog
    )
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
    assert d.descriptor.runtime.healthcheck.cmd is None


async def test_a_descriptor_rule_broken_by_the_answer_is_retried(catalog):
    no_ports = answer(ports=[])  # an HTTP healthcheck needs a port
    fake = FakeCompleter({"fake/large": [no_ports, answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.runtime.ports and "needs at least one port" in fake.calls[1][1]


async def test_draft_gives_up_after_two_invalid_answers(catalog):
    bad = answer(requires=[{"capability": "postgres"}])
    fake = FakeCompleter({"fake/large": [bad, bad]})
    with pytest.raises(ModelUnavailable, match="no template provides postgres"):
        await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)


def saved(path, text):
    return inspection({path: text})


DOCKERFILE_DESC = (
    "name: ui\nkind: app\nstack: dockerfile\nsources:\n  workdir: { dockerfile: Dockerfile }\n"
    "runtime: {}\n"
)


RELEASE_ONLY = (
    "name: ui\nkind: app\nstack: java-springboot\n"
    "sources:\n  release: { nexus: 'a:b:{version}:jar' }\nruntime: {}\n"
)


def test_read_back_validates_the_file_on_disk():
    desc = read_back("ui", saved(".cagent/services/ui.yaml", DOCKERFILE_DESC))
    assert desc.stack == "dockerfile"
    # The single-service file is the fallback, as for the Runner.
    assert read_back("ui", saved(".cagent/service.yaml", DOCKERFILE_DESC)).name == "ui"


@pytest.mark.parametrize(
    "files, why",
    [
        ({}, "not saved yet"),
        ({".cagent/services/ui.yaml": "name: [unclosed"}, "not valid YAML"),
        ({".cagent/services/ui.yaml": DOCKERFILE_DESC.replace("ui", "web")}, "names web, not ui"),
        ({".cagent/services/ui.yaml": "name: ui\nkind: app\n"}, "Field required"),
        (
            {
                ".cagent/services/ui.yaml": DOCKERFILE_DESC.replace(
                    "runtime: {}", "runtime:\n  env: { defaults: { API_TOKEN: '******' } }"
                )
            },
            "masked secret",
        ),
        ({".cagent/services/ui.yaml": RELEASE_ONLY}, "declares no workdir source"),
    ],
)
def test_read_back_refuses_what_cannot_be_used(files, why):
    with pytest.raises(DraftError, match=why):
        read_back("ui", inspection(files))


async def test_draft_feedback_never_echoes_the_answer(catalog):
    bad = answer(probe_paths=["/x;sekrit-value-1"])
    fake = FakeCompleter({"fake/large": [bad, answer()]})
    await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    feedback = fake.calls[1][1].split("Your previous answer was invalid:")[1]
    assert "sekrit-value-1" not in feedback and "path: an http path is" in feedback


@pytest.mark.parametrize(
    "text",
    [
        "name: [unclosed sekrit-value-1",
        DOCKERFILE_DESC.replace("kind: app", "kind: sekrit-value-1"),
    ],
)
def test_read_back_errors_never_echo_the_file(text):
    with pytest.raises(DraftError) as e:
        read_back("ui", inspection({".cagent/services/ui.yaml": text}))
    assert "sekrit-value-1" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
