"""cagent-eval: lint scenarios, prepare caches, run an evaluation, rebuild its report, pack it.

    cagent-eval lint
    cagent-eval prepare --set full-repair --set safety
    cagent-eval run --report formal | ci-fast | comparison [--set S] [--mode M]
    cagent-eval run --resume eval-runs/<id> [--retry-void "<the cause that was fixed>"]
    cagent-eval report eval-runs/<id>
    cagent-eval pack eval-runs/<id>

Paths default to the repository that holds the current directory. Exit codes: 0 passed,
1 complete but a gate failed, 2 incomplete or an error.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from cagent.eval import evaluate
from cagent.eval.host import DockerHost, HostError
from cagent.eval.report import write_report
from cagent.eval.scenario import ScenarioError, lint, load_scenario, load_sets


def _repo() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return Path(out)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cagent-eval")
    p.add_argument("--repo", type=Path, default=None, help="repository root (default: git)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("lint")
    prep = sub.add_parser("prepare")
    prep.add_argument("--set", dest="sets", action="append", required=True)
    r = sub.add_parser("run")
    r.add_argument("--report", choices=evaluate.REPORT_TYPES)
    r.add_argument("--set", dest="sets", action="append", default=[])
    r.add_argument("--mode", dest="modes", action="append", default=[])
    r.add_argument("--resume", type=Path)
    r.add_argument("--retry-void", metavar="BECAUSE")
    r.add_argument("--out", type=Path, default=None, help="base directory (default eval-runs)")
    r.add_argument("--runner", type=Path, default=None)
    for name in ("report", "pack"):
        sub.add_parser(name).add_argument("dir", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = (args.repo or _repo()).resolve()
    root, catalog = repo / "scenarios", repo / "catalog"
    try:
        if args.command == "lint":
            problems = lint(root, catalog)
            for problem in problems:
                print(problem)
            print(f"{len(problems)} problems" if problems else "scenarios ok")
            return 2 if problems else 0
        if args.command == "prepare":
            return asyncio.run(_prepare(repo, args.sets))
        if args.command == "report":
            return _print_summary(write_report(args.dir))
        if args.command == "pack":
            return _pack(args.dir, repo)
        return asyncio.run(_run(repo, args))
    except (evaluate.PreflightError, ScenarioError, HostError) as e:
        print(f"cagent-eval: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # 1 means "complete, a gate failed": a crash must never look so
        print(f"cagent-eval: unexpected {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def _print_summary(summary: dict) -> int:
    g = summary["gates"]
    print(f"{summary['eval_id']}: {g['status']} (exit {g['exit']})")
    if summary["repair"]["n"]:
        print(
            f"R = {summary['repair']['R']}, stable = {summary['repair']['stable']}, "
            f"N = {summary['repair']['n']}"
        )
    for b in summary["blocking"]:
        print(f"incomplete: {b}")
    return g["exit"]


async def _run(repo: Path, args: argparse.Namespace) -> int:
    runner = args.runner or Path(os.environ.get("CAGENT_RUNNER", repo / "bin/cagent-runner"))
    runner = runner.resolve()
    models = repo / "platform" / "models.yaml"
    host = DockerHost()
    if args.resume:
        directory = args.resume
        manifest = json.loads((directory / "manifest.json").read_text())
        evaluate.check_unchanged(repo, repo / "scenarios", runner, manifest)
    else:
        if args.report is None:
            raise evaluate.PreflightError("run needs --report or --resume")
        directory = await evaluate.start(
            repo,
            repo / "scenarios",
            args.out or repo / "eval-runs",
            args.report,
            host,
            runner,
            models,
            args.sets,
            args.modes,
        )
        manifest = json.loads((directory / "manifest.json").read_text())
        print(f"evaluation {directory}")
    tools = evaluate.make_tools(repo, repo / "scenarios", runner, models, host, manifest)
    summary = await evaluate.run(
        directory, repo / "scenarios", repo / "catalog", tools, args.retry_void
    )
    return _print_summary(summary)


async def _prepare(repo: Path, set_names: list[str]) -> int:
    """Pull every middleware image and warm every fixture build the sets use. Formal runs
    still build through the Runner; this only fills Docker's caches."""
    root, host = repo / "scenarios", DockerHost(timeout_s=1800)
    sets = load_sets(root)
    images, builds = set(), set()
    for name in set_names:
        for sid in sets[name].scenarios:
            loaded = load_scenario(root / sid, repo / "catalog")
            images |= loaded.images()
            for project in loaded.workdirs():  # every variant: the baseline may use another
                for d in loaded.descriptors().values():
                    w = d.sources.workdir
                    if w is not None and w.dockerfile and (project / w.dockerfile).is_file():
                        builds.add((project, w.dockerfile, w.context or "."))
    for image in sorted(images):
        print(f"pull {image}", flush=True)
        await host._docker("pull", "-q", image, timeout=1800)
    for w, dockerfile, context in sorted(builds):
        print(f"build {w.name}/{dockerfile}", flush=True)
        await host._docker("build", "-q", "-f", str(w / dockerfile), str(w / context), timeout=1800)
    return 0


REPORT_FILES = ("manifest.json", "summary.json", "judgements.json", "report.md")


def _pack(directory: Path, repo: Path) -> int:
    """Pack a copy of the evaluation with the repository root replaced by <repo>. The
    evaluation itself is never changed. The report regenerated from the copy must equal the
    original byte for byte, or nothing is exported: the replacement must not touch a result."""
    out = directory.with_name(directory.name + "-evidence.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / directory.name
        shutil.copytree(directory, copy)
        root = str(repo).encode()
        for path in copy.rglob("*"):
            if path.is_file() and root in (data := path.read_bytes()):
                path.write_bytes(data.replace(root, b"<repo>"))
        write_report(copy)
        if changed := [
            f for f in REPORT_FILES if (copy / f).read_bytes() != (directory / f).read_bytes()
        ]:
            print(
                f"cagent-eval: pack stopped, nothing exported: {', '.join(changed)} would change",
                file=sys.stderr,
            )
            return 2
        with tarfile.open(out, "w:gz") as tar:
            tar.add(copy, arcname=directory.name)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out} sha256 {digest} {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
