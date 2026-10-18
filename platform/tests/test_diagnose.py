import pytest

from cagent.catalog import load_catalog
from cagent.diagnose import (
    declared_env,
    is_cross_service,
    judge,
    mask_secrets,
    prompt_view,
    propose,
)
from cagent.llm import Router
from cagent.rules import Problem

from .conftest import FIXTURES, REPO_CATALOG, spec_with
from .fakellm import CONFIG, FakeCompleter

DECLARED = {"web": {"APP_MODE", "JAVA_OPTS"}}


def op(op, path, value=None):
    return {"op": op, "path": path, **({} if value is None else {"value": value})}


@pytest.mark.parametrize(
    "patch, verdict",
    [
        ([op("replace", "/services/web/environment/APP_MODE", "server")], "apply"),
        ([op("replace", "/services/web/mem_limit", "1g")], "apply"),
        ([op("remove", "/services/web/environment/OLD_KEY")], "apply"),
        ([op("add", "/services/web/environment/FEATURE_FLAG", "on")], "approve"),
        ([op("replace", "/services/web/environment", {"APP_MODE": "x", "NEW": "y"})], "approve"),
        ([op("add", "/services/web/environment/-", "NEW=1")], "approve"),
        ([op("replace", "/services/web/environment/JAVA_OPTS", "-javaagent:/x.jar")], "approve"),
        (
            [op("replace", "/services/web/environment/JAVA_OPTS", "-Xmx1g -agentpath:/a.so")],
            "approve",
        ),
        (
            [
                op(
                    "replace",
                    "/services/web/environment/JAVA_OPTS",
                    "-XX:OnOutOfMemoryError=sh -c x",
                )
            ],
            "approve",
        ),
        (
            [op("replace", "/services/web/environment/JAVA_OPTS", "-Xmx1g -XX:OnError=/x")],
            "approve",
        ),
        ([op("replace", "/services/web/environment/JAVA_OPTS", "-Xmx1g -XX:+UseG1GC")], "apply"),
        ([op("replace", "/services/web/image", "evil:1")], "reject"),
        ([op("add", "/services/web/environment/LD_PRELOAD", "/x.so")], "reject"),
    ],
)
def test_judge(patch, verdict):
    assert judge(patch, DECLARED)[0] == verdict


JVM_DECLARED = {"web": {"JAVA_OPTS", "JAVA_TOOL_OPTIONS", "KAFKA_HEAP_OPTS", "APP_MODE"}}


@pytest.mark.parametrize(
    "value",
    [
        "-Xmx512m",
        "-Xmx256m -Xms256m",
        "-Xss1m -Xmn128m",
        "-XX:+UseG1GC -XX:-UseAdaptiveSizePolicy",
        "-XX:MaxRAMPercentage=75.0 -XX:MaxMetaspaceSize=256m",
        "-Dspring.profiles.active=local,debug -Dserver.port=8080",
        "-Dfile.encoding=UTF-8 -Duser.timezone=UTC",
        "  -Xmx1g  ",
    ],
)
def test_judge_applies_tuning_options(value):
    assert judge([op("replace", "/services/web/environment/JAVA_OPTS", value)], JVM_DECLARED) == (
        "apply",
        [],
    )


@pytest.mark.parametrize(
    "value",
    [
        "-Xrunjdwp:transport=dt_socket,server=y",
        "-Xdebug",
        "-agentlib:jdwp=transport=dt_socket",
        "-Xbootclasspath/a:/tmp/x.jar",
        "-Djava.system.class.loader=evil.Loader",
        "-Dloader.path=/tmp/lib",
        "@/tmp/args",
        "-Xmx1g @argfile",
        "-cp /tmp/x.jar",
        "-XX:Flags=/tmp/.hotspotrc",
        "-XX:VMOptionsFile=/tmp/opts",
        "-XX:OnOutOfMemoryError=sh",
        "-XX:+OnError",
        "-Dx=$(id)",
        "-Dx=`id`",
        "-Xmx1g;id",
        "-Dcom.sun.management.jmxremote.port=9010",
        "-Dcom.sun.management.jmxremote.authenticate=false",
        "-Dlogging.config=http://evil/x.xml",
        "-Dlogback.configurationFile=http://evil/x.xml",
        "-Dlog4j.configurationFile=http://evil/x.xml",
        "-Dspring.config.import=http://evil/",
        "-Dspring.profiles.active=http://evil/",
        "-Dserver.port=8080/x",
        "-Dspring.profiles.active",
    ],
)
def test_judge_holds_other_jvm_options_for_approval(value):
    for key in ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "KAFKA_HEAP_OPTS"):
        verdict, reasons = judge(
            [op("replace", f"/services/web/environment/{key}", value)], JVM_DECLARED
        )
        assert verdict == "approve", (key, value)
        assert any("JVM option" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "patch",
    [
        [op("replace", "/services/web/environment", {"JAVA_OPTS": "-Xdebug"})],
        [op("replace", "/services/web/environment", ["JAVA_OPTS=-Xbootclasspath/a:/x"])],
        [op("add", "/services/web/environment/-", "JAVA_TOOL_OPTIONS=@/tmp/args")],
    ],
)
def test_judge_reads_jvm_options_in_every_environment_shape(patch):
    assert judge(patch, JVM_DECLARED)[0] == "approve"


