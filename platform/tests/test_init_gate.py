"""M7c: automatic fixes never change an init service or what waits for it."""

import pytest

from cagent.diagnose import judge
from cagent.patches import init_services, within_environment

COMPOSE = {
    "services": {
        "mysql": {"image": "mysql:8.4"},
        "mysql-init": {"image": "mysql:8.4", "labels": {"cagent.role": "init"}},
        "orders": {
            "image": "cagent/orders:release-1",
            "depends_on": {
                "mysql": {"condition": "service_healthy"},
                "mysql-init": {"condition": "service_completed_successfully"},
            },
        },
    }
}
PROTECTED = init_services(COMPOSE)


def test_init_services_are_found_by_label():
    assert PROTECTED == frozenset({"mysql-init"})


@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": "/services/mysql-init/mem_limit", "value": "1g"},
        {"op": "add", "path": "/services/mysql-init/environment/X", "value": "1"},
        {"op": "remove", "path": "/services/orders/depends_on/mysql-init"},
        {"op": "replace", "path": "/services/orders/depends_on", "value": {}},
        {"op": "remove", "path": "/services/orders/depends_on"},
    ],
)
def test_patches_touching_init_are_outside_the_gate(op):
    assert within_environment([op])  # the gate alone does not know which services are init
    assert not within_environment([op], PROTECTED)


def test_other_fixes_still_pass():
    ops = [
        {"op": "replace", "path": "/services/mysql/mem_limit", "value": "1g"},
        {"op": "remove", "path": "/services/orders/depends_on/mysql"},
    ]
    assert within_environment(ops, PROTECTED)


def test_judge_rejects_a_model_patch_on_an_init_service():
    patch = [{"op": "add", "path": "/services/mysql-init/mem_limit", "value": "1g"}]
    verdict, _ = judge(patch, {"mysql-init": set()}, PROTECTED, COMPOSE)
    assert verdict == "reject"
