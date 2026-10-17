# Container Agent Design

- Date: 2026-10-17
- Status: design approved; implementation (POC) pending

## 1. Goal

Give developers, testers, and architects an AI agent that builds a docker compose cluster on the user's machine for a stated purpose, fixes it automatically, and stops only when the cluster is fully healthy.

```mermaid
flowchart LR
  A["User states purpose<br/>and services"] --> B["Agent asks about gaps,<br/>proposing defaults"]
  B --> C["User approves<br/>compose YAML"]
  C --> D["Agent builds<br/>and starts cluster"]
  D --> E{"Fully healthy?"}
  E -->|no| F["Agent diagnoses and<br/>fixes the environment"]
  F --> D
  E -->|yes| G["Hand over,<br/>then watch"]
```

Constraints:

- The agent asks about any compose element the user leaves unspecified and proposes a default for each.
- The user must approve the generated compose YAML before anything starts.
- After start-up the agent monitors, diagnoses, and fixes the cluster until it passes layered acceptance.
- The agent covers every company project. A service can come from a released binary, a snapshot, a CI build artifact, or a local working directory.
- Models come from AWS Bedrock or self-hosted open-source models. In fully automatic mode the agent picks a model per subtask by complexity; users can override the choice.
- An internal platform serves the web UI; clusters run on the user's machine.

## 2. Key Decisions

| Question | Decision |
|---|---|
| Delivery | Internal platform serves the web UI; clusters run on the user's machine |
| Platform vs. local split | The platform does the reasoning; a local Runner executes. The Runner dials out and runs only whitelisted tools |
| Project knowledge | Registered `.cagent/service.yaml` first; live inspection as fallback; after a successful run the agent proposes a descriptor |
| Self-healing scope | Change the environment, never source code. Stop with a diagnosis when the root cause is in source |
| Acceptance | Layered checks (L1–L4) plus a stability window |
| Artifact sources | Nexus / Artifactory and CI build artifacts. No prebuilt images exist, so the Runner builds them locally |
| After delivery | Rule-based lightweight watch, cluster management, reusable recipes |
| Architecture | Deterministic state machine with bounded LLM nodes |
| Stack | Platform: Python (LangGraph + LiteLLM). Runner: Go (compose-go + Docker SDK) |
| Model hosting | The POC may use a local Ollama; production routes everything through the LiteLLM gateway |

**Why a state machine instead of a single autonomous agent.** Approval gates, the environment-only rule, budgets, and an LLM-free watch phase are hard rules. Hard rules belong in code, not in prompts. A state machine also confines small models to the narrow tasks they handle well.

**Why Go for the Runner.** `compose-go` is the Compose Spec parser and validator that docker compose itself uses. The Runner validates YAML with Docker's own semantics before start-up, which saves a full "start → fail → ask the LLM" round. Go also cross-compiles to a single binary, which makes distribution simple.

## 3. Architecture

```mermaid
flowchart LR
  subgraph PLATFORM["Internal platform · Python"]
    UI["Web UI<br/>chat · approval · status · logs"]
    ORC["Orchestrator<br/>LangGraph state machine"]
    PG[("Postgres<br/>checkpoints · audit")]
    GW["Model Gateway<br/>LiteLLM"]
    CAT["Catalog Service<br/>descriptors · templates · recipes"]
    HUB["Runner Hub<br/>connections · tool RPC"]
    UI <--> ORC
    ORC --- PG
    ORC --> GW
    ORC --> CAT
    ORC <--> HUB
  end

  subgraph MODELS["Models"]
    BR["AWS Bedrock"]
    OSS["vLLM / Ollama"]
  end

  subgraph LOCAL["User machine"]
    RUN["Runner · single Go binary<br/>tool whitelist · compose-go<br/>builders · watch rules · redaction"]
    DOCK["Docker Engine"]
    CREDS[/"Local credentials<br/>~/.m2 · .npmrc · CI token"/]
    WD[/"Working dirs, read-only"/]
    CREDS -.-> RUN
    WD -.-> RUN
    RUN --> DOCK
  end

  subgraph ARTIFACTS["Artifact sources"]
    NX["Nexus / Artifactory"]
    CIS["Jenkins / GitLab CI"]
  end

  GW --> BR
  GW --> OSS
  RUN ==>|"WSS · outbound only"| HUB
  RUN --> NX
  RUN --> CIS
```

