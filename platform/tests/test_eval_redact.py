"""Everything the harness writes is redacted, and redacted secrets still compare equal."""

import pytest

from cagent.eval.redact import Redactor

# The Runner's own cases (runner/internal/redact/redact_test.go): the same text rules.
RUNNER_CASES = [
    (
        "jdbc:mysql://db:3306/app?user=root&password=s3cret&useSSL=false",
        "jdbc:mysql://db:3306/app?user=root&password=******&useSSL=false",
    ),
    ("SPRING_DATASOURCE_PASSWORD=hunter2", "SPRING_DATASOURCE_PASSWORD=******"),
    ('{"api_key": "abc123", "user": "bob"}', '{"api_key": "******", "user": "bob"}'),
    (
        "connecting to postgres://admin:pa55@db:5432/x",
        "connecting to postgres://admin:******@db:5432/x",
    ),
    ("Authorization: Bearer eyJhbGciOi.abc.def", "Authorization: Bearer ******"),
    ("key AKIAIOSFODNN7EXAMPLE loaded", "key ****** loaded"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "aws_secret_access_key = ******"),
    (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "-----PRIVATE KEY ******-----",
    ),
    ('SPRING_DATASOURCE_PASSWORD: "{{password}}"', 'SPRING_DATASOURCE_PASSWORD: "{{password}}"'),
    ("spring.datasource.password=${DB_PASSWORD}", "spring.datasource.password=${DB_PASSWORD}"),
    ("password: ${DB_PASSWORD:s3cret}", "password: ${DB_PASSWORD:******}"),
    (
        "jdbc:mysql://{{user}}:{{password}}@{{host}}/app",
        "jdbc:mysql://{{user}}:{{password}}@{{host}}/app",
    ),
    ("password=${x}y", "password=******"),
    (
        "Started Application in 3.2 seconds (tokenizer ready)",
        "Started Application in 3.2 seconds (tokenizer ready)",
    ),
]


@pytest.mark.parametrize("text, want", RUNNER_CASES)
def test_text_follows_the_runners_rules(text, want):
    assert Redactor(b"k").text(text) == want


def test_known_secrets_become_tags_that_compare_equal_and_nothing_else():
    r = Redactor(b"k", ["hunter2-pw"])
    a, b = r.text("login with hunter2-pw failed"), r.text("retry hunter2-pw")
    tag = r.tag("hunter2-pw")
    assert a == f"login with {tag} failed" and b == f"retry {tag}"
    assert tag != r.tag("hunter3-pw") and tag != Redactor(b"other").tag("hunter2-pw")
    assert r.text("pw") == "pw"  # too short to be learned as a secret
    r.add("root")
    assert r.text("user root logged in") == "user root logged in"  # short: keys only
    assert r.text("xhunter2-pwx") == "xhunter2-pwx"  # whole words only


def test_documents_tag_secret_keys_and_patch_values_by_their_path():
    r = Redactor(b"k")
    compose = {"services": {"db": {"environment": {"DB_PASSWORD": "s3cret-value", "USER": "app"}}}}
    r.learn(compose)
    out = r.value(
        {
            "compose": compose,
            "patch": [
                {"op": "replace", "path": "/services/db/environment/DB_PASSWORD", "value": "******"}
            ],
            "log": ["connecting as app with s3cret-value"],
        }
    )
    env = out["compose"]["services"]["db"]["environment"]
    assert env == {"DB_PASSWORD": r.tag("s3cret-value"), "USER": "app"}
    assert out["patch"][0]["value"] == r.tag("******") != env["DB_PASSWORD"]
    assert out["log"] == [f"connecting as app with {r.tag('s3cret-value')}"]
    assert r.value(out) == out  # tags are left alone on a second pass