def test_judge_leaves_ordinary_values_alone():
    patch = [op("replace", "/services/web/environment/APP_MODE", "@home -Xdebug")]
    assert judge(patch, JVM_DECLARED) == ("apply", [])


def test_judge_explains_approvals():
    _, reasons = judge([op("add", "/services/web/environment/FEATURE_FLAG", "on")], DECLARED)
    assert reasons == ["web: FEATURE_FLAG is not a declared environment key"]


def test_declared_env_comes_from_the_services_own_definitions(catalog, orders_spec):
    declared = declared_env(orders_spec, catalog)
    assert {
        "SPRING_PROFILES_ACTIVE",
        "JAVA_OPTS",
        "SPRING_DATASOURCE_URL",
        "SPRING_KAFKA_BOOTSTRAP_SERVERS",
    } <= declared["order-service"]
    assert "MYSQL_PASSWORD" in declared["mysql"]
    assert declared["redis"] == set()


def test_secret_values_are_masked_in_prompts():
    compose = {"services": {"db": {"environment": {"MYSQL_PASSWORD": "app", "MYSQL_USER": "app"}}}}
    masked = mask_secrets(compose)
    assert masked["services"]["db"]["environment"] == {
        "MYSQL_PASSWORD": "******",
        "MYSQL_USER": "app",
    }
    assert compose["services"]["db"]["environment"]["MYSQL_PASSWORD"] == "app"


@pytest.mark.parametrize(
    "kind, lines, cross",
    [
        ("exit:1", ["FATAL: APP_MODE must be server"], False),
        ("exit:1", ["nc: bad address 'rediss'"], True),  # near-miss of another service's name
        ("exit:1", ["cannot reach redis:6379"], True),
        ("conn:refused", [], True),
    ],
)
def test_cross_service_detection(kind, lines, cross):
    compose = {"services": {"app": {}, "redis": {}}}
    assert is_cross_service(Problem(kind, "app"), compose, lines) is cross


async def test_propose_starts_cross_service_problems_on_the_large_tier():
    fake = FakeCompleter(
        {
            "fake/large": [
                {
                    "root_cause": "typo",
                    "patch": [
                        {
                            "op": "replace",
                            "path": "/services/app/environment/CACHE_HOST",
                            "value": "redis",
                        }
                    ],
                }
            ]
        }
    )
    snapshot = {
        "services": [
            {
                "service": "app",
                "state": "exited",
                "exit_code": 1,
                "error_lines": ["nc: bad address 'rediss'"],
                "tail": [],
            }
        ]
    }
    compose = {"services": {"app": {"environment": {"CACHE_HOST": "rediss"}}, "redis": {}}}
    proposal = await propose(
        Router(CONFIG, fake),
        Problem("exit:1", "app"),
        snapshot,
        compose,
        {"app": {"CACHE_HOST"}},
        attempts=0,
    )
    assert (proposal.tier, proposal.verdict) == ("large", "apply")
    assert "redis:" in fake.calls[0][1]  # the whole cluster is in the prompt


async def test_propose_uses_only_the_failing_service_for_single_service_problems():
    catalog = load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")
    spec = spec_with(services=[], middleware=[{"template": "modecheck"}, {"template": "redis"}])
    fake = FakeCompleter({"fake/small": [{"root_cause": "code bug", "patch": []}]})
    snapshot = {
        "services": [
            {"service": "modecheck", "error_lines": ["FATAL: APP_MODE must be server, got srvr"]}
        ]
    }
    compose = {
        "services": {
            "modecheck": {"environment": {"APP_MODE": "srvr"}},
            "redis": {"image": "redis:7.4"},
        }
    }
    proposal = await propose(
        Router(CONFIG, fake),
        Problem("exit:1", "modecheck"),
        snapshot,
        compose,
        declared_env(spec, catalog),
        attempts=0,
    )
    assert (proposal.tier, proposal.verdict) == ("small", "give_up")
    assert "redis:7.4" not in fake.calls[0][1]


def test_prompt_view_shows_commands_as_the_container_sees_them():
    compose = {
        "services": {
            "app": {
                "command": ["sh", "-c", 'nc -z "$$CACHE_HOST" "$$CACHE_PORT"'],
                "environment": {"DB_PASSWORD": "pw", "MODE": "a$$b"},
            }
        }
    }
    view = prompt_view(compose)["services"]["app"]
    assert view["command"][2] == 'nc -z "$CACHE_HOST" "$CACHE_PORT"'
    assert view["environment"] == {"DB_PASSWORD": "******", "MODE": "a$b"}
    assert compose["services"]["app"]["command"][2].count("$$") == 2  # input untouched