### 3.1 Platform (Python)

- **Web UI**: chat, YAML diff approval, cluster status panel, live logs.
- **Orchestrator**: a LangGraph state machine, one run per cluster task. A Postgres checkpointer persists state, so a run survives a platform restart or a closed browser tab. Approval waits sit at LangGraph interrupts.
- **Model Gateway**: LiteLLM in front of Bedrock and self-hosted models. It handles routing, usage, and cost accounting.
- **Catalog Service**: collects each repository's `.cagent/service.yaml`, maintains middleware templates, and stores shared recipes.
- **Runner Hub**: holds the Runners' WebSocket connections and turns tool calls into RPCs to the right Runner.

### 3.2 User machine (Go Runner)

- Opens no ports. It dials the platform with a platform-issued token bound to the user and the device.
- Runs only a fixed whitelist of tools. It exposes no arbitrary shell.
- Gives each cluster an isolated directory, `~/.cagent/clusters/<name>/`, holding the compose file, generated Dockerfiles, and the tuning log.
- Reads Nexus and CI credentials from the machine (`~/.m2/settings.xml`, `.npmrc`, CI tokens) and never uploads them.

### 3.3 POC vs. production

The Orchestrator talks only to LiteLLM and references tiers, not models. Moving from POC to production changes gateway configuration, not code.

```mermaid
flowchart LR
  ORC["Orchestrator<br/>references tiers only"] --> GW["LiteLLM gateway"]
  GW -->|POC| P1["small → Ollama on the dev machine"]
  GW -->|POC| P2["large → Bedrock"]
  GW -->|Production| R1["small / medium → vLLM on internal GPUs"]
  GW -->|Production| R2["large → Bedrock"]
```

- **POC**: the platform, the Runner, and LiteLLM run together on one dev machine via compose.
- **Production**: the platform runs on internal infrastructure, and Runners connect to it remotely.

## 4. Workflow State Machine

Every phase revolves around a **ClusterSpec**, a structured statement of cluster intent. Conversation fills in the ClusterSpec; the compose YAML is generated from it.

```mermaid
stateDiagram-v2
  direction TB
  state "Clarify" as Clarify
  state "Generate" as Generate
  state "Pre-check" as PreCheck
  state "Approve (human)" as Approve
  state "Prepare" as Prepare
  state "Start" as Start
  state "Verify L1–L4" as Verify
  state "Diagnose & fix" as Fix
  state "Escalate to human" as Escalate
  state "Hand over" as HandOver
  state "Watch" as Watch

  [*] --> Clarify
  Clarify --> Generate: ClusterSpec complete
  Generate --> PreCheck
  PreCheck --> Generate: validation failed
  PreCheck --> Approve
  Approve --> Generate: rejected with comments
  Approve --> Prepare: approved
  Prepare --> Start
  Start --> Verify
  Verify --> HandOver: all layers pass and stable
  Prepare --> Fix: fetch or build failed
  Start --> Fix: start failed
  Verify --> Fix: check failed
  Fix --> Prepare: image or Dockerfile changed
  Fix --> Start: compose or config changed
  Fix --> Approve: patch outside whitelist
  Fix --> Escalate: budget spent, oscillation, or source issue
  Escalate --> Fix: user adds a hint
  HandOver --> Watch
  Watch --> Fix: user re-enters healing
  Watch --> [*]: cluster destroyed
```

