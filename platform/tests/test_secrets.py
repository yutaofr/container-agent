import pytest

from cagent.secrets import (
    MASK,
    SECRET_KEY,
    is_reference,
    known_secrets,
    mask_argv,
    mask_compose,
    mask_shell,
    mask_text,
    trigger,
)


@pytest.mark.parametrize(
    "value, ref",
    [
        ("$MYSQL_PASSWORD", True),
        ("${MYSQL_PASSWORD}", True),
        ('"$MYSQL_PASSWORD"', True),
        ("'${DB_TOKEN}'", True),
        ("${PASSWORD:-root}", False),
        ("${PASSWORD-root}", False),
        ("$(cat /secret)", False),
        ("${PASSWORD}:literal", False),
        ("$PASSWORD$OTHER", False),
        ("root", False),
        ("", False),
    ],
)
def test_a_reference_is_a_whole_variable_and_nothing_else(value, ref):
    assert is_reference(value) is ref


def test_secret_key_names():
    for name in ("MYSQL_PASSWORD", "db_passwd", "PWD", "client_secret", "API_KEY", "apikey"):
        assert SECRET_KEY.search(name)
    for name in ("MYSQL_USER", "APP_MODE", "CACHE_PORT"):
        assert not SECRET_KEY.search(name)


@pytest.mark.parametrize(
    "text, want",
    [
        ("mysqladmin ping -h 127.0.0.1 -uapp -papp --silent", "-uapp -p****** --silent"),
        ("/usr/bin/mysql -uroot -proot app", "-uroot -p****** app"),
        ('mysql -h "a;b" -proot', '-h "a;b" -p******'),
        ("mysql -h db -p'se cret' -e 'SELECT 1'", "-p****** -e 'SELECT 1'"),
        ('mysqladmin ping -u{{user}} -p"$MYSQL_PASSWORD" --silent', '-p"$MYSQL_PASSWORD"'),
        ("mysql -uroot -p", "mysql -uroot -p"),  # -p alone prompts for the password
        ("echo hi; mysql -uroot -proot -N", "echo hi; mysql -uroot -p****** -N"),
        ("redis-cli -p 6380 ping", "redis-cli -p 6380 ping"),  # -p is a port without mysql
        ("nc -p 6380 x; mysql -pabc", "nc -p 6380 x; mysql -p******"),
        ("tool --password=hunter2 --x", "tool --password=****** --x"),
        ("tool --password hunter2 --x", "tool --password ****** --x"),
        ('tool --password="$DB_PASSWORD"', 'tool --password="$DB_PASSWORD"'),
        ("curl http://u:pw@host/x", "curl http://u:******@host/x"),
        ("DB_PASSWORD=abc ./run", "DB_PASSWORD=****** ./run"),
        ('mysql -uroot -proot app < "$file" 2>/dev/null', "-p****** app <"),
    ],
)
def test_shell_text_masks_credential_positions(text, want):
    assert want in mask_shell(text)


def test_separators_inside_quotes_keep_the_command_together():
    assert mask_shell("mysql -e 'a; b' -proot") == "mysql -e 'a; b' -p******"
    # outside quotes, the separator ends mysql's command: -p belongs to nc
    assert mask_shell("mysql -e x; nc -p 5 h") == "mysql -e x; nc -p 5 h"


@pytest.mark.parametrize(
    "text",
    [
        'echo "$(mysql -proot)"',
        "echo `mysql -uroot -proot`",
        "cat <<EOF\nmysql -proot\nEOF",
        "eval mysql -proot",
        "mysql -proot 'unbalanced",
        "sh -c 'mysql -proot'",
    ],
)
def test_unsupported_syntax_with_a_trigger_masks_the_whole_field(text):
    assert mask_shell(text) == MASK


def test_unsupported_syntax_without_a_trigger_is_kept():
    script = (
        "for file in /cagent-init/orders/*; do\n"
        '  sha=$(sha256sum "$file" | cut -d " " -f 1)\n'
        '  mysql -hmysql -uroot -p"$MYSQL_ROOT_PASSWORD" app < "$file"\n'
        "done"
    )
    assert mask_shell(script) == script


def test_triggers_ignore_references_and_ports():
    assert trigger('mysql -p"$MYSQL_PASSWORD"') is False
    assert trigger("redis-cli -p 6380") is False
    assert trigger("echo $(mysql -proot)") is True
    assert trigger("x --password=abc") is True
    assert trigger("http://u:p@h") is True


