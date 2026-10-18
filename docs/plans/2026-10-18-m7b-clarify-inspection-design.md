# M7b Clarify & Project Inspection — Design

- Date: 2026-10-18
- Roadmap: [2026-10-17-poc-roadmap.md](2026-10-17-poc-roadmap.md), M7b
- Design: [2026-10-17-container-agent-design.md](2026-10-17-container-agent-design.md), sections 4 (Clarify), 6.1–6.3, 7

## 1. Goal

A user states a cluster's purpose in plain words. The platform turns it into a valid `ClusterSpec`, asking one question at a time about every gap the generator finds. When a named service is a project in a working directory with no descriptor, the platform inspects it on the user's machine, drafts `.cagent/services/<name>.yaml`, and asks the user to save it; the cluster then builds and runs as if the project had always been registered.

**Done when:** Clarify produces a valid ClusterSpec from a prompt, and an unregistered sample project — single- or multi-module — gets a descriptor that builds and runs.

## 2. Decisions

| Decision | Why |
|---|---|
| **The user saves drafted descriptors**; the Runner gets no write tool (user decision) | Keeps the workdir invariant intact: the Runner only runs a build command the developer has in their own project. The platform reads the saved file back and uses exactly what is on disk. |
| **The model extracts once; questions are deterministic** (user decision) | The purpose sentence becomes a draft spec in one large-tier call. Every later question comes from a structured generator gap with options and a default, and every answer is a typed value. Matches "deterministic state machine, bounded LLM nodes"; fully testable with a scripted model; small local models cannot derail it. |
| **Stacks: `java-springboot` and a new `dockerfile` stack** (user decision) | Maven/Gradle Spring Boot modules use the existing builder. Any project with its own Dockerfile (Node, Go, …) builds from that Dockerfile, which the developer wrote. A project with neither gets a note: add a Dockerfile. |
| **Multi-module projects in M7b; descriptors at the root in `.cagent/services/<name>.yaml`** (user decision) | Builds run at the root (`mvn -pl <module> -am`), outputs stay inside the root, so path confinement is unchanged. `.cagent/service.yaml` still works for single-service projects. |
| **Init resources move to a new M7c** (user decision) | Kafka topics, Cassandra keyspaces/tables, and SQL schema scripts need a descriptor schema, generator init jobs, a Cassandra template, and Runner script copying. None depends on inspection, and all of it helps registered projects too. M7b only reports the resource files it finds. |
| A deterministic skeleton decides name, stack, build, and output; the model fills only `runtime`, `requires`, and `probes` | The fields that decide what runs and what is built never come from a model. |
| The Runner reads only a fixed set of files; the platform names no path | `inspect` takes a directory under `-workdir-root` and nothing else. Module lists come from the Runner's own parse of `pom.xml`/`settings.gradle`. |
| Answers and drafts live in checkpointed state; a thread's catalog is the base catalog plus that thread's descriptors | The shared catalog is never written; a resumed thread re-validates its descriptors. |

## 3. Flow

```mermaid
flowchart TD
  P["purpose (free text)"] --> X["extract<br/>large tier, one call → draft ClusterSpec"]
  X --> G["generate"]
  G -->|ok| V["validate → approve ⏸ → build → up → … (unchanged)"]
  G -->|gaps| K{"first gap"}
  K -->|"choice or value"| ASK["ask ⏸<br/>{question, options, default, error}"]
  ASK -->|"valid answer → apply to spec"| G
  ASK -->|invalid| ASK
  K -->|"unregistered, workdir source"| INS["inspect (Runner, read-only)"]
  INS --> SK["skeleton (deterministic)<br/>modules · name · stack · build · output"]
  SK --> DR["draft runtime (model)<br/>ports · env · healthcheck · requires · probes"]
  DR --> SAVE["save ⏸<br/>show YAML + resources found"]
  SAVE --> RB["inspect again → validate the file on disk"]
  RB -->|"missing or invalid"| SAVE
  RB -->|ok| OV["add to the thread's catalog"] --> G
  K -->|"unregistered release/snapshot,<br/>bind render error"| ESC["escalate ⏸ clarify"]
```

`Orchestrator.start(thread_id, runner, spec=…)` keeps working; `start(thread_id, runner, purpose=…)` enters at `extract`. Everything from `validate` on is unchanged.

## 4. Runner (Go)

### 4.1 `inspect -workdir DIR`

A read-only subcommand, also served over RPC. `DIR` must resolve under a `-workdir-root`, as for `build`.

| Read | What |
|---|---|
| Root and every module | `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle`, `settings.gradle.kts`, `package.json`, `go.mod`, `Dockerfile`, `src/main/resources/application.yml`, `application.yaml`, `application.properties` |
| Root only | `.cagent/service.yaml`, `.cagent/services/*.yaml` |
| Modules | The Runner parses `<modules>` in `pom.xml` (recursively) and `include` in `settings.gradle(.kts)` |
| Resources (paths only) | `**/*.cql`, `**/schema*.sql`, `**/db/init/**/*.sql`, Flyway `**/db/migration/**`, Liquibase `**/db/changelog/**` |

- Every path is confined with `guard`; a symlink that leaves `DIR` is skipped.
- Limits: 50 modules, 64 KiB per file (truncated and listed), 2 MiB in total.
- Contents pass through `redact`.
- Output: `{"modules": [...], "files": {"<relative path>": "<content>"}, "resources": [...], "truncated": [...]}`.

### 4.2 Descriptor lookup

A workdir build reads `.cagent/services/<service>.yaml`, else `.cagent/service.yaml`; the descriptor's `name` must equal the service. The build runs at the root and the artifact must resolve inside it.

### 4.3 `dockerfile` stack

`sources.workdir` becomes `{build?, output?, dockerfile?, context?}`:

| Stack | Requires | Build |
|---|---|---|
| `java-springboot` | `build`, `output` | Run `build`, find one artifact, render the JRE Dockerfile (unchanged) |
| `dockerfile` | `dockerfile`; `context` defaults to `.` | Run `build` if present, then `docker build -f <dockerfile> -t <tag> --label cagent.managed=true <context>` |

Both paths are relative and confined to the root. Docker's layer cache stands in for the artifact-sha256 cache. For a workdir build the Runner uses the stack in the project's own descriptor; a different stack from the platform is an error.

Python's `WorkdirSource` and Go's `workdir.Descriptor` apply the same rules and are tested against the same fixture descriptors.

## 5. Platform (Python)

### 5.1 Gaps

`generate` collects structured gaps instead of strings; `str(gap)` keeps today's wording.

| Kind | Question | Options, default | Answer changes |
|---|---|---|---|
| `unknown_template` | Which middleware did you mean? | All templates; closest name | `template` |
| `no_provider` | X needs C — which middleware? | Templates providing C; the first | adds a `MiddlewareRef` |
| `ambiguous_provider` | Which instance should X use for C? | Instances providing C | `bindings` |
| `version_mismatch` | Which version? | Versions satisfying the requirement; the highest | `version` |
| `missing_env` | Value for K? | none; must be answered | `env` |
| `no_source` | Which source for X? then its ref | Source kinds the descriptor declares | `source` |
| `unregistered` | — workdir: inspection; otherwise escalate | | |
| `bind_render` | — escalate (catalog bug) | | |

### 5.2 Question protocol

- Interrupt: `{"type": "question", "gap": {...}, "question": str, "options": [...], "default": ..., "error": str | None}`.
- Resume: `{"answer": value}`; `{"answer": None}` takes the default; `{"action": "abort"}` stops.
- `apply_answer(spec, gap, answer) -> ClusterSpec` is pure. An invalid answer is asked again with `error` set. After each answer `generate` runs again, since one answer can add or remove gaps.
- At most 20 questions per thread, then escalate `clarify`.

### 5.3 Extraction

`extract(router, purpose, catalog)` asks the large tier once. The prompt holds the purpose and a catalog summary: services with their source kinds, templates with capability and versions. The answer schema is `{name, services: [{name, source: {kind, ref}}], middleware: [{template, version?}], checks: [...]}`. Names outside the catalog are kept so they surface as gaps. Schema failures retry once, then move a tier up; when all tiers fail, escalate `model_unavailable`.

### 5.4 Drafting a descriptor

```mermaid
flowchart LR
  F["inspect output"] --> D{"deployable module?<br/>Boot plugin or own Dockerfile"}
  D -->|Boot| J["java-springboot<br/>mvn -q -pl m -am package -DskipTests → m/target/*.jar<br/>./gradlew -q :m:bootJar → m/build/libs/*.jar"]
  D -->|Dockerfile only| DF["dockerfile<br/>dockerfile: m/Dockerfile"]
  D -->|neither| N["note: add a Dockerfile"]
  J --> M["model: runtime, requires, probes<br/>from the module's build file,<br/>application.*, EXPOSE"]
  DF --> M
  M --> CHK{"ServiceDescriptor<br/>+ stack rules"}
  CHK -->|invalid| M
```

- Single-module projects drop `-pl`/`:m:`.
- `${PLACEHOLDER}` values in `application.*` become required env keys; `requires[].capability` must be a capability some template provides.
- An invalid draft is retried once with the validation error, then a tier up; when all tiers fail, escalate `model_unavailable`.
- Save interrupt: `{"type": "save_descriptors", "files": {".cagent/services/<n>.yaml": "<yaml>"}, "resources": [...], "note": str}`; resume `{"saved": true}` or `{"action": "abort"}`. The note says, for example, that Flyway migrations need nothing, and that SQL/CQL scripts and topics are wired up by M7c.
- Read-back validates what the user actually saved, including their edits.

### 5.5 A thread's catalog

State gains `descriptors: {name: yaml_text}`. `Orchestrator._catalog(state)` returns the base catalog plus those descriptors, validated on each read. `generate`, `build`, and `declared_env` use it.

## 6. Testing

| Layer | Covers |
|---|---|
| Go unit | `inspect`: allow-list, nested modules, limits, symlink escape, redaction, outside-root refusal, resource paths; descriptor lookup; `dockerfile` stack validation; shared fixture descriptors |
| Go integration (real Docker) | Two busybox fixture projects, no toolchain needed: a single-module `dockerfile` project, and a two-module project (one deployable, one library) built from `.cagent/services/<name>.yaml` |
| Python unit | Every gap kind × `apply_answer` (valid, invalid, default); extraction schema and unknown names; skeleton rules (Maven single and multi-module, Gradle, Dockerfile, neither); thread catalog after a checkpoint resume |
| Orchestrator (`fakellm` + `simcluster`) | purpose → questions → drafts → save → read-back → healthy; abort; question budget; model failure |
| Real Runner e2e | The two-module fixture: inspect → draft (scripted model) → save → builds and runs |
| Live model (`CAGENT_LLM_E2E`) | One extraction from a purpose sentence; one runtime draft from a Spring Boot fixture `pom.xml` and `application.yml`. A failing check prints the exact prompt first |

Java drafts are checked as descriptors only; this machine cannot run Maven.

## 7. Out of scope

- Init resources (Kafka topics, Cassandra keyspaces and tables, SQL schema scripts), a Cassandra template: M7c.
- Free-text answers mapped by a model, and the chat UI: M8.
- Node and Go builders that generate a Dockerfile; projects without a Dockerfile get a note.
- Topics or schemas defined only in source code: `inspect` does not read source.