| # | Phase | Work | Performed by |
|---|---|---|---|
| 1 | Clarify | Extract the purpose and the service list; tag each service with its source (`release:1.4.2`, `snapshot:2.0-SNAPSHOT`, `workdir:/path`, `ci:job#123`). Ask about each missing required field from the descriptors, one at a time, each with a default | LLM (large) + descriptors |
| 2 | Generate | ClusterSpec → compose YAML, Dockerfiles, build plan. Registered services use templates; the LLM fills in unregistered ones after inspecting them | Templates first, LLM for gaps |
| 3 | Pre-check | compose-go validation; check port conflicts, image architecture, free local resources, security rules | Rules |
| 4 | Approve | Show YAML, Dockerfiles, and build plan. The user approves, edits, or rejects with comments | Human |
| 5 | Prepare | Fetch artifacts and build images | Runner |
| 6 | Start | `docker compose up -d` | Runner |
| 7 | Verify | L1 containers running → L2 healthchecks → L3 smoke probes → L4 user-defined checks → stability window (default: no restarts for 2 minutes) | Rules + probes |
| 8 | Diagnose & fix | Entered on any failure. Rules first, then the LLM | Rules → LLM |
| 9 | Hand over | Output endpoints, credentials, tuning log. Optionally save a recipe or write the fixed configuration back to the descriptor | Templates |
| 10 | Watch | Rule-based monitoring; on anomalies, notify the user, who decides whether to re-enter self-healing | Runner rules |

Automatic fixes return to Prepare or Start, never to Approve — unless a patch falls outside the environment-only whitelist.

## 5. Self-Healing Loop

```mermaid
flowchart TD
  FAIL["Failure detected"] --> SIG["Runner collects signals<br/>state · exit code · events<br/>filtered logs · probes · resources"]
  SIG --> RED["Runner redacts snapshot"]
  RED --> FP["Fingerprint error<br/>e.g. jdbc:connection_refused@mysql"]
  FP --> TOPO{"Upstream service<br/>unhealthy?"}
  TOPO -->|yes| UP["Retarget to upstream;<br/>treat this error as a symptom"]
  UP --> T0
  TOPO -->|no| T0{"T0 rule<br/>matches?"}
  T0 -->|yes| PATCH["Structured patch"]
  T0 -->|no| SCORE["Score complexity"]
  SCORE --> LLM["T1 small model<br/>or T2 large model"]
  LLM --> PATCH
  PATCH --> WL{"Whitelist check"}
  WL -->|touches source| SRC["Stop: source-code diagnosis"]
  WL -->|outside env whitelist| APR["New approval"]
  WL -->|ok| BUD{"Budget left?<br/>Patch hash new?<br/>Not a revert?"}
  BUD -->|no| ESC["Escalate to human"]
  BUD -->|yes| APPLY["Apply, log diff,<br/>rebuild or restart"]
  APPLY --> GONE{"Signature gone?"}
  GONE -->|yes| VER["Back to Verify"]
  GONE -->|no| RAISE["Raise one model tier"]
  RAISE --> SCORE
```

### 5.1 Collect signals

Each round the Runner collects container state, exit codes, restart counts, OOM kills, healthchecks, docker events, the last 200 log lines per service (pre-filtered for ERROR / Exception / FATAL), build logs, probe results, and resource usage. It packs them into a structured, redacted **diagnostic snapshot**. Raw logs are never uploaded in full.

### 5.2 Fingerprint errors

Strip timestamps, container IDs, IPs, port numbers, and similar variables, then compute a signature such as `bind:address_already_in_use`, `jdbc:connection_refused@mysql`, or `oom:exit137@app`.

### 5.3 Tiered handling

| Tier | Handling | Typical problems |
|---|---|---|
| T0 rules | Deterministic fix, no LLM | Host port conflict, exit code 137, dependency not ready (add `depends_on: service_healthy`), image architecture mismatch |
| T1 small model | Problems inside one service | Missing environment variable, wrong config field |
| T2 large model | Cross-service root causes, unseen errors | Kafka `advertised.listeners`, cross-service auth chains |

Fix in dependency order, upstream first. While an upstream service is unhealthy, treat downstream connection errors as symptoms.

