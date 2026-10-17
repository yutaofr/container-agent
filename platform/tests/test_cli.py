import json
import os
import subprocess

import pytest

from cagent.cli import EXIT_INPUT, EXIT_OK, EXIT_PROBLEMS, main

from .conftest import FIXTURE_CATALOG, FIXTURES, REPO_CATALOG

CATALOG_ARGS = ["--catalog", str(REPO_CATALOG), "--catalog", str(FIXTURE_CATALOG)]


def test_generates_compose_file(tmp_path):
    out = tmp_path / "compose.yaml"
    code = main([str(FIXTURES / "orders-poc.spec.yaml"), *CATALOG_ARGS, "-o", str(out)])
    assert code == EXIT_OK
    assert out.read_text() == (FIXTURES / "orders-poc.compose.yaml").read_text()


def test_lists_problems_on_stderr(tmp_path, capsys):
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "name: x\npurpose: p\nservices:\n  - {name: billing, source: {kind: release, ref: '1'}}\n"
    )
    assert main([str(spec), *CATALOG_ARGS]) == EXIT_PROBLEMS
    assert "- billing: not registered in the catalog" in capsys.readouterr().err


def test_rejects_malformed_spec(tmp_path, capsys):
    spec = tmp_path / "spec.yaml"
    spec.write_text("name: x\n")
    assert main([str(spec), *CATALOG_ARGS]) == EXIT_INPUT
    assert "purpose" in capsys.readouterr().err


@pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)
def test_generated_file_passes_runner_validate(tmp_path):
    out = tmp_path / "compose.yaml"
    assert main([str(FIXTURES / "orders-poc.spec.yaml"), *CATALOG_ARGS, "-o", str(out)]) == EXIT_OK
    result = subprocess.run(
        [os.environ["CAGENT_RUNNER"], "validate", "-f", str(out), "-name", "orders-poc"],
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout or "{}")
    assert result.returncode == 0, f"stderr: {result.stderr}\nreport: {report}"
    assert report["findings"] == [] and report["port_conflicts"] == []
