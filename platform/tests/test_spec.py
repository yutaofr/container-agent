import pytest
from pydantic import ValidationError

from cagent.spec import ClusterSpec, SpecError, load_spec

from .conftest import FIXTURES


def test_sample_spec_loads(orders_spec):
    assert orders_spec.name == "orders-poc"
    assert [m.instance for m in orders_spec.middleware] == ["mysql", "kafka", "redis"]
    assert orders_spec.services[0].publish == {"http": 18080}


def test_duplicate_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate names: mysql"):
        ClusterSpec.model_validate(
            {
                "name": "x",
                "purpose": "p",
                "middleware": [{"template": "mysql"}, {"template": "mysql"}],
            }
        )


def test_instance_name_overrides_template():
    spec = ClusterSpec.model_validate(
        {
            "name": "x",
            "purpose": "p",
            "middleware": [{"template": "mysql"}, {"template": "mysql", "name": "mysql-b"}],
        }
    )
    assert [m.instance for m in spec.middleware] == ["mysql", "mysql-b"]


@pytest.mark.parametrize("name", ["Orders", "-orders", "orders poc", ""])
def test_invalid_project_names_are_rejected(name):
    with pytest.raises(ValidationError):
        ClusterSpec.model_validate({"name": name, "purpose": "p"})


def test_load_spec_wraps_errors(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text("name: x\npurpose: p\nservcies: []\n")
    with pytest.raises(SpecError, match="servcies"):
        load_spec(path)
    with pytest.raises(SpecError):
        load_spec(FIXTURES / "missing.yaml")