@pytest.mark.parametrize(
    "argv, want",
    [
        (["CMD", "mysql", "-uroot", "-proot"], ["CMD", "mysql", "-uroot", "-p******"]),
        (["/usr/bin/mysqladmin", "-pabc", "ping"], ["/usr/bin/mysqladmin", "-p******", "ping"]),
        (["tool", "--password", "abc", "x"], ["tool", "--password", "******", "x"]),
        (["tool", "--password=abc"], ["tool", "--password=******"]),
        (["redis-server", "-p", "6380"], ["redis-server", "-p", "6380"]),
        (["mysql", '-p"$MYSQL_PASSWORD"'], ["mysql", '-p"$MYSQL_PASSWORD"']),
        (["sh", "-c", "mysql -proot"], ["sh", "-c", "mysql -p******"]),
        (["/bin/bash", "-c", 'echo "$(mysql -proot)"'], ["/bin/bash", "-c", MASK]),
    ],
)
def test_argument_arrays_keep_their_context(argv, want):
    assert mask_argv(argv) == want


def test_text_masks_urls_assignments_and_long_known_values():
    known = {"s3cr3t-value-123", "app"}
    line = "jdbc:mysql://app:pw@mysql/app?token=abc&x=1 s3cr3t-value-123 user app"
    assert mask_text(line, known) == (
        "jdbc:mysql://app:******@mysql/app?token=******&x=1 ****** user app"
    )


def test_text_keeps_references_and_short_known_values():
    assert mask_text('PASSWORD="$DB_PASSWORD" -uroot', {"root"}) == 'PASSWORD="$DB_PASSWORD" -uroot'


def test_prose_masks_short_options_only_beside_a_mysql_client():
    assert mask_text("run mysql -uroot -proot to check") == "run mysql -uroot -p****** to check"
    assert mask_text("redis-cli -p 6380 and nc -p5") == "redis-cli -p 6380 and nc -p5"
    assert mask_text('mysql -p"$MYSQL_PASSWORD"') == 'mysql -p"$MYSQL_PASSWORD"'


def test_known_secrets_come_from_secret_named_keys_in_every_shape():
    compose = {
        "services": {
            "db": {"environment": {"MYSQL_PASSWORD": "app", "MYSQL_USER": "app", "X_TOKEN": ""}},
            "app": {"environment": ["API_KEY=k-123456789", "MODE=a", "DB_PASSWORD=$DB_PASSWORD"]},
            "bare": {"image": "busybox"},
        }
    }
    assert known_secrets(compose) == {"app", "k-123456789"}


def test_mask_compose_keeps_context_and_leaves_the_input_alone():
    compose = {
        "services": {
            "mysql": {
                "image": "mysql:8.4",
                "environment": {"MYSQL_PASSWORD": "app", "MYSQL_DATABASE": "app"},
                "healthcheck": {
                    "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -papp --silent"],
                    "interval": "5s",
                },
            },
            "mysql-init": {
                "entrypoint": ["sh", "-c", "mysql -hmysql -uroot -proot app < /x.sql"],
            },
            "orders": {
                "environment": [
                    "SPRING_DATASOURCE_URL=jdbc:mysql://app:pw@mysql:3306/app",
                    "SPRING_DATASOURCE_PASSWORD=app",
                ],
                "command": ["java", "-Dspring.datasource.password=long-secret-1", "-jar", "a"],
                "labels": {"note": "long-secret-1 is here"},
            },
        }
    }
    view = mask_compose(compose, {"app", "long-secret-1"})
    mysql, init, orders = (
        view["services"]["mysql"],
        view["services"]["mysql-init"],
        view["services"]["orders"],
    )
    assert mysql["environment"] == {"MYSQL_PASSWORD": MASK, "MYSQL_DATABASE": "app"}
    assert mysql["healthcheck"] == {
        "test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uapp -p****** --silent"],
        "interval": "5s",
    }
    assert init["entrypoint"][2] == "mysql -hmysql -uroot -p****** app < /x.sql"
    assert orders["environment"] == [
        "SPRING_DATASOURCE_URL=jdbc:mysql://app:******@mysql:3306/app",
        f"SPRING_DATASOURCE_PASSWORD={MASK}",
    ]
    assert orders["command"][1] == "-Dspring.datasource.password=******"
    assert orders["labels"] == {"note": "****** is here"}
    assert compose["services"]["mysql"]["environment"]["MYSQL_PASSWORD"] == "app"
