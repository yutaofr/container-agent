import pytest
from pydantic import ValidationError

from cagent.catalog import load_catalog
from cagent.diagnose import (
    PatchOp,
    declared_env,
    is_cross_service,
    judge,
    mask_secrets,
    prompt,
    prompt_view,
    propose,
)
from cagent.llm import Router
from cagent.rules import Problem

from .conftest import FIXTURES, REPO_CATALOG, spec_with
from .fakellm import CONFIG, FakeCompleter

DECLARED = {"web": {"APP_MODE", "JAVA_OPTS"}}
WEB = {
    "services": {
        "web": {
            "image": "web:1",
            "mem_limit": "512m",
            "environment": {"APP_MODE": "a", "JAVA_OPTS": "-Xmx256m", "OLD_KEY": "x"},
        }
    }
}


def verdict_of(patch, declared=DECLARED, before=WEB):
    return judge(patch, declared, frozenset(), before)


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
    assert verdict_of(patch)[0] == verdict


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
    assert verdict_of(
        [op("replace", "/services/web/environment/JAVA_OPTS", value)], JVM_DECLARED
    ) == (
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
        # add, not replace: WEB holds only JAVA_OPTS, and a replace of a missing key does
        # not apply
        verdict, reasons = verdict_of(
            [op("add", f"/services/web/environment/{key}", value)], JVM_DECLARED
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
    assert verdict_of(patch, JVM_DECLARED)[0] == "approve"


def test_judge_leaves_ordinary_values_alone():
    patch = [op("replace", "/services/web/environment/APP_MODE", "@home -Xdebug")]
    assert verdict_of(patch, JVM_DECLARED) == ("apply", [])


def test_judge_explains_approvals():
    _, reasons = verdict_of([op("add", "/services/web/environment/FEATURE_FLAG", "on")], DECLARED)
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


MYSQL_COMPOSE = {
    "services": {
        "mysql": {
            "image": "mysql:8.4",
            "environment": {"MYSQL_DATABASE": "app", "MYSQL_PASSWORD": "app"},
            "healthcheck": {
                "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -papp --silent"]
            },
        },
        "mysql-init": {
            "entrypoint": ["sh", "-c", 'set -eu\nmysql -hmysql -uroot -proot app < "$$file"'],
        },
        "orders": {"environment": {"SPRING_DATASOURCE_PASSWORD": "long-secret-1"}},
    }
}


def test_prompts_hold_no_credential_and_keep_diagnostics():
    text = prompt(
        Problem("auth:denied", "orders"),
        {"state": "exited"},
        ["Access denied: long-secret-1 rejected", "ran mysql -uroot -proot"],
        MYSQL_COMPOSE,
        {"orders": {"SPRING_DATASOURCE_PASSWORD"}},
        cross=True,
    )
    for secret in ("-papp", "-proot", "long-secret-1"):
        assert secret not in text
    for kept in ("-uapp", "-uroot", "MYSQL_DATABASE: app", '< "$file"'):
        assert kept in text


async def test_the_root_cause_is_masked_before_anything_uses_it():
    fake = FakeCompleter(
        {
            "fake/large": [
                {"root_cause": "mysql -uroot -proot fails; long-secret-1 is stale", "patch": []}
            ]
        }
    )
    snapshot = {"services": [{"service": "orders", "error_lines": ["Access denied"]}]}
    proposal = await propose(
        Router(CONFIG, fake), Problem("auth:denied", "orders"), snapshot, MYSQL_COMPOSE, {}, 0
    )
    assert proposal.root_cause == "mysql -uroot -p****** fails; ****** is stale"
    assert "long-secret-1" not in fake.calls[0][1]


SECRETS_BEFORE = {
    "services": {
        "db": {"image": "mysql:8.4", "environment": {"MYSQL_PASSWORD": "app", "MYSQL_USER": "app"}},
        "app": {
            "image": "app:1",
            "command": ["run"],
            "environment": ["DB_PASSWORD=pw-value", "APP_MODE=a"],
        },
    }
}
SECRETS_DECLARED = {"db": {"MYSQL_PASSWORD", "MYSQL_USER"}, "app": {"DB_PASSWORD", "APP_MODE"}}


def credentials_of(patch, before=SECRETS_BEFORE):
    return judge(patch, SECRETS_DECLARED, frozenset(), before)


@pytest.mark.parametrize(
    "patch, fields",
    [
        ([op("replace", "/services/db/environment/MYSQL_PASSWORD", "x")], ["db: MYSQL_PASSWORD"]),
        ([op("remove", "/services/db/environment/MYSQL_PASSWORD")], ["db: MYSQL_PASSWORD"]),
        ([op("add", "/services/db/environment/ROOT_PASSWORD", "x")], ["db: ROOT_PASSWORD"]),
        # list entries: the old key and the new key both count
        ([op("replace", "/services/app/environment/0", "APP_MODE=test")], ["app: DB_PASSWORD"]),
        ([op("replace", "/services/app/environment/1", "API_TOKEN=x")], ["app: API_TOKEN"]),
        ([op("remove", "/services/app/environment/0")], ["app: DB_PASSWORD"]),
        ([op("add", "/services/app/environment/-", "X_SECRET=1")], ["app: X_SECRET"]),
        # a list-form entry written into a mapping still names its key
        ([op("add", "/services/db/environment/0", "TOKEN=x")], ["db: TOKEN"]),
        # the whole environment, when the old or the new one holds a secret key
        (
            [op("replace", "/services/db/environment", {"MYSQL_USER": "app"})],
            ["db: MYSQL_PASSWORD"],
        ),
        (
            [
                op(
                    "replace",
                    "/services/db/environment",
                    {"MYSQL_USER": "a", "MYSQL_PASSWORD": "******"},
                )
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        ([op("remove", "/services/db/environment")], ["db: MYSQL_PASSWORD"]),
        ([op("replace", "/services/db", {"image": "mysql:8.4"})], ["db: MYSQL_PASSWORD"]),
        # credentials win over the gate: the command change does not make it a reject
        (
            [
                op("replace", "/services/app/command", ["evil"]),
                op("replace", "/services/db/environment/MYSQL_PASSWORD", "x"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
    ],
)
def test_a_patch_that_touches_a_credential_escalates_whole(patch, fields):
    assert credentials_of(patch) == ("credentials", fields)


def test_indexes_resolve_on_the_state_each_operation_sees():
    before = {"services": {"app": {"environment": ["APP_MODE=a", "DB_PASSWORD=pw-value"]}}}
    patch = [
        op("remove", "/services/app/environment/0"),
        op("replace", "/services/app/environment/0", "APP_MODE=b"),
    ]
    assert credentials_of(patch, before) == ("credentials", ["app: DB_PASSWORD"])


def test_ordinary_environment_changes_are_judged_as_before():
    assert credentials_of([op("replace", "/services/app/environment/1", "APP_MODE=b")]) == (
        "apply",
        [],
    )
    before = {"services": {"web": {"environment": {"APP_MODE": "a"}}}}
    patch = [op("replace", "/services/web/environment", {"APP_MODE": "b"})]
    assert judge(patch, {"web": {"APP_MODE"}}, frozenset(), before) == ("apply", [])


@pytest.mark.parametrize(
    "patch, fields",
    [
        # found before the failing operation: kept
        (
            [
                op("replace", "/services/db/environment/MYSQL_PASSWORD", "x"),
                op("replace", "/services/nope/mem_limit", "1g"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        # after the failure, judged by path and value
        (
            [
                op("replace", "/services/nope/mem_limit", "1g"),
                op("remove", "/services/db/environment/MYSQL_PASSWORD"),
            ],
            ["db: MYSQL_PASSWORD"],
        ),
        # an index that cannot be resolved, on an environment that holds a secret key
        (
            [
                op("replace", "/services/nope/mem_limit", "1g"),
                op("replace", "/services/app/environment/7", "A=1"),
            ],
            ["app: environment"],
        ),
    ],
)
def test_a_patch_that_does_not_apply_still_escalates_its_credentials(patch, fields):
    assert credentials_of(patch) == ("credentials", fields)


def test_a_patch_that_does_not_apply_is_never_applied():
    assert credentials_of([op("replace", "/services/db/mem_limit", "1g")]) == (
        "reject",
        ["patch does not apply"],
    )


def test_credential_reasons_name_fields_only():
    _, reasons = credentials_of(
        [op("replace", "/services/app/environment/0", "DB_PASSWORD=new-pw-1")]
    )
    text = " ".join(reasons)
    assert "pw-value" not in text and "new-pw-1" not in text


def test_a_model_cannot_send_test_operations():
    with pytest.raises(ValidationError):
        PatchOp(op="test", path="/services/db/environment/MYSQL_PASSWORD", value="app")
