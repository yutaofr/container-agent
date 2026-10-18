"""M7c: the orchestrator stages release scripts and stops at an init failure."""

from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.orchestrator import Orchestrator
from cagent.spec import ClusterSpec

from .conftest import REPO_CATALOG
from .simcluster import SimCluster
from .test_orchestrator import FAST, approve, no_sleep

SHOP = """name: shop
kind: app
stack: java-springboot
sources:
  release: { nexus: "com.acme:shop:{version}:jar" }
  workdir: { build: "mvn -q package", output: "target/*.jar" }
runtime: {}
requires:
  - capability: mysql
    init: { scripts: [db/schema.sql, db/seed.sql] }
  - capability: kafka
    init: { topics: [{ name: orders }] }
"""
FAILED_LINE = "cagent init failed: /cagent-init/shop/01-schema.sql"


def catalog(tmp_path: Path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "shop.yaml").write_text(SHOP)
    return load_catalog(REPO_CATALOG, tmp_path)


def spec(kind: str = "release") -> ClusterSpec:
    ref = "1.0" if kind == "release" else "/work/shop"
    return ClusterSpec.model_validate(
        {
            "name": "shop",
            "purpose": "test",
            "services": [{"name": "shop", "source": {"kind": kind, "ref": ref}}],
            "middleware": [{"template": "mysql"}, {"template": "kafka"}],
        }
    )


def orchestrator(sim, cat) -> Orchestrator:
    return Orchestrator(sim, cat, InMemorySaver(), FAST, sleep=no_sleep)


async def test_release_scripts_go_to_build_and_init_counts_as_settled(tmp_path):
    sim = SimCluster()
    o = orchestrator(sim, catalog(tmp_path))
    await o.start("t1", spec(), "laptop-1")
    status = await approve(o, "t1")
    assert status.outcome == "healthy", status.waiting
    assert sim.builds[0]["init"] == [
        {"capability": "mysql", "path": "db/schema.sql"},
        {"capability": "mysql", "path": "db/seed.sql"},
    ]


async def test_workdir_builds_send_no_scripts(tmp_path):
    sim = SimCluster()
    o = orchestrator(sim, catalog(tmp_path))
    await o.start("t1", spec("workdir"), "laptop-1")
    assert (await approve(o, "t1")).outcome == "healthy"
    assert "init" not in sim.builds[0]


async def test_failed_init_escalates_without_a_fix_then_retry_rebuilds(tmp_path):
    sim = SimCluster(failing_init={"mysql-init": FAILED_LINE})
    o = orchestrator(sim, catalog(tmp_path))
    await o.start("t1", spec(), "laptop-1")
    status = await approve(o, "t1")
    assert status.waiting["kind"] == "escalation"
    assert status.waiting["reason"] == "init_failed"
    assert status.waiting["service"] == "mysql-init"
    assert status.waiting["script"] == "/cagent-init/shop/01-schema.sql"
    assert status.waiting["log"] == [FAILED_LINE]
    assert status.fixes == []

    sim.failing_init = {}  # the user fixed the script in the project or the release
    status = await o.resume("t1", {"action": "retry"})
    assert status.outcome == "healthy", status.waiting
    assert len(sim.builds) == 2  # build ran again, so it staged the fixed script


async def test_init_failure_seen_only_in_a_snapshot_also_escalates(tmp_path):
    sim = SimCluster(failing_init={"mysql-init": FAILED_LINE}, init_fails_quietly=True)
    o = orchestrator(sim, catalog(tmp_path))
    await o.start("t1", spec(), "laptop-1")
    status = await approve(o, "t1")
    assert status.waiting["reason"] == "init_failed"
    assert status.fixes == []