### 5.4 Fix format and boundary

Every fix is a structured patch: a JSON Patch against the compose file, or a diff against a config file or Dockerfile. A whitelist validator checks each patch path. A patch that touches a source path stops the loop and produces a "source-code issue" diagnosis.

### 5.5 Budgets and stop conditions

- At most 3 attempts per error signature, 10 rounds overall, and 30 minutes; plus token and cost caps. Each cluster may override these.
- The same patch (by hash) never runs twice. If the signature survives a fix, the node moves up one model tier.
- A patch that reverts an earlier patch counts as oscillation and escalates to a human immediately.

### 5.6 Escalate to a human

The web UI shows the suspected root cause, log evidence, every attempted fix with its result, and suggested next steps. After the user adds a hint, the state machine resumes from where it stopped.

### 5.7 Learn from fixes

```mermaid
flowchart LR
  OK["Successful fix<br/>signature → patch"] --> HIST[("Fix history")]
  HIST --> CHK{"Same patch fixed the same<br/>signature N times across clusters?"}
  CHK -->|yes| CAND["Candidate T0 rule"]
  CAND --> REV["Human review"]
  REV --> LIVE["Live T0 rule"]
  CHK -->|no| HIST
```

## 6. Service Descriptors and Builds

### 6.1 `.cagent/service.yaml`

```yaml
name: order-service
kind: app
stack: java-springboot          # selects the builder template
sources:
  release:  { nexus: "com.acme:order-service:{version}:jar" }
  snapshot: { nexus: "com.acme:order-service:{version}-SNAPSHOT:jar" }
  ci:       { jenkins: "order-service/main", artifact: "target/*.jar" }
  workdir:  { build: "mvn -q package -DskipTests", output: "target/*.jar" }
runtime:
  ports: [{ name: http, container: 8080 }]
  env:
    required: [SPRING_PROFILES_ACTIVE]
    defaults: { JAVA_OPTS: "-Xmx512m" }
  healthcheck: { http: "/actuator/health", expect: 200 }
requires:                        # capabilities needed, not concrete instances
  - capability: mysql
    version: ">=8.0"
    bind: { SPRING_DATASOURCE_URL: "jdbc:mysql://{{host}}:{{port}}/{{db}}" }
  - capability: kafka
    bind: { KAFKA_BOOTSTRAP: "{{host}}:{{port}}" }
probes:                          # L3 smoke probes
  - http: { path: "/api/orders/ping", expect: 200 }
```

Middleware templates use the same format (`kind: middleware`). The platform maintains them; each declares `provides`, supported versions, default configuration, healthchecks, and resource presets (`tiny` / `standard`). The generator matches `requires` against `provides` and renders connection parameters into environment variables:

```mermaid
flowchart LR
  APP["order-service<br/>requires: mysql ≥ 8.0, kafka"]
  MY["mysql 8.0 template<br/>provides: mysql"]
  TI["tidb template<br/>provides: mysql"]
  KF["kafka template<br/>provides: kafka"]
  APP -.->|"mysql · pick one"| MY
  APP -.->|"mysql · pick one"| TI
  APP -.->|kafka| KF
  MY --> ENV["Rendered into app env<br/>SPRING_DATASOURCE_URL=jdbc:mysql://mysql:3306/app"]
```

Swapping MySQL for TiDB changes the middleware, not the application.

### 6.2 From source to image

```mermaid
flowchart LR
  subgraph SOURCES["Service sources"]
    REL["release"]
    SNAP["snapshot"]
    CIA["ci"]
    WDIR["workdir"]
  end
  REL -->|"Nexus, local creds"| ART
  SNAP -->|"resolve timestamped version"| ART
  CIA -->|"CI API, user token"| ART
  WDIR -->|"run build cmd, read output"| ART
  ART["Local artifact + sha256"] --> CACHE{"Tag exists?<br/>cagent/svc:source-sha12"}
  CACHE -->|yes| IMG["Image ready"]
  CACHE -->|no| TPL["Builder template by stack<br/>java-springboot → temurin JRE + jar"]
  TPL --> BUILD["docker build"]
  BUILD --> IMG
```

