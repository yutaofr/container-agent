"""cagent-gen: generate a compose file from a ClusterSpec."""

import argparse
import sys
from pathlib import Path

from cagent.catalog import CatalogError, load_catalog
from cagent.generate import GenerationError, generate, to_yaml
from cagent.spec import SpecError, load_spec

EXIT_OK, EXIT_PROBLEMS, EXIT_INPUT = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cagent-gen", description=__doc__)
    parser.add_argument("spec", type=Path, help="ClusterSpec YAML file")
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        required=True,
        help="catalog root with middleware/ and services/; repeatable",
    )
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(*args.catalog)
        spec = load_spec(args.spec)
    except (CatalogError, SpecError) as e:
        print(e, file=sys.stderr)
        return EXIT_INPUT
    try:
        text = to_yaml(generate(spec, catalog))
    except GenerationError as e:
        for problem in e.problems:
            print(f"- {problem}", file=sys.stderr)
        return EXIT_PROBLEMS
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return EXIT_OK
