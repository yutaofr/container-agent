"""F1: catalog lint keeps secrets out of every command field (rules L1-L4)."""

import pytest
from pydantic import ValidationError

from cagent.catalog import (
    CatalogError,
    Healthcheck,
    HttpProbe,
    InitCommand,
    ScriptInit,
    load_catalog,
)
from cagent.secrets import safe_errors

from .conftest import REPO_CATALOG

MYSQL = (REPO_CATALOG / "middleware" / "mysql.yaml").read_text()


def rule_of(build) -> str:
    with pytest.raises(ValidationError) as e:
        build()
    [line] = safe_errors(e.value)
    return line.split(": ", 1)[1].split(":", 1)[0]


@pytest.mark.parametrize(
    "build, rule",
    [
        (lambda: Healthcheck(cmd=["CMD-SHELL", "mysqladmin ping -p{{password}}"]), "L1"),
        (lambda: Healthcheck(cmd=["CMD", "mysqladmin", "-u{{user}}", "-p{{db_pwd}}"]), "L1"),
        (lambda: Healthcheck(cmd=["CMD", "mysql", "-uroot", "-proot"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", "mysqladmin ping -uapp -papp"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", "curl http://u:pw@localhost/x"]), "L2"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'mysql -p"${MYSQL_PASSWORD:-root}"']), "L3"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'mysql -p"${DB_PWD-root}"']), "L3"),
        (lambda: Healthcheck(cmd=["CMD-SHELL", 'echo "$(mysql -proot)"']), "L4"),
        (lambda: Healthcheck(http="/health?token=abc"), "L2"),
        (lambda: HttpProbe(path="/ping?api_key=k"), "L2"),
        (lambda: InitCommand(run="cqlsh -u cassandra --password cassandra"), "L2"),
        (
            lambda: ScriptInit(
                suffix=".sql", run='mysql < "$file"', marked="true", mark="mysql -uroot -proot"
            ),
            "L2",
        ),
    ],
)
def test_command_fields_refuse_secrets(build, rule):
    assert rule_of(build) == rule


@pytest.mark.parametrize(
    "build",
    [
        lambda: Healthcheck(cmd=["CMD-SHELL", 'mysqladmin ping -u{{user}} -p"$MYSQL_PASSWORD"']),
        lambda: Healthcheck(cmd=["CMD", "tool", '--password="$DB_PASSWORD"']),
        lambda: Healthcheck(cmd=["CMD-SHELL", "redis-cli -p 6379 ping"]),
        lambda: Healthcheck(http="/check?a=1&b=2"),
        lambda: InitCommand(run='echo "run $(basename "$file")" >> {{log}}'),
    ],
)
def test_references_and_ordinary_commands_pass(build):
    build()


def test_the_template_command_is_linted_and_the_message_holds_no_value(tmp_path):
    (tmp_path / "middleware").mkdir()
    bad = MYSQL.replace("port: 3306", 'port: 3306\ncommand: ["mysqld", "--password=sekrit-pw"]')
    (tmp_path / "middleware" / "mysql.yaml").write_text(bad)
    with pytest.raises(CatalogError) as e:
        load_catalog(tmp_path)
    assert "command: L2:" in str(e.value) and "sekrit-pw" not in str(e.value)


def test_the_shipped_catalog_passes():
    load_catalog(REPO_CATALOG)
