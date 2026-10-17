import pytest

from cagent.render import RenderError, escape_compose, placeholders, render


def test_render_replaces_placeholders():
    got = render(
        "jdbc:mysql://{{host}}:{{ port }}/{{db}}", {"host": "mysql", "port": "3306", "db": "app"}
    )
    assert got == "jdbc:mysql://mysql:3306/app"


def test_render_rejects_unknown_placeholder():
    with pytest.raises(RenderError, match="db"):
        render("{{host}}/{{db}}", {"host": "mysql"})


def test_placeholders():
    assert placeholders("{{a}} and {{ b }} and {{a}}") == {"a", "b"}


def test_escape_compose_doubles_dollar():
    assert escape_compose('test "$(curl x)" = 200') == 'test "$$(curl x)" = 200'