| Source | Runner action |
|---|---|
| release / snapshot | Download with the local Nexus credentials. Resolve a snapshot to its timestamped version (e.g. `2.0-20261016.031522-7`) and record it in the tuning log so the cluster is reproducible |
| ci | Download the artifact through the Jenkins / GitLab API with the user's CI token |
| workdir | Run the `build` command locally and read the `output` artifact. The working directory is read-only |

- **Image cache**: tag images `cagent/<svc>:<source>-<first 12 chars of artifact sha256>`; skip the build when the artifact is unchanged.
- **Single-service rebuild**: after a code change in a working directory, replace just that service's container in one step.
- **Unregistered projects**: the agent reads pom, gradle, package.json, go.mod, and any existing Dockerfile to draft a descriptor. Once the cluster works, the user can submit the draft to the project repository as a PR.

### 6.3 Middleware templates and ClusterSpec

Middleware templates live in `catalog/middleware/`. Each declares the capability it provides, its supported versions, one container port, the `connection` values a consumer's `bind` templates may use, a healthcheck, and resource presets:

```yaml
name: mysql
kind: middleware
provides: { capability: mysql }
image: "mysql:{version}"
versions: ["8.0", "8.4"]
default_version: "8.4"
port: 3306
connection: { db: app, user: app, password: app }   # throwaway prototype credentials
env:
  MYSQL_DATABASE: "{{db}}"
  MYSQL_USER: "{{user}}"
  MYSQL_PASSWORD: "{{password}}"
  MYSQL_ROOT_PASSWORD: root
healthcheck:
  cmd: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u{{user}} -p{{password}} --silent"]
presets:
  tiny: { mem_limit: 512m, cpus: 0.5 }
  standard: { mem_limit: 1g, cpus: 1.0 }
```

A **ClusterSpec** is what the Clarify phase produces and the Generate phase consumes:

```yaml
name: orders-poc
purpose: Check that order-service stores orders in MySQL and publishes events to Kafka
services:
  - name: order-service
    source: { kind: release, ref: "1.4.2" }   # release | snapshot | ci | workdir
    env: { SPRING_PROFILES_ACTIVE: dev }
    publish: { http: 18080 }                   # port name → host port
middleware:
  - template: mysql
    preset: tiny
    publish: 13306
  - template: kafka
bindings: {}   # e.g. "order-service/mysql": mysql-b when two instances provide mysql
checks:
  - Creating an order returns 201 and the event appears on the orders topic
```

```mermaid
flowchart LR
  SPEC["ClusterSpec"] --> M["Match requires → provides<br/>check version specifiers"]
  CAT["Catalog"] --> M
  M --> R["Render bind templates<br/>host · port · connection"]
  R --> C["compose.yaml"]
  C --> V["cagent-runner validate"]
  M -.->|"missing, ambiguous,<br/>or wrong version"| Q["Problems → Clarify questions"]
```

- Each `{{placeholder}}` in a consumer's `bind` is filled from the chosen provider: `host` (its compose service name), `port`, and its `connection` values.
- Every `$` in a generated string is escaped as `$$`, so compose does not interpolate it.
- App images are tagged `cagent/<service>:<source kind>-<ref>` (a hash for `workdir`); M4 builds them.
- The generator collects every problem instead of stopping at the first, so the Clarify phase can ask about all gaps in one round.

## 7. Model Routing

