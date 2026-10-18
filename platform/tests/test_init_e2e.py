"""M7c's acceptance on real Docker: MySQL tables, a Cassandra keyspace and table, and Kafka
topics exist before the application starts; a second up runs no script again; a broken script
stops the run as init_failed until the user fixes it.

The project already has its descriptor, so no model is involved and this runs in CI. Skipped
unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import os
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.generate import to_yaml
from cagent.hub import RunnerHub, start
from cagent.orchestrator import Orchestrator, Settings
from cagent.spec import ClusterSpec

from .conftest import REPO_CATALOG
from .test_hub_e2e import TOKEN, free_port

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

DOCKERFILE = (
    "FROM busybox:1.37\n"
    "RUN mkdir -p /www && echo ok > /www/index.html\n"
    'CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]\n'
)
DESCRIPTOR = """name: shop
kind: app
stack: dockerfile
sources:
  workdir: {{ dockerfile: Dockerfile }}
runtime:
  ports: [{{ name: http, container: 8080 }}]
  healthcheck: {{ cmd: ["CMD-SHELL", "wget -q -O /dev/null http://localhost:8080/"] }}
requires:
{requires}
"""
ALL_THREE = """  - capability: mysql
    init: { scripts: [db/schema.sql, db/seed.sql] }
  - capability: cassandra
    init: { keyspaces: [shop], scripts: [cql/schema.cql] }
  - capability: kafka
    init: { topics: [{ name: orders-events, partitions: 3 }] }"""


def exec_in(name: str, service: str, *cmd: str) -> str:
    out = subprocess.run(
        ["docker", "exec", f"cagent-{name}-{service}-1", *cmd], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def container_time(name: str, service: str, field: str) -> datetime:
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.State." + field + "}}", f"cagent-{name}-{service}-1"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    base, _, frac = out.removesuffix("Z").partition(".")  # nanoseconds, maybe trimmed
    return datetime.fromisoformat(f"{base}.{(frac + '000000')[:6]}+00:00")


def logs(name: str, service: str) -> str:
    return subprocess.run(
        ["docker", "logs", f"cagent-{name}-{service}-1"], capture_output=True, text=True
    ).stdout


@asynccontextmanager
async def runner_session(tmp_path: Path, root: Path):
    hub_port = free_port()
    hub = RunnerHub({TOKEN: "e2e"})
    server = await start(hub, "127.0.0.1", hub_port)
    env = {**os.environ, "CAGENT_TOKEN": TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
    runner = subprocess.Popen(
        [
            os.environ["CAGENT_RUNNER"],
            "serve",
            "-hub",
            f"ws://127.0.0.1:{hub_port}/runner",
            "-workdir-root",
            str(root),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        yield hub
    finally:
        runner.terminate()
        runner.wait(timeout=10)
        server.close()
        await server.wait_closed()


def project(root: Path, requires: str, files: dict[str, str]) -> Path:
    path = root / "shop"
    for rel, body in {
        "Dockerfile": DOCKERFILE,
        ".cagent/services/shop.yaml": DESCRIPTOR.format(requires=requires),
        **files,
    }.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(body)
    return path


def run(hub, name: str, workdir: Path, *middleware: str) -> tuple[Orchestrator, ClusterSpec]:
    spec = ClusterSpec.model_validate(
        {
            "name": name,
            "purpose": "the shop with its schema, keyspace, and topics",
            "services": [{"name": "shop", "source": {"kind": "workdir", "ref": str(workdir)}}],
            "middleware": [{"template": m} for m in middleware],
        }
    )
    settings = Settings(poll_interval_s=1, stability_window_s=5, startup_timeout_s=240)
    return Orchestrator(hub, load_catalog(REPO_CATALOG), InMemorySaver(), settings), spec


async def test_init_runs_once_before_the_application(tmp_path):
    root = tmp_path / "projects"
    workdir = project(
        root,
        ALL_THREE,
        {
            "db/schema.sql": "CREATE TABLE orders (id INT PRIMARY KEY, item VARCHAR(40));\n",
            "db/seed.sql": "INSERT INTO orders VALUES (1, 'book');\n",
            "cql/schema.cql": "CREATE TABLE shop.events (id int PRIMARY KEY, kind text);\n",
        },
    )
    name = f"m7c-e2e-{int(time.time()) % 100000}"
    tag = None
    async with runner_session(tmp_path, root) as hub:
        try:
            o, spec = run(hub, name, workdir, "mysql", "cassandra", "kafka")
            status = await o.start("run-1", spec, "e2e")
            assert status.waiting["kind"] == "approval", status.waiting
            status = await o.resume("run-1", {"approve": True})
            assert status.outcome == "healthy", status.waiting
            tag = status.compose["services"]["shop"]["image"]

            rows = exec_in(
                name, "mysql", "mysql", "-uroot", "-proot", "-N", "-e", "SELECT * FROM app.orders"
            )
            assert rows.split() == ["1", "book"]
            assert "CREATE TABLE shop.events" in exec_in(
                name, "cassandra", "cqlsh", "-e", "DESCRIBE TABLE shop.events"
            )
            topic = exec_in(
                name,
                "kafka",
                "/opt/kafka/bin/kafka-topics.sh",
                "--bootstrap-server",
                "localhost:9092",
                "--describe",
                "--topic",
                "orders-events",
            )
            assert "PartitionCount: 3" in topic
            started = container_time(name, "shop", "StartedAt")
            for init in ("mysql-init", "cassandra-init", "kafka-init"):
                assert container_time(name, init, "FinishedAt") < started, init

            # A second up, as after a self-healing patch: init runs again and skips everything.
            res = await hub.call("e2e", "up", {"name": name, "compose": to_yaml(status.compose)})
            assert res.ok, res.stderr
            assert logs(name, "mysql-init").count("skip /cagent-init/shop/") == 2
            assert logs(name, "cassandra-init").count("skip /cagent-init/shop/") == 1
            rows = exec_in(
                name,
                "mysql",
                "mysql",
                "-uroot",
                "-proot",
                "-N",
                "-e",
                "SELECT COUNT(*) FROM app.orders",
            )
            assert rows.strip() == "1"
        finally:
            await hub.call("e2e", "down", {"name": name})
            if tag:
                subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


async def test_a_broken_script_stops_the_run_until_it_is_fixed(tmp_path):
    root = tmp_path / "projects"
    requires = "  - capability: mysql\n    init: { scripts: [db/schema.sql] }"
    workdir = project(
        root, requires, {"db/schema.sql": "CREATE TABLE orders (id INT PRIMARY KEY;\n"}
    )
    name = f"m7c-bad-{int(time.time()) % 100000}"
    tag = None
    async with runner_session(tmp_path, root) as hub:
        try:
            o, spec = run(hub, name, workdir, "mysql")
            await o.start("run-1", spec, "e2e")
            status = await o.resume("run-1", {"approve": True})
            assert status.waiting["kind"] == "escalation", status.waiting
            assert status.waiting["reason"] == "init_failed"
            assert status.waiting["service"] == "mysql-init"
            assert status.waiting["script"] == "/cagent-init/shop/01-schema.sql"
            assert status.fixes == []

            (workdir / "db/schema.sql").write_text("CREATE TABLE orders (id INT PRIMARY KEY);\n")
            status = await o.resume("run-1", {"action": "retry"})
            assert status.outcome == "healthy", status.waiting
            tag = status.compose["services"]["shop"]["image"]
        finally:
            await hub.call("e2e", "down", {"name": name})
            if tag:
                subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
