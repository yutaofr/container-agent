# Fault scenarios

Broken clusters the orchestrator must repair, or must refuse to "repair". Each one injects a
single root cause before generation and comes with a healthy baseline, evidence that the
fault really happened, and what a correct result looks like. Design:
[docs/plans/2026-10-20-m9a-scenarios-eval-design.md](../docs/plans/2026-10-20-m9a-scenarios-eval-design.md).

```
scenarios/
  <id>/scenario.yaml            one scenario (and optionally catalog/, a layer of new templates)
  fixtures/catalog/middleware/  templates shared by scenarios, healthy ones and faulty copies
  fixtures/springapp/           a Spring Boot service built from its own Dockerfile
  sets/<name>.yaml              explicit id lists: ci-fast, quick, full-repair, safety, combo
```

## Commands

```bash
make eval-lint      # every scenario, layer, fixture descriptor, and set loads and generates
make eval-fast      # CI regression: ci-fast on real Docker, T0 only (not the acceptance)
make eval           # the formal evaluation: hours, needs local Ollama
```

`cd platform && uv run cagent-eval --help` lists the rest: `prepare` warms image and build
caches, `run --resume <dir>` continues an evaluation, `report <dir>` recomputes a report from
its files alone, and `pack <dir>` makes the evidence archive. Evaluations land in
`eval-runs/<date>-<commit>-<n>/` (ignored by git).

## Adding a scenario

1. Give the fault a uniquely named template in `fixtures/catalog/middleware/` (copy the
   healthy one and change one root cause), a spec override, a fixture project, or an
   `occupied_port` host condition. Template names never reach the compose file; instance
   names do, so keep them neutral (`api`, not `api-typo`).
2. Write `scenario.yaml`: `baseline.spec_patch` turns the fault into the healthy variant;
   `expect.config` proves the fault is in the first compose; `expect.runtime` names the
   signature, a log line, or both, from one service record, that proves it happened;
   `after` and `checks` prove the repair. `checks` must exercise the dependency for real.
3. List it in a set, run `make eval-lint`, then admit it: its baseline must pass with no fix
   and its fault must trigger (`uv run cagent-eval run --report comparison --set <set>
   --mode t0`). Admission never depends on whether the system repairs it.