```mermaid
flowchart LR
  NODE["LLM node<br/>e.g. fix_single_service"] --> PIN{"User pinned a<br/>tier or model?"}
  PIN -->|"yes · cluster > user > team > platform"| TIER["Starting tier"]
  PIN -->|no| SCORE["Rule-based complexity score<br/>services · known signature<br/>unregistered count · prior failures"]
  SCORE --> TIER
  TIER --> CAP{"Model has the<br/>required capability tags?"}
  CAP -->|no| REJ["Reject with explanation"]
  CAP -->|yes| CALL["Call via LiteLLM"]
  CALL --> VAL{"Schema valid and<br/>fix effective?"}
  VAL -->|yes| DONE["Use result"]
  VAL -->|"no · first failure"| CALL
  VAL -->|"no · again"| RAISE["Raise one tier"]
  RAISE --> CAP
```

### 7.1 Three-level mapping

```yaml
tiers:                     # tier → model; varies by environment
  small:  ollama/qwen2.5-coder:7b
  medium: vllm/qwen2.5-32b-instruct
  large:  bedrock/claude-sonnet
nodes:                     # node → default tier
  clarify: large
  generate_unregistered: medium
  log_classify: small
  fix_single_service: small
  fix_cross_service: large
  handover_summary: small
```

Node code references tier names, never model names.

### 7.2 Automatic selection

Rules score complexity; the LLM never rates itself. The score sets the starting tier; a failed fix or a schema-invalid output moves the node up one tier. Pydantic schemas validate every LLM output: retry once on failure, then escalate.

### 7.3 Capability tags

Each model carries tags such as `tool_use`, `json_mode`, and `context`. The router refuses to assign a tool-calling node to a model without `tool_use` and explains why.

### 7.4 User overrides

Override order: platform default < team < user < this cluster. The web UI offers presets ("large model everywhere", "local models only") and per-node pinning.

## 8. Security

The sequence below shows one approved tool call and where each trust boundary sits.

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant W as Web UI
  participant O as Orchestrator
  participant H as Runner Hub
  participant R as Runner
  participant D as Docker
  participant M as LiteLLM
  R->>H: Dial out over WSS with user and device token
  U->>W: Approve compose YAML
  W->>O: Resume run from interrupt
  O->>H: Tool call compose_up
  H->>R: RPC compose_up
  R->>R: Check args against whitelist and cagent.managed label
  R->>D: docker compose up -d
  D-->>R: Status, events, logs
  R->>R: Filter logs and redact snapshot
  R-->>H: Redacted diagnostic snapshot
  H-->>O: Snapshot
  O->>M: Diagnose using redacted snapshot only
  M-->>O: Structured patch
  Note over R,D: Credentials and raw logs never leave the user machine
```

- **Runner authentication**: the user signs in to the web UI via SSO and registers the device, receiving a revocable token bound to user and device. Connections use WSS.
- **Tool argument checks**: file access stays inside `~/.cagent/`; declared working directories are read-only. Docker operations touch only compose projects labelled `cagent.managed=true`.
- **Compose security lint**: the Runner rejects every setting in the table below by default. The user can allow each one individually during approval, as `service:rule` (for example `web:privileged`). For a top-level volume the name before the colon is the volume name (for example `data:volume-driver`).

  | Rule ID | What it catches |
  | --- | --- |
  | `privileged` | `privileged: true` |
  | `host-network` | `network_mode: host` |
  | `pid-host` | `pid: host`, which exposes host processes |
  | `ipc-host` | `ipc: host`, which shares host IPC and shared memory |
  | `userns-host` | `userns_mode: host`, which turns off user-namespace remapping |
  | `cap-add` | any `cap_add` entry |
  | `devices` | any `devices` entry, which maps host devices into the container |
  | `security-opt` | `seccomp` or `apparmor` set to `unconfined`, `label=disable` (or `label:disable`), `no-new-privileges:false` |
  | `cgroup-parent` | any `cgroup_parent`, which places the container in a host cgroup |
  | `docker-sock` | a mount of the Docker socket, or of a directory that contains it (such as `/var/run` or `/run`) |
  | `host-mount` | a bind mount, or a local volume with `driver_opts` `device` plus `o: bind` (or `type: none`), whose source is outside the workspace |
  | `volume-driver` | a top-level volume with any driver other than `local`, because its options are opaque to the Runner; or a `local` volume whose `device` is not a bind path, a network share (`nfs`, `nfs4`, `cifs`), or `tmpfs`, such as `type: ext4` on `/dev/sda1` |

  Mount sources are checked after symlink resolution, and the check fails closed: a source that cannot be resolved, including a dangling symlink, is reported as `host-mount`. The finding shows the path as declared in the compose file.

```mermaid
flowchart TD
  S["Bind source, or volume driver_opts device"] --> L{"Declared path ends<br/>in docker.sock?"}
  L -->|yes| DS["docker-sock"]
  L -->|no| R{"Resolve symlinks"}
  R -->|"error or dangling link"| HM1["host-mount (fail closed)"]
  R -->|resolved| D{"Resolved path is a Docker socket<br/>or contains one?"}
  D -->|yes| DS
  D -->|no| W{"Inside the workspace<br/>(also resolved)?"}
  W -->|no| HM2["host-mount"]
  W -->|yes| OK["Allowed"]
