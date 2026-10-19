"""platform.yaml: strict loading, tokens from the environment, origin and cookie rules."""

import pytest
import yaml

from cagent.config import ConfigError, check_origin, load_config

ENV = {"RUNNER_1": "r" * 32, "ALICE": "a" * 32, "BOB": "b" * 32}


def config(tmp_path, env=ENV, **web):
    doc = {
        "db": {"path": "data/cagent.db"},
        "catalog": ["../catalog"],
        "hub": {"runners": {"laptop-1": {"token_env": "RUNNER_1"}}},
        "web": {"origin": "http://127.0.0.1:8000", "dev_http": True, **web},
        "users": {
            "alice": {"token_env": "ALICE", "runners": ["laptop-1"]},
            "bob": {"token_env": "BOB", "runners": []},
        },
    }
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(doc))
    return load_config(path, env)


def test_a_local_development_config_loads(tmp_path):
    c = config(tmp_path)
    assert c.runner_tokens == {"r" * 32: "laptop-1"}
    assert c.user_tokens == {"alice": "a" * 32, "bob": "b" * 32}
    assert c.user_runners == {"alice": {"laptop-1"}, "bob": set()}
    assert c.db_path == tmp_path / "data/cagent.db" and c.catalog == [tmp_path / "../catalog"]
    assert (c.web_host, c.web_port, c.secure_cookies) == ("127.0.0.1", 8000, False)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({**ENV, "ALICE": ""}, "ALICE is missing or empty"),
        ({k: v for k, v in ENV.items() if k != "BOB"}, "BOB is missing or empty"),
        ({**ENV, "ALICE": "short"}, "shorter than 32"),
        ({**ENV, "BOB": "a" * 32}, "same token"),
        ({**ENV, "ALICE": "r" * 32}, "same token"),
    ],
)
def test_tokens_must_exist_be_long_and_be_unique(tmp_path, env, message):
    with pytest.raises(ConfigError, match=message):
        config(tmp_path, env)


def test_a_user_names_only_known_runners(tmp_path):
    path = tmp_path / "platform.yaml"
    config(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["users"]["bob"]["runners"] = ["laptop-9"]
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ConfigError, match="unknown runners"):
        load_config(path, ENV)


@pytest.mark.parametrize(
    ("web", "message"),
    [
        ({"origin": "https://cagent.example.com"}, "needs web.secure_cookies"),
        ({"secure_cookies": True}, "needs an https origin"),
        ({"dev_http": False}, "only for local development"),
        ({"origin": "http://cagent.example.com"}, "only for local development"),
        ({"host": "0.0.0.0"}, "only for local development"),
        ({"origin": "http://127.0.0.1:8000/app"}, "exactly scheme://host"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_the_origin_matches_how_cookies_are_sent(tmp_path, web, message):
    with pytest.raises(ConfigError, match=message):
        config(tmp_path, **web)


def test_https_behind_a_proxy_is_allowed(tmp_path):
    c = config(tmp_path, origin="https://cagent.example.com", secure_cookies=True, dev_http=False)
    assert c.origin == "https://cagent.example.com" and c.secure_cookies


@pytest.mark.parametrize(
    "origin",
    ["ftp://h", "https://", "https://h/path", "https://h?q=1", "https://h#f", "https://u:p@h"],
)
def test_an_origin_is_exactly_scheme_host_port(origin):
    with pytest.raises(ConfigError):
        check_origin(origin)