```

- **Reviewed-field allow-list**: the deny-list above names known escape vectors; the allow-list closes the rest. `runner/internal/compose/allowed_fields.json` lists the top-level and service keys the generator emits or that cannot weaken isolation. `validate` checks the keys in effect after YAML anchors, `extends`, and `include` are applied, and reports every other key as `field:<key>`, approvable one key at a time (`web:field:sysctls`, or `*:field:configs` for a top-level key). A key that already triggered a specific rule above is not reported twice. Keys starting with `x-` are compose extensions and always allowed. The platform's tests read the same file, so the generator cannot emit a key `validate` would reject.
- **Credentials and redaction**: credentials never leave the machine. The Runner redacts diagnostic snapshots (passwords, tokens, AK/SK, passwords inside JDBC URLs) before upload, so neither the platform nor any model sees raw logs.
- **Audit**: the platform logs every tool call, patch, and approval, replayable per cluster.

## 9. Testing Strategy

```mermaid
flowchart LR
  LIB[("Fault-scenario library")] --> RUNALL["Run every scenario<br/>on every tier"]
  RUNALL --> MET["Fix rate · rounds · cost"]
  MET --> ROUTE["Tune default routing table"]
  NEW["New real-world failure"] --> LIB
```

| Layer | Scope | Method |
|---|---|---|
| Unit | Runner: compose-go validation, whitelist, redaction, fingerprinting. Platform: ClusterSpec → compose generation, complexity scoring, budgets and oscillation detection | Go table-driven tests; golden files for generators |
| LLM nodes | Output structure and decisions of clarify, generate, diagnose | Record/replay in CI; nightly run against live models |
| Fault-scenario library | Prebuilt broken clusters: port conflict, OOM, missing env var, MySQL not ready, misconfigured Kafka listeners, architecture mismatch, config typos, and more. Each scenario records its root-cause category and maximum rounds | Run every scenario on every tier; track fix rate, rounds, and cost; tune default routing from the results |
| End-to-end | The agent deploys its own platform, plus 2–3 real company projects | Nightly |

## 10. POC Scope

```mermaid
mindmap
  root((POC))
    In scope
      Single machine via compose
      Ollama plus Bedrock
      Web chat, diff approval, status, logs
      State machine steps 1 to 9
      Budgets, oscillation detection, escalation
      MySQL, Redis, Kafka templates
      Java Spring Boot builder
      Nexus release and snapshot, workdir
      About 10 T0 rules plus T1 and T2 fixes
      Acceptance L1 to L4
    Out of scope
      CI artifact source
      Recipe sharing
      Git webhook descriptor collection
      Descriptor write-back PRs
      External watch notifications
      Team-level overrides
      SSO, static token instead
      Rule mining from fix history
```

The POC reads descriptors from a local catalog directory instead of collecting them via Git webhooks.

**Success criteria:**

- At least 80% of fault scenarios fixed automatically within budget
- 2 real projects running end to end
- Registered projects go from prompt to healthy cluster in under 15 minutes
