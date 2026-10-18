# M7b Clarify & Project Inspection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn a sentence into a valid ClusterSpec by asking one plain question per generator gap, and give an unregistered project — single- or multi-module — a descriptor the user saves, so it builds and runs like a registered one.

**Architecture:** The Runner gains a read-only `inspect` tool (fixed file names, Maven/Gradle modules, paths confined, contents redacted), per-service descriptors in `.cagent/services/<name>.yaml`, and a `dockerfile` stack that builds the project's own Dockerfile. The platform's generator reports structured gaps; `clarify` extracts a draft spec with one model call and closes each gap with a deterministic question; `draft` combines a code-built skeleton with a model-filled runtime that code checks. The orchestrator adds `extract`, `ask`, `inspect`, `save`, and `readback` before `generate`, and keeps the descriptors the user saved in the checkpoint.

**Tech Stack:** Go (compose-go, `encoding/xml`), Python (`langgraph`, Pydantic, `litellm` 1.106), local Ollama (`qwen3.5:latest` as the top tier).

- Design: [2026-10-18-m7b-clarify-inspection-design.md](2026-10-18-m7b-clarify-inspection-design.md) — this plan implements it; Task 15 records what changed while planning
- Roadmap: [2026-10-17-poc-roadmap.md](2026-10-17-poc-roadmap.md), M7b (init resources are M7c)

## Decisions made while planning

| Decision | Why |
|---|---|
| The user saves drafted descriptors; the Runner gets no write tool (user decision) | The workdir invariant stays whole: the Runner runs only a build the project describes itself. |
| One extraction call, then deterministic questions (user decision) | No model reads the user's answers; every question has options and a default from the catalog. |
| Stacks `java-springboot` and `dockerfile`; multi-module in M7b; `.cagent/services/<name>.yaml` at the root (user decisions) | Builds run at the root, outputs stay inside it, so path confinement does not change. |
| Init resources (topics, keyspaces, schema scripts) move to M7c (user decision) | They help registered projects too; M7b only reports the files it finds. |
| Interrupt payloads use `kind` (`question`, `save_descriptors`), and `start_from_purpose` is a new method | Matches the existing `approval`/`escalation` interrupts; `start(thread_id, spec, runner)` keeps its signature. |
| Every generator problem is a gap kind, including unsupported versions, presets, publish ports, and bindings | The design table named six; the generator has nine answerable shapes. A choice with no options escalates. |
| A workdir build uses the project's stack; a different `-stack` is an error | Before, the platform's `-stack` won. The platform names a directory, not how it is built. |
| Redaction keeps `{{template}}` and `${VAR}` references, masks a reference's default | Found while implementing: masking them made a read-back bind template `******`, and hid which variable holds a password. |
| A read-back descriptor holding `******` is refused | A literal secret cannot survive redaction; the user moves it to a bind or removes it. |
| The model answers a flat draft schema; code builds the descriptor | Found on `qwen3.5`: a JSON schema cannot say "exactly one of http or cmd", and the model filled both. |
| Code checks every draft: capabilities exist, bind keys are env names using only the provider's values, every placeholder is set, no default points at `localhost`; a bound variable's default is dropped | Found on `qwen3.5`: bind keyed by `host`/`port`, and the config's `localhost` default copied next to the bind. |
| A "service" named like a middleware template becomes middleware | Found on `qwen3.5` without thinking: MySQL listed as a service with a release source. |
| Extraction and drafting run with thinking off; diagnosis unchanged | Measured below: minutes and a failed draft with thinking, seconds and correct answers without. |
| A project that already has a valid descriptor is used as is | No model, no save step: inspection reads it back directly. |

## Measurements (local Ollama, 2026-10-18)

`qwen3.5:latest` (the top tier in `platform/models.yaml`) on a 24 GB Apple Silicon Mac. Extraction: the purpose sentence in Task 14. Drafting: the Spring Boot fixture `orders-api` (MySQL and Kafka). "Correct" means the checks in Task 14 pass.

| Thinking | Prompts | Extraction | Drafting |
|---|---|---|---|
| on (model default), run 1 | final | correct, 167 s | failed after 186 s: HTTP healthcheck but no ports |
| on (model default), run 2 | final | correct, 146 s | failed after 214 s: same |
| off | before the fixes in this plan | wrong: MySQL listed as a service | wrong: `bind` keyed by `host`/`port` |
| off, 3 runs | final | correct, 6.6–14.4 s (first run loads the model) | correct, 6.9–8.4 s |

A trivial call took 10 s and 372 tokens with thinking on, 0.28 s with it off.


## How a purpose becomes a running cluster

```mermaid
flowchart TD
  P["purpose"] --> X["extract<br/>top tier, thinking off"]
  X --> G["generate"]
  S["spec"] --> G
  G -->|ok| V["validate → approve ⏸ → build → up → …"]
  G -->|"answerable gap"| A["ask ⏸<br/>options · default · error"]
  A -->|"valid answer"| G
  A -->|invalid| A
  G -->|"unregistered workdir service"| I["inspect (Runner)"]
  I -->|"descriptor on disk"| G
  I -->|none| D["draft: skeleton + checked runtime"]
  D --> SV["save ⏸"]
  SV --> RB["readback (Runner)"]
  RB -->|ok| G
  RB -->|"missing or invalid"| SV
  G -->|"other gaps"| E["escalate ⏸ clarify"]
```

## Prerequisites

- Work on a feature branch from `main`, in its own worktree.
- **Go is not installed on the host.** Every Go command runs in `golang:1.24`, as written in each step (the `cagent-gomod`/`cagent-gocache` volumes only cache downloads and builds).
- **Docker** running, for Tasks 6 and 13.
- **Ollama** at `localhost:11434` serving `qwen3.5:latest` — only for Task 14. Everything else uses a scripted fake model.
- Baseline on `main`: `make platform-test` → `188 passed, 4 skipped`; `make runner-test` (in the container) → every package `ok`.

---

### Task 1: Per-service descriptors and the `dockerfile` stack in `workdir`

A project root may hold `.cagent/services/<service>.yaml` (one per deployable module) or the single `.cagent/service.yaml`. A `dockerfile`-stack source names the project's own Dockerfile and build context; both must stay inside the project, symlinks included.

**Files:**
- Create: `platform/tests/fixtures/descriptors/{valid,invalid}/*.yaml` (shared with Task 7)
- Modify (full file below): `runner/internal/workdir/workdir.go`, `runner/internal/workdir/workdir_test.go`
- Modify: `runner/cmd/cagent-runner/main.go`, `runner/test/integration/build_test.go` (one call each)

**Step 1: Add the shared descriptor fixtures**

The Runner (here) and the platform (Task 7) both validate these, so the two sides agree on which workdir sources are valid.

`platform/tests/fixtures/descriptors/valid/java.yaml`:

```yaml
name: demo
kind: app
stack: java-springboot
sources:
  workdir: { build: "mvn -q package -DskipTests", output: "target/*.jar" }
runtime: {}
```

`platform/tests/fixtures/descriptors/valid/dockerfile.yaml`:

```yaml
name: demo
kind: app
stack: dockerfile
sources:
  workdir: { dockerfile: Dockerfile }
runtime: {}
```

`platform/tests/fixtures/descriptors/valid/dockerfile-module.yaml`:

```yaml
name: demo
kind: app
stack: dockerfile
sources:
  workdir:
    build: "mvn -q -pl api -am package -DskipTests"
    dockerfile: api/Dockerfile
    context: api
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/java-no-output.yaml`:

```yaml
name: demo
kind: app
stack: java-springboot
sources:
  workdir: { build: "mvn -q package" }
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/dockerfile-missing.yaml`:

```yaml
name: demo
kind: app
stack: dockerfile
sources:
  workdir: { build: "make" }
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/dockerfile-escapes.yaml`:

```yaml
name: demo
kind: app
stack: dockerfile
sources:
  workdir: { dockerfile: ../other/Dockerfile }
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/context-absolute.yaml`:

```yaml
name: demo
kind: app
stack: dockerfile
sources:
  workdir: { dockerfile: Dockerfile, context: /etc }
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/unknown-stack.yaml`:

```yaml
name: demo
kind: app
stack: node
sources:
  workdir: { build: "npm ci", output: "dist/app.js" }
runtime: {}
```

`platform/tests/fixtures/descriptors/invalid/java-with-dockerfile.yaml`:

```yaml
name: demo
kind: app
stack: java-springboot
sources:
  workdir: { build: "mvn -q package", output: "target/*.jar", dockerfile: Dockerfile }
runtime: {}
```

**Step 2: Write the failing tests — replace `workdir_test.go`**

Write `runner/internal/workdir/workdir_test.go`:

```go
package workdir

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// project creates a working directory with a descriptor using build and output.
func project(t *testing.T, build, output string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	desc := "name: demo\nkind: app\nstack: java-springboot\nsources:\n  workdir:\n" +
		"    build: '" + build + "'\n    output: '" + output + "'\nruntime: {}\n"
	if err := os.WriteFile(filepath.Join(dir, DescriptorPath), []byte(desc), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func build(t *testing.T, dir string) (string, error) {
	t.Helper()
	d, err := LoadDescriptor(dir, "demo")
	if err != nil {
		t.Fatal(err)
	}
	return Build(context.Background(), dir, d)
}

func TestBuildReturnsTheArtifact(t *testing.T) {
	dir := project(t, "mkdir -p target && echo jar > target/demo.jar && echo x > target/demo.jar.original", "target/*.jar")
	got, err := build(t, dir)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(got) != "demo.jar" {
		t.Errorf("artifact = %s", got)
	}
}

func TestBuildFailureCarriesRedactedLog(t *testing.T) {
	dir := project(t, "echo compiling; echo password=hunter2; echo BUILD FAILURE; exit 1", "target/*.jar")
	_, err := build(t, dir)
	var be *BuildError
	if !errors.As(err, &be) {
		t.Fatalf("got %v, want BuildError", err)
	}
	log := strings.Join(be.Log, "\n")
	if strings.Contains(log, "hunter2") || !strings.Contains(log, "BUILD FAILURE") {
		t.Errorf("log = %q", log)
	}
}

func TestBuildNeedsExactlyOneArtifact(t *testing.T) {
	dir := project(t, "mkdir -p target && touch target/a.jar target/a-sources.jar", "target/*.jar")
	if _, err := build(t, dir); err == nil || !strings.Contains(err.Error(), "matched 2 files") {
		t.Errorf("got %v", err)
	}
}

func TestBuildOutputMustStayInside(t *testing.T) {
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "evil.jar"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	dir := project(t, "mkdir -p target && ln -sf "+outside+"/evil.jar target/app.jar", "target/*.jar")
	if _, err := build(t, dir); err == nil || !strings.Contains(err.Error(), "resolves outside") {
		t.Errorf("symlinked artifact: got %v", err)
	}
}

func TestLoadDescriptorNeedsWorkdirSource(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	body := "name: demo\nsources:\n  release: { nexus: 'a:b:{version}:jar' }\n"
	if err := os.WriteFile(filepath.Join(dir, DescriptorPath), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadDescriptor(dir, "demo"); err == nil || !strings.Contains(err.Error(), "sources.workdir is missing") {
		t.Errorf("got %v", err)
	}
	if _, err := LoadDescriptor(t.TempDir(), "demo"); err == nil {
		t.Error("missing descriptor accepted")
	}
}

// write creates dir/rel with body, making parent directories.
func write(t *testing.T, dir, rel, body string) {
	t.Helper()
	path := filepath.Join(dir, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLoadDescriptorPrefersThePerServiceFile(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, DescriptorPath, "name: other\nstack: dockerfile\nsources:\n  workdir: { dockerfile: Dockerfile }\n")
	write(t, dir, ServicesDir+"/api.yaml", "name: api\nstack: dockerfile\nsources:\n  workdir: { dockerfile: api/Dockerfile }\n")
	d, err := LoadDescriptor(dir, "api")
	if err != nil || d.Sources.Workdir.Dockerfile != "api/Dockerfile" {
		t.Fatalf("got %+v, %v", d, err)
	}
	// Without a per-service file the project's single descriptor is used, and must name the service.
	if _, err := LoadDescriptor(dir, "web"); err == nil || !strings.Contains(err.Error(), `names "other", not "web"`) {
		t.Errorf("got %v", err)
	}
}

func TestLoadDescriptorRejectsBadServiceNames(t *testing.T) {
	for _, svc := range []string{"", "../x", "a/b", "Api"} {
		if _, err := LoadDescriptor(t.TempDir(), svc); err == nil || !strings.Contains(err.Error(), "invalid service") {
			t.Errorf("%q: got %v", svc, err)
		}
	}
}

// The same fixture descriptors are checked by the platform's ServiceDescriptor tests, so the
// Runner and the platform agree on which workdir sources are valid.
func TestSharedDescriptorFixtures(t *testing.T) {
	root := filepath.Join("..", "..", "..", "platform", "tests", "fixtures", "descriptors")
	for _, kind := range []string{"valid", "invalid"} {
		paths, err := filepath.Glob(filepath.Join(root, kind, "*.yaml"))
		if err != nil || len(paths) == 0 {
			t.Fatalf("no %s fixtures: %v", kind, err)
		}
		for _, path := range paths {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			_, err = ParseDescriptor(data, "demo")
			if (err == nil) != (kind == "valid") {
				t.Errorf("%s: err = %v", path, err)
			}
		}
	}
}

func TestDockerfileStackRunsTheOptionalBuildFirst(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "api/Dockerfile", "FROM busybox\n")
	write(t, dir, DescriptorPath, "name: demo\nstack: dockerfile\nsources:\n  workdir:\n"+
		"    build: 'mkdir -p api/out && touch api/out/built'\n    dockerfile: api/Dockerfile\n    context: api\n")
	d, err := LoadDescriptor(dir, "demo")
	if err != nil {
		t.Fatal(err)
	}
	if artifact, err := Build(context.Background(), dir, d); err != nil || artifact != "" {
		t.Fatalf("artifact %q, err %v", artifact, err)
	}
	if _, err := os.Stat(filepath.Join(dir, "api", "out", "built")); err != nil {
		t.Error("build command did not run")
	}
	df, ctxDir, err := DockerInputs(dir, d)
	if err != nil {
		t.Fatal(err)
	}
	real, _ := filepath.EvalSymlinks(dir)
	if df != filepath.Join(real, "api", "Dockerfile") || ctxDir != filepath.Join(real, "api") {
		t.Errorf("dockerfile %s, context %s", df, ctxDir)
	}
}

func TestDockerInputsStayInsideTheProject(t *testing.T) {
	outside := t.TempDir()
	write(t, outside, "Dockerfile", "FROM busybox\n")
	dir := t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "Dockerfile"), filepath.Join(dir, "Dockerfile")); err != nil {
		t.Fatal(err)
	}
	d := Descriptor{Name: "demo", Stack: StackDockerfile}
	d.Sources.Workdir = &Workdir{Dockerfile: "Dockerfile", Context: "."}
	if _, _, err := DockerInputs(dir, d); err == nil || !strings.Contains(err.Error(), "outside") {
		t.Errorf("symlinked Dockerfile: got %v", err)
	}
	d.Sources.Workdir.Dockerfile = "missing/Dockerfile"
	if _, _, err := DockerInputs(dir, d); err == nil {
		t.Error("missing Dockerfile accepted")
	}
}
```

**Step 3: Run them to verify they fail**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 go vet ./internal/workdir/
```

Expected: FAIL — `too many arguments in call to LoadDescriptor`.

**Step 4: Implement — replace `workdir.go`**

Write `runner/internal/workdir/workdir.go`:

```go
// Package workdir builds a project in the developer's working directory with their own
// toolchain. The build command, Dockerfile, and output come only from the project's own
// descriptor in that directory, never from the platform: the platform can name a directory
// and a service, but it cannot make the Runner run a command the developer did not write.
package workdir

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go.yaml.in/yaml/v3"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/redact"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// DescriptorPath is a single-service project's descriptor, relative to its root.
const DescriptorPath = ".cagent/service.yaml"

// ServicesDir holds one descriptor per service, <service>.yaml, for projects with several
// deployable modules. A service's own file wins over DescriptorPath.
const ServicesDir = ".cagent/services"

// LogTail is how many redacted lines of a failed build's output are reported.
const LogTail = 40

// Stacks a workdir source can use.
const (
	StackJavaSpringBoot = "java-springboot" // build produces a jar; the Runner's template images it
	StackDockerfile     = "dockerfile"      // the project's own Dockerfile builds the image
)

// Workdir is a descriptor's sources.workdir.
type Workdir struct {
	Build      string `yaml:"build"`
	Output     string `yaml:"output"`
	Dockerfile string `yaml:"dockerfile"`
	Context    string `yaml:"context"`
}

// Descriptor is the part of a service descriptor the Runner needs. The platform validates
// the full schema with the same workdir rules; unknown keys are ignored here.
type Descriptor struct {
	Name    string `yaml:"name"`
	Stack   string `yaml:"stack"`
	Sources struct {
		Workdir *Workdir `yaml:"workdir"`
	} `yaml:"sources"`
}

// LoadDescriptor reads service's descriptor from dir: ServicesDir/<service>.yaml when it
// exists, else DescriptorPath. The descriptor must name the service and declare a valid
// workdir source.
func LoadDescriptor(dir, service string) (Descriptor, error) {
	if !workspace.ValidName(service) {
		return Descriptor{}, fmt.Errorf("invalid service name %q", service)
	}
	path := filepath.Join(dir, ServicesDir, service+".yaml")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		path = filepath.Join(dir, DescriptorPath)
		data, err = os.ReadFile(path)
	}
	if err != nil {
		return Descriptor{}, err
	}
	d, err := ParseDescriptor(data, service)
	if err != nil {
		return Descriptor{}, fmt.Errorf("%s: %w", path, err)
	}
	return d, nil
}

// ParseDescriptor parses a descriptor and checks it names service and declares a workdir
// source its stack can build. An empty stack means java-springboot.
func ParseDescriptor(data []byte, service string) (Descriptor, error) {
	var d Descriptor
	if err := yaml.Unmarshal(data, &d); err != nil {
		return Descriptor{}, fmt.Errorf("parse: %w", err)
	}
	if d.Name != service {
		return Descriptor{}, fmt.Errorf("descriptor names %q, not %q", d.Name, service)
	}
	if d.Stack == "" {
		d.Stack = StackJavaSpringBoot
	}
	w := d.Sources.Workdir
	switch {
	case w == nil:
		return Descriptor{}, errors.New("sources.workdir is missing")
	case d.Stack == StackJavaSpringBoot:
		if w.Build == "" || w.Output == "" {
			return Descriptor{}, errors.New("sources.workdir needs build and output")
		}
		if w.Dockerfile != "" || w.Context != "" {
			return Descriptor{}, errors.New("sources.workdir: dockerfile and context need stack dockerfile")
		}
	case d.Stack == StackDockerfile:
		if w.Dockerfile == "" {
			return Descriptor{}, errors.New("sources.workdir needs dockerfile")
		}
		if w.Context == "" {
			w.Context = "."
		}
		for _, p := range []string{w.Dockerfile, w.Context} {
			if !Relative(p) {
				return Descriptor{}, fmt.Errorf("sources.workdir: %q must be a relative path inside the project", p)
			}
		}
	default:
		return Descriptor{}, fmt.Errorf("unsupported stack %q", d.Stack)
	}
	return d, nil
}

// Relative reports whether p is a relative path that stays inside its base lexically.
// Symlinks are checked when the path is used.
func Relative(p string) bool {
	if p == "" || filepath.IsAbs(p) {
		return false
	}
	c := filepath.Clean(p)
	return c != ".." && !strings.HasPrefix(c, ".."+string(filepath.Separator))
}

// DockerInputs returns the resolved Dockerfile and build context of a dockerfile-stack
// descriptor. Both must resolve inside dir: a symlink cannot point the build elsewhere.
func DockerInputs(dir string, d Descriptor) (dockerfile, contextDir string, err error) {
	root, err := guard.Resolve(dir)
	if err != nil {
		return "", "", err
	}
	w := d.Sources.Workdir
	resolve := func(rel string) (string, error) {
		p, err := guard.Resolve(filepath.Join(root, rel))
		if err != nil {
			return "", err
		}
		if !guard.Within(root, p) {
			return "", fmt.Errorf("%s resolves outside %s", rel, dir)
		}
		return p, nil
	}
	if dockerfile, err = resolve(w.Dockerfile); err != nil {
		return "", "", err
	}
	if fi, err := os.Stat(dockerfile); err != nil || !fi.Mode().IsRegular() {
		return "", "", fmt.Errorf("dockerfile %s is not a regular file", w.Dockerfile)
	}
	if contextDir, err = resolve(w.Context); err != nil {
		return "", "", err
	}
	if fi, err := os.Stat(contextDir); err != nil || !fi.IsDir() {
		return "", "", fmt.Errorf("context %s is not a directory", w.Context)
	}
	return dockerfile, contextDir, nil
}

// BuildError is a failed build with the redacted tail of its output.
type BuildError struct {
	Err error
	Log []string
}

func (e *BuildError) Error() string {
	return fmt.Sprintf("build failed: %v\n%s", e.Err, strings.Join(e.Log, "\n"))
}

func (e *BuildError) Unwrap() error { return e.Err }

// Build runs the descriptor's build command in dir, if it has one. For java-springboot it
// returns the single artifact the output glob matches, which must resolve inside dir; for
// dockerfile there is no artifact and it returns "".
func Build(ctx context.Context, dir string, d Descriptor) (string, error) {
	w := d.Sources.Workdir
	if w.Build != "" {
		cmd := exec.CommandContext(ctx, "sh", "-c", w.Build)
		cmd.Dir = dir
		out, err := cmd.CombinedOutput()
		if err != nil {
			return "", &BuildError{Err: err, Log: tail(out)}
		}
	}
	if d.Stack == StackDockerfile {
		return "", nil
	}
	matches, err := filepath.Glob(filepath.Join(dir, w.Output))
	if err != nil {
		return "", err
	}
	if len(matches) != 1 {
		return "", fmt.Errorf("output %q matched %d files, want exactly 1: %v", w.Output, len(matches), matches)
	}
	root, err := guard.Resolve(dir)
	if err != nil {
		return "", err
	}
	artifact, err := guard.Resolve(matches[0])
	if err != nil {
		return "", err
	}
	if !guard.Within(root, artifact) {
		return "", fmt.Errorf("output %s resolves outside %s", matches[0], dir)
	}
	if fi, err := os.Stat(artifact); err != nil || !fi.Mode().IsRegular() {
		return "", fmt.Errorf("output %s is not a regular file", matches[0])
	}
	return artifact, nil
}

// tail returns the last LogTail non-empty lines of out, redacted.
func tail(out []byte) []string {
	var lines []string
	for _, l := range bytes.Split(out, []byte("\n")) {
		if s := strings.TrimRight(string(l), "\r "); s != "" {
			lines = append(lines, redact.String(s))
		}
	}
	if len(lines) > LogTail {
		lines = lines[len(lines)-LogTail:]
	}
	return lines
}
```

**Step 5: Update the two callers**

In `runner/cmd/cagent-runner/main.go` (`runBuild`), replace:

```go
		desc, err := workdir.LoadDescriptor(abs)
```

with:

```go
		desc, err := workdir.LoadDescriptor(abs, *service)
```

In `runner/test/integration/build_test.go`, replace:

```go
	dl, err := workdir.LoadDescriptor(proj)
```

with:

```go
	dl, err := workdir.LoadDescriptor(proj, "demo")
```

**Step 6: Run all Go tests, and vet the integration tests**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 sh -c 'test -z "$(gofmt -l .)" && go vet ./... && go vet -tags integration ./... && go test ./...'
```

Expected: every package `ok`, no gofmt output.

**Step 7: Commit**

```bash
git add platform/tests/fixtures/descriptors runner/internal/workdir runner/cmd/cagent-runner/main.go runner/test/integration/build_test.go
git commit -m "feat(runner): per-service descriptors and the dockerfile stack in workdir"
```

---

### Task 2: Build from the project's Dockerfile; the project decides the stack

Before, `-stack` from the platform overrode the descriptor's. Now a workdir build uses the project's stack, and a different `-stack` is an error: the platform can name a directory, not choose how it is built.

**Files:**
- Modify: `runner/internal/builder/builder.go`, `runner/internal/builder/builder_test.go`
- Modify: `runner/cmd/cagent-runner/main.go`, `runner/cmd/cagent-runner/build_test.go`

**Step 1: Write the failing tests**

In `runner/internal/builder/builder_test.go`, add two tests above `TestBuildReusesImageWithSameArtifact` — replace:

```go

func TestBuildReusesImageWithSameArtifact(t *testing.T) {
```

with:

```go

func TestBuildDockerfileUsesTheProjectsDockerfile(t *testing.T) {
	cmd := "build -t " + tag + " --label cagent.managed=true -f /p/api/Dockerfile /p/api"
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{cmd: {}}}
	if err := BuildDockerfile(context.Background(), f, tag, "/p/api/Dockerfile", "/p/api"); err != nil {
		t.Fatal(err)
	}
	if len(f.Calls) != 1 {
		t.Errorf("calls %v", f.Calls)
	}
}

func TestBuildDockerfileFailureCarriesRedactedLog(t *testing.T) {
	cmd := "build -t " + tag + " --label cagent.managed=true -f /p/Dockerfile /p"
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		cmd: {Out: "step 2\npassword=hunter2\nERROR: failed to solve", Err: errors.New("exit status 1")},
	}}
	err := BuildDockerfile(context.Background(), f, tag, "/p/Dockerfile", "/p")
	var be *BuildError
	if !errors.As(err, &be) || strings.Contains(strings.Join(be.Log, "\n"), "hunter2") {
		t.Fatalf("got %v", err)
	}
	if err := BuildDockerfile(context.Background(), f, "nginx:latest", "/p/Dockerfile", "/p"); err == nil {
		t.Error("tag outside cagent/ accepted")
	}
}

func TestBuildReusesImageWithSameArtifact(t *testing.T) {
```

In `runner/cmd/cagent-runner/build_test.go`, add the `strings` import:

```go
	"path/filepath"
	"testing"
```

with:

```go
	"path/filepath"
	"strings"
	"testing"
```

and insert the helper and two tests above `TestBuildArgumentErrors` — replace:

```go
func TestBuildArgumentErrors(t *testing.T) {
```

with:

```go
// project writes files (relative path -> content) into a fresh working directory.
func project(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		path := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

func TestBuildFromWorkdirWithTheProjectsDockerfile(t *testing.T) {
	proj := project(t, map[string]string{
		"api/Dockerfile": "FROM busybox\n",
		".cagent/services/web.yaml": "name: web\nstack: dockerfile\nsources:\n  workdir:\n" +
			"    build: 'touch api/built'\n    dockerfile: api/Dockerfile\n    context: api\n",
	})
	d, f, _ := buildDeps(t)
	cmd := "build -t " + webTag + " --label cagent.managed=true -f " + filepath.Join(proj, "api", "Dockerfile") + " " + filepath.Join(proj, "api")
	f.Responses[cmd] = dockercli.FakeResponse{}
	code, res, stderr := runBuildCmd(t, d, "-workdir", proj, "-stack", "dockerfile")
	if code != exitOK || res.Source != "workdir:"+proj || res.SHA256 != "" || res.Cached {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
	if _, err := os.Stat(filepath.Join(proj, "api", "built")); err != nil {
		t.Error("the descriptor's build command did not run first")
	}
}

func TestBuildRefusesAStackOtherThanTheProjects(t *testing.T) {
	proj := project(t, map[string]string{
		"Dockerfile":           "FROM busybox\n",
		".cagent/service.yaml": "name: web\nstack: java-springboot\nsources:\n  workdir: { build: 'true', output: 'x.jar' }\n",
	})
	d, f, _ := buildDeps(t)
	code, _, stderr := runBuildCmd(t, d, "-workdir", proj, "-stack", "dockerfile")
	if code != exitError || !strings.Contains(stderr, `stack "dockerfile" differs from the project's descriptor ("java-springboot")`) {
		t.Fatalf("exit %d, stderr %s", code, stderr)
	}
	if len(f.Calls) != 0 {
		t.Errorf("docker called: %v", f.Calls)
	}
}

func TestBuildArgumentErrors(t *testing.T) {
```

**Step 2: Run them to verify they fail**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 go vet ./internal/builder/ ./cmd/...
```

Expected: FAIL — `undefined: BuildDockerfile`.

**Step 3: Implement**

In `runner/internal/builder/builder.go`, replace the package comment:

```go
// Package builder turns a local artifact into a labelled image with a stack template.
```

with:

```go
// Package builder turns a local artifact into a labelled image with a stack template, or
// builds a project's own Dockerfile (stack dockerfile).
```

and add `BuildDockerfile` above `tail` — replace:

```go
func tail(out []byte, n int) []string {
```

with:

```go
// BuildDockerfile builds tag from a project's own Dockerfile and context, which the caller
// has confined to the project. Docker's layer cache decides what is rebuilt.
func BuildDockerfile(ctx context.Context, d dockercli.Docker, tag, dockerfile, contextDir string) error {
	if !ValidTag(tag) {
		return fmt.Errorf("invalid image tag %q: must be cagent/<service>:<ref>", tag)
	}
	out, err := d.Combined(ctx, "build", "-t", tag, "--label", "cagent.managed=true", "-f", dockerfile, contextDir)
	if err != nil {
		return &BuildError{Err: err, Log: tail(out, 30)}
	}
	return nil
}

func tail(out []byte, n int) []string {
```

In `runner/cmd/cagent-runner/main.go` (`runBuild`), replace the two flag descriptions:

```go
	dir := fs.String("workdir", "", "working directory holding .cagent/service.yaml")
```

with:

```go
	dir := fs.String("workdir", "", "project root holding .cagent/services/<service>.yaml or .cagent/service.yaml")
```

In `runner/cmd/cagent-runner/main.go`, replace:

```go
	stack := fs.String("stack", "", "builder stack (default: the descriptor's, else java-springboot)")
```

with:

```go
	stack := fs.String("stack", "", "builder stack; for -workdir it must match the project's descriptor (default java-springboot)")
```

and the start of the workdir branch:

```go
		desc, err := workdir.LoadDescriptor(abs, *service)
		if err != nil {
			return usageErr(err.Error())
		}
		res.Source = "workdir:" + abs
		if *stack == "" {
			*stack = desc.Stack
		}
		artifact, err := workdir.Build(ctx, abs, desc)
		if err != nil {
			return failed(err)
		}
```

with:

```go
		desc, err := workdir.LoadDescriptor(abs, *service)
		if err != nil {
			return usageErr(err.Error())
		}
		// The project decides how it is built; the platform cannot pick another stack.
		if *stack != "" && *stack != desc.Stack {
			return usageErr(fmt.Sprintf("stack %q differs from the project's descriptor (%q)", *stack, desc.Stack))
		}
		*stack = desc.Stack
		res.Source = "workdir:" + abs
		artifact, err := workdir.Build(ctx, abs, desc)
		if err != nil {
			return failed(err)
		}
		if desc.Stack == workdir.StackDockerfile {
			dockerfile, contextDir, err := workdir.DockerInputs(abs, desc)
			if err != nil {
				return failed(err)
			}
			if err := builder.BuildDockerfile(ctx, d.docker, *tag, dockerfile, contextDir); err != nil {
				return failed(err)
			}
			return writeOrFail(stdout, stderr, res)
		}
```

**Step 4: Run all Go tests**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 sh -c 'test -z "$(gofmt -l .)" && go vet ./... && go test ./...'
```

Expected: every package `ok`.

**Step 5: Commit**

```bash
git add runner/internal/builder runner/cmd/cagent-runner
git commit -m "feat(runner): build a workdir project from its own Dockerfile"
```

---

### Task 3: Redaction keeps references

`{{password}}` and `${DB_PASSWORD}` name a secret without holding it. Masking them breaks M7b twice: a saved descriptor's bind template reads back as `******`, and a draft cannot see which variable holds the password. A reference's default (`${VAR:value}`) is a value, so it stays masked.

**Files:**
- Modify (full file below): `runner/internal/redact/redact.go`
- Modify: `runner/internal/redact/redact_test.go`

**Step 1: Write the failing tests**

In `runner/internal/redact/redact_test.go`, add cases above the last one — replace:

```go
		{"plain log line untouched",
```

with:

```go
		// References name a secret without holding it: the platform needs them to draft and
		// read back descriptors. A reference's default is a value, so it is masked.
		{"descriptor bind template",
			`SPRING_DATASOURCE_PASSWORD: "{{password}}"`,
			`SPRING_DATASOURCE_PASSWORD: "{{password}}"`},
		{"spring placeholder",
			"spring.datasource.password=${DB_PASSWORD}",
			"spring.datasource.password=${DB_PASSWORD}"},
		{"spring placeholder default",
			"password: ${DB_PASSWORD:s3cret}",
			"password: ${DB_PASSWORD:******}"},
		{"url credentials reference",
			"jdbc:mysql://{{user}}:{{password}}@{{host}}/app",
			"jdbc:mysql://{{user}}:{{password}}@{{host}}/app"},
		{"placeholder-shaped secret is still masked",
			"password=${x}y",
			"password=******"},
		{"plain log line untouched",
```

and widen the idempotence input:

```go
	in := "password=s3cret token: abc"
```

with:

```go
	in := "password=s3cret token: abc secret: ${API_SECRET:x} url://u:p@h"
```

**Step 2: Run them to verify they fail**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 go test ./internal/redact/
```

Expected: FAIL in `descriptor_bind_template`, `spring_placeholder`, `spring_placeholder_default`, `url_credentials_reference`.

**Step 3: Implement — replace `redact.go`**

Write `runner/internal/redact/redact.go`:

```go
// Package redact masks secrets in text before it leaves the user's machine.
package redact

import "regexp"

// Mask replaces every secret value.
const Mask = "******"

var (
	// A reference names a secret without holding it: a {{template}} placeholder, or a
	// ${VAR} / ${VAR:default} placeholder. Its default is a value and is masked.
	template    = regexp.MustCompile(`^\{\{\w+\}\}$`)
	placeholder = regexp.MustCompile(`^\$\{([\w.-]+)(:.*)?\}$`)
)

// mask returns Mask for a secret value, and keeps the name of a reference.
func mask(value string) string {
	if template.MatchString(value) {
		return value
	}
	if m := placeholder.FindStringSubmatch(value); m != nil {
		if m[2] == "" {
			return value
		}
		return "${" + m[1] + ":" + Mask + "}"
	}
	return Mask
}

// rules run in order; earlier rules handle shapes later rules would mangle. A rule with
// value set masks only group 2, the value; groups 1 and 3 (if any) are kept.
var rules = []struct {
	re    *regexp.Regexp
	repl  string
	value bool
}{
	// PEM private key blocks.
	{re: regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`), repl: "-----PRIVATE KEY " + Mask + "-----"},
	// AWS access key IDs.
	{re: regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`), repl: Mask},
	// Credentials embedded in URLs: scheme://user:password@host.
	{re: regexp.MustCompile(`(://[^:/@\s]+:)([^@\s]+)(@)`), value: true},
	// Bearer tokens.
	{re: regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+`), repl: "${1}" + Mask},
	// key=value, key: value, "key": "value" where the key names a secret.
	{re: regexp.MustCompile(`(?i)("?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*"?\s*[:=]\s*"?)([^"&;,\s]+)`), value: true},
}

// String returns s with every recognised secret replaced by Mask.
func String(s string) string {
	for _, r := range rules {
		if !r.value {
			s = r.re.ReplaceAllString(s, r.repl)
			continue
		}
		s = r.re.ReplaceAllStringFunc(s, func(match string) string {
			m := r.re.FindStringSubmatch(match)
			if len(m) > 3 {
				return m[1] + mask(m[2]) + m[3]
			}
			return m[1] + mask(m[2])
		})
	}
	return s
}
```

**Step 4: Run all Go tests**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 sh -c 'test -z "$(gofmt -l .)" && go vet ./... && go test ./...'
```

Expected: every package `ok`.

**Step 5: Commit**

```bash
git add runner/internal/redact
git commit -m "fix(runner): keep {{template}} and ${VAR} references when redacting"
```

> **Checkpoint.** Tasks 1–3 done. Verify: compare files with the plan, rerun `make runner-test` in the container, and check `git status` is clean.

---

### Task 4: The read-only `inspect` package

It reads a fixed set of file names in the root and in the modules the project's own `pom.xml`/`settings.gradle(.kts)` list, confines every path (symlinks included), caps sizes, redacts contents, and reports init-resource files by path only.

**Files:**
- Create: `runner/internal/inspect/inspect.go`, `runner/internal/inspect/inspect_test.go`

**Step 1: Write the failing tests**

Write `runner/internal/inspect/inspect_test.go`:

```go
package inspect

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// project writes files (relative path -> content) into a fresh directory.
func project(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		path := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func inspect(t *testing.T, dir string) Result {
	t.Helper()
	res, err := Inspect(dir)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func keys(m map[string]string) []string {
	var ks []string
	for k := range m {
		ks = append(ks, k)
	}
	slices.Sort(ks)
	return ks
}

func pom(modules ...string) string {
	var b strings.Builder
	b.WriteString("<project><artifactId>x</artifactId><modules>")
	for _, m := range modules {
		fmt.Fprintf(&b, "<module>%s</module>", m)
	}
	b.WriteString("</modules></project>")
	return b.String()
}

func TestInspectReadsOnlyTheFixedFiles(t *testing.T) {
	dir := project(t, map[string]string{
		"pom.xml":                                pom(),
		"Dockerfile":                             "FROM busybox\n",
		"src/main/resources/application.yml":     "server:\n  port: 8080\n",
		"README.md":                              "hello",
		"src/main/java/App.java":                 "class App {}",
		"src/main/resources/application-dev.yml": "x: 1",
	})
	got := keys(inspect(t, dir).Files)
	want := []string{"Dockerfile", "pom.xml", "src/main/resources/application.yml"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("files %v, want %v", got, want)
	}
}

func TestInspectFollowsMavenModules(t *testing.T) {
	dir := project(t, map[string]string{
		"pom.xml":          pom("api", "lib", "../escape", "/etc", "missing"),
		"api/pom.xml":      pom("impl"),
		"api/Dockerfile":   "FROM busybox\n",
		"api/impl/pom.xml": pom(),
		"lib/pom.xml":      pom(),
	})
	res := inspect(t, dir)
	if want := []string{"api", "lib", "api/impl"}; !reflect.DeepEqual(res.Modules, want) {
		t.Errorf("modules %v, want %v", res.Modules, want)
	}
	for _, f := range []string{"api/pom.xml", "api/Dockerfile", "api/impl/pom.xml", "lib/pom.xml"} {
		if _, ok := res.Files[f]; !ok {
			t.Errorf("missing %s in %v", f, keys(res.Files))
		}
	}
}

func TestInspectFollowsGradleIncludes(t *testing.T) {
	dir := project(t, map[string]string{
		"settings.gradle.kts":   "rootProject.name = \"shop\"\ninclude(\"web\", \":lib:core\")\ninclude 'api'\n",
		"web/build.gradle.kts":  "plugins { id(\"org.springframework.boot\") }",
		"lib/core/build.gradle": "",
		"api/build.gradle":      "",
	})
	res := inspect(t, dir)
	if want := []string{"web", "lib/core", "api"}; !reflect.DeepEqual(res.Modules, want) {
		t.Errorf("modules %v, want %v", res.Modules, want)
	}
	if _, ok := res.Files["web/build.gradle.kts"]; !ok {
		t.Errorf("files %v", keys(res.Files))
	}
}

func TestInspectReadsExistingDescriptors(t *testing.T) {
	dir := project(t, map[string]string{
		".cagent/service.yaml":       "name: a\n",
		".cagent/services/api.yaml":  "name: api\n",
		".cagent/services/notes.txt": "x",
		"api/.cagent/service.yaml":   "name: nested\n",
	})
	got := keys(inspect(t, dir).Files)
	want := []string{".cagent/service.yaml", ".cagent/services/api.yaml"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("files %v, want %v", got, want)
	}
}

func TestInspectSkipsSymlinksLeavingTheProject(t *testing.T) {
	outside := project(t, map[string]string{"pom.xml": pom(), "secret/pom.xml": pom()})
	dir := project(t, map[string]string{"pom.xml": pom("evil")})
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(dir, "evil")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "pom.xml"), filepath.Join(dir, "Dockerfile")); err != nil {
		t.Fatal(err)
	}
	res := inspect(t, dir)
	if len(res.Modules) != 0 || !reflect.DeepEqual(keys(res.Files), []string{"pom.xml"}) {
		t.Errorf("modules %v, files %v", res.Modules, keys(res.Files))
	}
}

func TestInspectRedactsSecrets(t *testing.T) {
	dir := project(t, map[string]string{
		"src/main/resources/application.properties": "spring.datasource.password=hunter2\n" +
			"app.secret=${APP_SECRET}\nserver.port=8080\n",
	})
	got := inspect(t, dir).Files["src/main/resources/application.properties"]
	// Secrets are masked; references to them stay, so a draft knows which variable to set.
	if strings.Contains(got, "hunter2") || !strings.Contains(got, "app.secret=${APP_SECRET}") ||
		!strings.Contains(got, "server.port=8080") {
		t.Errorf("content %q", got)
	}
}

func TestInspectTruncatesLargeFiles(t *testing.T) {
	big := strings.Repeat("a=b\n", MaxFileBytes/2)
	dir := project(t, map[string]string{"src/main/resources/application.properties": big})
	res := inspect(t, dir)
	if n := len(res.Files["src/main/resources/application.properties"]); n != MaxFileBytes {
		t.Errorf("kept %d bytes, want %d", n, MaxFileBytes)
	}
	if !reflect.DeepEqual(res.Truncated, []string{"src/main/resources/application.properties"}) {
		t.Errorf("truncated %v", res.Truncated)
	}
}

func TestInspectStopsAtTheTotalLimit(t *testing.T) {
	files := map[string]string{}
	var modules []string
	for i := range 40 {
		m := fmt.Sprintf("m%02d", i)
		modules = append(modules, m)
		files[m+"/pom.xml"] = pom() + strings.Repeat(" ", MaxFileBytes)
	}
	files["pom.xml"] = pom(modules...)
	res := inspect(t, project(t, files))
	total := 0
	for _, c := range res.Files {
		total += len(c)
	}
	if total > MaxTotalBytes || len(res.Truncated) == 0 {
		t.Errorf("total %d, truncated %d", total, len(res.Truncated))
	}
}

func TestInspectCapsModules(t *testing.T) {
	files := map[string]string{}
	var modules []string
	for i := range MaxModules + 5 {
		m := fmt.Sprintf("m%02d", i)
		modules = append(modules, m)
		files[m+"/pom.xml"] = pom()
	}
	files["pom.xml"] = pom(modules...)
	res := inspect(t, project(t, files))
	if len(res.Modules) != MaxModules || !reflect.DeepEqual(res.Truncated, []string{"modules"}) {
		t.Errorf("modules %d, truncated %v", len(res.Modules), res.Truncated)
	}
}

func TestInspectListsInitResourcesByPath(t *testing.T) {
	dir := project(t, map[string]string{
		"api/src/main/resources/db/migration/V1__init.sql": "create table t (id int);",
		"api/src/main/resources/db/changelog/master.xml":   "<databaseChangeLog/>",
		"cassandra/keyspace.cql":                           "CREATE KEYSPACE k;",
		"sql/schema-mysql.sql":                             "create table s (id int);",
		"docker/db/init/01-users.sql":                      "insert into u values (1);",
		"node_modules/pkg/schema.sql":                      "ignored",
		"api/target/classes/db/migration/V1__init.sql":     "ignored",
		"docs/notes.sql":                                   "not an init resource",
	})
	got := inspect(t, dir).Resources
	want := []string{
		"api/src/main/resources/db/changelog/master.xml",
		"api/src/main/resources/db/migration/V1__init.sql",
		"cassandra/keyspace.cql",
		"docker/db/init/01-users.sql",
		"sql/schema-mysql.sql",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("resources %v, want %v", got, want)
	}
	for _, f := range inspect(t, dir).Files {
		if strings.Contains(f, "create table") {
			t.Error("a resource file's content was read")
		}
	}
}

func TestInspectNeedsADirectory(t *testing.T) {
	if _, err := Inspect(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Error("missing directory accepted")
	}
}
```

**Step 2: Run them to verify they fail**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 go vet ./internal/inspect/
```

Expected: FAIL — `undefined: Result`.

**Step 3: Implement**

Write `runner/internal/inspect/inspect.go`:

```go
// Package inspect reads what the platform needs to draft a descriptor for a project that has
// none: build files, Dockerfiles, Spring configuration, and existing descriptors. It reads a
// fixed set of file names in the project root and in the modules the project's own build
// files list; the platform names only the project directory, never a file. Init resources
// (SQL, CQL, migrations) are reported by path only.
package inspect

import (
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/redact"
)

// Limits on what one inspection reads and reports.
const (
	MaxModules    = 50
	MaxFileBytes  = 64 << 10
	MaxTotalBytes = 2 << 20
	MaxResources  = 200
	maxWalk       = 50_000 // directory entries visited while looking for resources
)

// ModuleFiles are read in the root and in every module, relative to that directory.
var ModuleFiles = []string{
	"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
	"package.json", "go.mod", "Dockerfile",
	"src/main/resources/application.yml", "src/main/resources/application.yaml",
	"src/main/resources/application.properties",
}

// Descriptor files are read in the root only.
const (
	descriptorFile = ".cagent/service.yaml"
	descriptorDir  = ".cagent/services"
)

// skipDirs are never searched for resources: dependencies, build output, tool state.
var skipDirs = map[string]bool{
	".git": true, ".gradle": true, ".idea": true, ".cagent": true,
	"node_modules": true, "target": true, "build": true, "dist": true, "vendor": true,
}

// Result is what inspect reports. Paths are relative to the project root, with slashes.
type Result struct {
	Modules   []string          `json:"modules"`   // module directories, in discovery order
	Files     map[string]string `json:"files"`     // path -> redacted content
	Resources []string          `json:"resources"` // init-resource files, sorted
	Truncated []string          `json:"truncated"` // cut files, files over the total, "modules", "resources"
}

type reader struct {
	root  string
	total int
	res   Result
}

// Inspect reads dir. dir must be a directory; the caller checks it is one the user allowed.
func Inspect(dir string) (Result, error) {
	root, err := guard.Resolve(dir)
	if err != nil {
		return Result{}, err
	}
	if fi, err := os.Stat(root); err != nil || !fi.IsDir() {
		return Result{}, fmt.Errorf("%s is not a directory", dir)
	}
	r := &reader{root: root, res: Result{
		Modules: []string{}, Files: map[string]string{}, Resources: []string{}, Truncated: []string{},
	}}
	r.readDescriptors()
	seen := map[string]bool{".": true}
	queue := []string{"."}
	for len(queue) > 0 {
		mod := queue[0]
		queue = queue[1:]
		for _, child := range r.readModule(mod) {
			if seen[child] {
				continue
			}
			seen[child] = true
			if len(r.res.Modules) == MaxModules {
				r.truncated("modules")
				continue
			}
			r.res.Modules = append(r.res.Modules, child)
			queue = append(queue, child)
		}
	}
	r.findResources()
	return r.res, nil
}

func (r *reader) truncated(what string) {
	if !slices.Contains(r.res.Truncated, what) {
		r.res.Truncated = append(r.res.Truncated, what)
	}
}

// inside resolves rel under the root and reports whether it stays there.
func (r *reader) inside(rel string) (string, bool) {
	p, err := guard.Resolve(filepath.Join(r.root, filepath.FromSlash(rel)))
	if err != nil || !guard.Within(r.root, p) {
		return "", false
	}
	return p, true
}

// read reads one file into the result and returns its raw (unredacted) content. Missing
// files, non-regular files, and symlinks leaving the root are skipped.
func (r *reader) read(rel string) (string, bool) {
	p, ok := r.inside(rel)
	if !ok {
		return "", false
	}
	fi, err := os.Stat(p)
	if err != nil || !fi.Mode().IsRegular() {
		return "", false
	}
	budget := min(MaxFileBytes, MaxTotalBytes-r.total)
	if budget <= 0 {
		r.truncated(rel)
		return "", false
	}
	f, err := os.Open(p)
	if err != nil {
		return "", false
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, int64(budget)+1))
	if err != nil {
		return "", false
	}
	if len(data) > budget {
		data = data[:budget]
		r.truncated(rel)
	}
	r.total += len(data)
	raw := string(data)
	r.res.Files[rel] = redact.String(raw)
	return raw, true
}

func (r *reader) readDescriptors() {
	r.read(descriptorFile)
	dir, ok := r.inside(descriptorDir)
	if !ok {
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".yaml") {
			r.read(descriptorDir + "/" + e.Name())
		}
	}
}

// readModule reads a module's files and returns the child modules its build files list.
func (r *reader) readModule(mod string) []string {
	var children []string
	add := func(rel string) {
		child := path.Join(mod, rel)
		if !relative(rel) || child == "." {
			return
		}
		if p, ok := r.inside(child); ok {
			if fi, err := os.Stat(p); err == nil && fi.IsDir() {
				children = append(children, child)
			}
		}
	}
	for _, name := range ModuleFiles {
		raw, ok := r.read(path.Join(mod, name))
		if !ok {
			continue
		}
		switch name {
		case "pom.xml":
			for _, m := range pomModules(raw) {
				add(m)
			}
		case "settings.gradle", "settings.gradle.kts":
			for _, m := range gradleIncludes(raw) {
				add(m)
			}
		}
	}
	return children
}

func relative(p string) bool {
	if p == "" || path.IsAbs(p) || filepath.IsAbs(p) {
		return false
	}
	c := path.Clean(p)
	return c != ".." && !strings.HasPrefix(c, "../")
}

func pomModules(raw string) []string {
	var p struct {
		Modules []string `xml:"modules>module"`
	}
	if err := xml.Unmarshal([]byte(raw), &p); err != nil {
		return nil
	}
	var out []string
	for _, m := range p.Modules {
		out = append(out, strings.TrimSpace(m))
	}
	return out
}

var (
	gradleInclude = regexp.MustCompile(`(?m)^\s*include\b(.*)$`)
	quoted        = regexp.MustCompile(`["']([^"']+)["']`)
)

// gradleIncludes maps `include("web", ":lib:core")` to web and lib/core.
func gradleIncludes(raw string) []string {
	var out []string
	for _, line := range gradleInclude.FindAllStringSubmatch(raw, -1) {
		for _, q := range quoted.FindAllStringSubmatch(line[1], -1) {
			out = append(out, strings.ReplaceAll(strings.TrimPrefix(q[1], ":"), ":", "/"))
		}
	}
	return out
}

// isResource reports whether a file is an init resource: CQL, schema SQL, SQL under db/init,
// or a Flyway or Liquibase file.
func isResource(rel string) bool {
	under := func(dir string) bool {
		return strings.HasPrefix(rel, dir+"/") || strings.Contains(rel, "/"+dir+"/")
	}
	base := path.Base(rel)
	schema, _ := path.Match("schema*.sql", base)
	return strings.HasSuffix(base, ".cql") || schema ||
		(under("db/init") && strings.HasSuffix(base, ".sql")) ||
		under("db/migration") || under("db/changelog")
}

var errStop = errors.New("stop")

func (r *reader) findResources() {
	visited := 0
	err := filepath.WalkDir(r.root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil // unreadable directory: skip it
		}
		if visited++; visited > maxWalk {
			r.truncated("resources")
			return errStop
		}
		if d.IsDir() {
			if p != r.root && skipDirs[d.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(r.root, p)
		if err != nil || !d.Type().IsRegular() || !isResource(filepath.ToSlash(rel)) {
			return nil
		}
		if len(r.res.Resources) == MaxResources {
			r.truncated("resources")
			return errStop
		}
		r.res.Resources = append(r.res.Resources, filepath.ToSlash(rel))
		return nil
	})
	_ = err // errStop or nil: what was found so far is the answer
	slices.Sort(r.res.Resources)
}
```

**Step 4: Run the tests**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 sh -c 'test -z "$(gofmt -l .)" && go vet ./internal/inspect/ && go test ./internal/inspect/'
```

Expected: `ok`.

**Step 5: Commit**

```bash
git add runner/internal/inspect
git commit -m "feat(runner): inspect a project's build files read-only"
```

---

### Task 5: `inspect` subcommand and RPC tool

Over RPC the platform may name only a directory under a `-workdir-root` the user gave the Runner; `build` and `inspect` share that check.

**Files:**
- Create: `runner/cmd/cagent-runner/inspect.go`, `runner/cmd/cagent-runner/inspect_test.go`
- Modify: `runner/cmd/cagent-runner/main.go`, `runner/cmd/cagent-runner/serve.go`

**Step 1: Write the failing tests**

Write `runner/cmd/cagent-runner/inspect_test.go`:

```go
package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/inspect"
)

func TestInspectPrintsWhatItRead(t *testing.T) {
	proj := project(t, map[string]string{"pom.xml": "<project/>", "README.md": "x"})
	var out, errOut bytes.Buffer
	if code := run([]string{"inspect", "-workdir", proj}, &out, &errOut, testDeps(t)); code != exitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var res inspect.Result
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Files) != 1 || res.Files["pom.xml"] != "<project/>" {
		t.Errorf("files %v", res.Files)
	}
}

func TestInspectArgumentErrors(t *testing.T) {
	for _, args := range [][]string{{"inspect"}, {"inspect", "-workdir", "/does/not/exist"}} {
		var out, errOut bytes.Buffer
		if code := run(args, &out, &errOut, testDeps(t)); code != exitError {
			t.Errorf("%v: exit %d, want %d", args, code, exitError)
		}
	}
}

// Over RPC the platform may inspect only directories under a -workdir-root the user gave.
func TestServeInspectStaysInsideWorkdirRoots(t *testing.T) {
	proj := project(t, map[string]string{"pom.xml": "<project/>"})
	c, _ := startServe(t, testDeps(t), func(cfg *serveConfig) { cfg.workdirRoots = []string{proj} })

	resp := call(t, c, 1, "inspect", map[string]any{"workdir": proj})
	if resp.Error != nil || resp.Result == nil || resp.Result.ExitCode != exitOK {
		t.Fatalf("inspect: %+v", resp)
	}
	if !strings.Contains(string(resp.Result.Output), `"pom.xml"`) {
		t.Errorf("output %s", resp.Result.Output)
	}
	for i, params := range []map[string]any{
		{"workdir": "/etc"},
		{"workdir": proj + "/.."},
		{"workdir": proj, "file": "/etc/passwd"},
		{},
	} {
		resp := call(t, c, i+2, "inspect", params)
		if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
			t.Errorf("%v: got %+v, want invalid params", params, resp)
		}
	}
}
```

**Step 2: Run them to verify they fail**

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 go test ./cmd/...
```

Expected: FAIL — `inspect` is an unknown subcommand and an unknown RPC method.

**Step 3: Implement**

Write `runner/cmd/cagent-runner/inspect.go`:

```go
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"path/filepath"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/inspect"
)

// runInspect prints what the platform may read of a project to draft its descriptor.
func runInspect(args []string, stdout, stderr io.Writer, _ deps) int {
	fs := flag.NewFlagSet("inspect", flag.ContinueOnError)
	fs.SetOutput(stderr)
	dir := fs.String("workdir", "", "project root to inspect")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if *dir == "" {
		fmt.Fprintln(stderr, "inspect needs -workdir")
		return exitError
	}
	res, err := inspect.Inspect(*dir)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return writeOrFail(stdout, stderr, res)
}

func inspectTool(cfg serveConfig, _ deps, params json.RawMessage) ([]string, error) {
	var p struct {
		Workdir string `json:"workdir"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if p.Workdir == "" {
		return nil, errors.New("workdir is required")
	}
	dir, err := allowedWorkdir(cfg, p.Workdir)
	if err != nil {
		return nil, err
	}
	return []string{"inspect", "-workdir", dir}, nil
}

// allowedWorkdir resolves a working directory the platform named and checks it lies under
// a -workdir-root the user gave this Runner.
func allowedWorkdir(cfg serveConfig, dir string) (string, error) {
	resolved, err := guard.Resolve(dir)
	if err != nil {
		return "", err
	}
	if !withinRoots(resolved, cfg.workdirRoots) {
		return "", fmt.Errorf("workdir %s is outside every -workdir-root the user allowed", dir)
	}
	return filepath.Clean(resolved), nil
}
```

In `runner/cmd/cagent-runner/main.go`, extend the usage:

```go
  cagent-runner build    -name N -service S -tag cagent/S:REF (-maven G:A:V[:EXT] [-settings F] | -workdir DIR) [-stack S] [-jdk V]`
```

with:

```go
  cagent-runner build    -name N -service S -tag cagent/S:REF (-maven G:A:V[:EXT] [-settings F] | -workdir DIR) [-stack S] [-jdk V]
  cagent-runner inspect  -workdir DIR`
```

and register the subcommand:

```go
		"build": runBuild, "serve": runServe,
```

with:

```go
		"build": runBuild, "serve": runServe, "inspect": runInspect,
```

In `runner/cmd/cagent-runner/serve.go`, register the tool:

```go
	"build":    buildTool,
}
```

with:

```go
	"build":    buildTool,
	"inspect":  inspectTool,
}
```

let `buildTool` share the root check with `inspectTool`:

```go
		dir, err := guard.Resolve(p.Workdir)
		if err != nil {
			return nil, err
		}
		if !withinRoots(dir, cfg.workdirRoots) {
			return nil, fmt.Errorf("workdir %s is outside every -workdir-root the user allowed", p.Workdir)
		}
		argv = append(argv, "-workdir", filepath.Clean(dir))
```

with:

```go
		dir, err := allowedWorkdir(cfg, p.Workdir)
		if err != nil {
			return nil, err
		}
		argv = append(argv, "-workdir", dir)
```

and drop the now unused import:

```go
	"os/signal"
	"path/filepath"
```

with:

```go
	"os/signal"
```

**Step 4: Run all Go tests**

```bash
docker run --rm -v "$PWD":/repo -w /repo -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 make runner-test
```

Expected: every package `ok` (fourteen with `inspect`).

**Step 5: Commit**

```bash
git add runner/cmd/cagent-runner
git commit -m "feat(runner): serve inspect under the user's workdir roots"
```

---

### Task 6: Real Docker: a two-module project builds from its Dockerfile

**Files:**
- Create: `runner/test/integration/project_test.go`

**Step 1: Write the test**

Write `runner/test/integration/project_test.go`:

```go
//go:build integration

package integration

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/builder"
	"github.com/yutaofr/container-agent/runner/internal/inspect"
	"github.com/yutaofr/container-agent/runner/internal/workdir"
)

// A two-module project whose deployable module has its own Dockerfile. Its build command
// stands in for the developer's toolchain; no JDK or Node is needed.
var twoModules = map[string]string{
	"pom.xml":        "<project><modules><module>api</module><module>lib</module></modules></project>",
	"api/pom.xml":    "<project><artifactId>api</artifactId></project>",
	"lib/pom.xml":    "<project><artifactId>lib</artifactId></project>",
	"api/Dockerfile": "FROM busybox:1.37\nCOPY out/msg /msg\nCMD [\"cat\", \"/msg\"]\n",
	".cagent/services/api.yaml": "name: api\nkind: app\nstack: dockerfile\nsources:\n  workdir:\n" +
		"    build: 'mkdir -p api/out && echo built-by-the-project > api/out/msg'\n" +
		"    dockerfile: api/Dockerfile\n    context: api\nruntime: {}\n",
}

func TestMultiModuleProjectIsInspectedAndBuiltFromItsDockerfile(t *testing.T) {
	ctx := context.Background()
	proj := t.TempDir()
	for rel, body := range twoModules {
		path := filepath.Join(proj, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	res, err := inspect.Inspect(proj)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(res.Modules, []string{"api", "lib"}) {
		t.Errorf("modules %v", res.Modules)
	}
	if _, ok := res.Files[".cagent/services/api.yaml"]; !ok {
		t.Errorf("descriptor not read back: %v", res.Files)
	}

	desc, err := workdir.LoadDescriptor(proj, "api")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := workdir.Build(ctx, proj, desc); err != nil {
		t.Fatal(err)
	}
	dockerfile, contextDir, err := workdir.DockerInputs(proj, desc)
	if err != nil {
		t.Fatal(err)
	}
	tag := uniqueTag(t, "workdir-dockerfile")
	if err := builder.BuildDockerfile(ctx, d, tag, dockerfile, contextDir); err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command("docker", "run", "--rm", tag).CombinedOutput()
	if err != nil || strings.TrimSpace(string(out)) != "built-by-the-project" {
		t.Fatalf("run: %v: %s", err, out)
	}
}
```

**Step 2: Build the integration tests for the host and run the workdir ones**

Go is not on the host, so cross-compile the test binary for macOS and run it there.

```bash
docker run --rm -v "$PWD":/repo -w /repo/runner -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build -e GOOS=darwin -e GOARCH=arm64 golang:1.24 go test -c -tags integration -o /repo/bin/it.test ./test/integration/ && (cd runner/test/integration && ../../../bin/it.test -test.run 'TestMultiModule|TestWorkdirBuilds' -test.v)
```

Expected: `--- PASS` for both tests (Docker must be running).

**Step 3: Run the whole integration suite, then clean up**

```bash
(cd runner/test/integration && ../../../bin/it.test) && rm -rf bin && test -z "$(docker ps -aq --filter label=cagent.managed)"
```

Expected: `PASS`, and no `cagent.managed` containers left.

**Step 4: Commit**

```bash
git add runner/test/integration/project_test.go
git commit -m "test(runner): inspect and build a multi-module project on real Docker"
```

> **Checkpoint.** Tasks 4–6 done: the Runner side of M7b is complete. Verify the files, `make runner-test`, the integration binary run, and that no containers or `cagent/it-*` images are left.

---

### Task 7: Descriptor schema: workdir rules shared with the Runner

`stack` becomes `java-springboot | dockerfile`, and `sources.workdir` gains `dockerfile` and `context`, with the same rules as the Runner's `workdir` package (Task 1), checked against the same fixtures.

**Files:**
- Modify (full files below): `platform/src/cagent/catalog.py`, `platform/tests/test_catalog.py`

**Step 1: Write the failing tests**

Replace `platform/tests/test_catalog.py`:

```python
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cagent.catalog import CatalogError, ServiceDescriptor, load_catalog

from .conftest import FIXTURE_CATALOG, REPO_CATALOG

MYSQL = (REPO_CATALOG / "middleware" / "mysql.yaml").read_text()


def write_template(root: Path, name: str, text: str) -> None:
    (root / "middleware").mkdir(parents=True, exist_ok=True)
    (root / "middleware" / f"{name}.yaml").write_text(text)


def test_shipped_catalog_loads(catalog):
    assert set(catalog.middleware) == {"mysql", "redis", "kafka"}
    assert set(catalog.services) == {"order-service"}
    assert catalog.middleware["kafka"].provides.capability == "kafka"
    assert catalog.services["order-service"].requires[0].version == ">=8.0"


def test_unknown_key_is_rejected(tmp_path):
    write_template(tmp_path, "mysql", MYSQL.replace("image:", "imgae:"))
    with pytest.raises(CatalogError, match="imgae"):
        load_catalog(tmp_path)


def test_duplicate_name_across_roots_is_rejected(tmp_path):
    write_template(tmp_path, "my-mysql", MYSQL)
    with pytest.raises(CatalogError, match="duplicate name 'mysql'"):
        load_catalog(REPO_CATALOG, tmp_path)


def test_template_placeholders_must_be_known(tmp_path):
    write_template(tmp_path, "mysql", MYSQL.replace('"{{db}}"', '"{{database}}"'))
    with pytest.raises(CatalogError, match="unknown placeholders: database"):
        load_catalog(tmp_path)


def test_healthcheck_needs_exactly_one_kind(tmp_path):
    bad = MYSQL.replace('  cmd: ["CMD-SHELL"', '  http: /health\n  cmd: ["CMD-SHELL"')
    write_template(tmp_path, "mysql", bad)
    with pytest.raises(CatalogError, match="exactly one of http or cmd"):
        load_catalog(tmp_path)


def test_invalid_version_specifier_is_rejected(tmp_path):
    text = (FIXTURE_CATALOG / "services" / "order-service.yaml").read_text()
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "order-service.yaml").write_text(text.replace(">=8.0", "at least 8"))
    with pytest.raises(CatalogError, match="invalid version specifier"):
        load_catalog(tmp_path)


def test_missing_root_is_rejected(tmp_path):
    with pytest.raises(CatalogError, match="not a directory"):
        load_catalog(tmp_path / "nope")


DESCRIPTORS = Path(__file__).parent / "fixtures" / "descriptors"


@pytest.mark.parametrize("path", sorted(DESCRIPTORS.glob("valid/*.yaml")), ids=lambda p: p.name)
def test_shared_valid_descriptors(path):
    ServiceDescriptor.model_validate(yaml.safe_load(path.read_text()))


# The Runner's workdir tests read the same files, so both sides agree on workdir sources.
@pytest.mark.parametrize("path", sorted(DESCRIPTORS.glob("invalid/*.yaml")), ids=lambda p: p.name)
def test_shared_invalid_descriptors(path):
    with pytest.raises(ValidationError):
        ServiceDescriptor.model_validate(yaml.safe_load(path.read_text()))


def test_dockerfile_stack_builds_only_from_a_workdir():
    doc = yaml.safe_load((DESCRIPTORS / "valid" / "dockerfile.yaml").read_text())
    doc["sources"]["release"] = {"nexus": "com.acme:demo:{version}:jar"}
    with pytest.raises(ValidationError, match="stack dockerfile builds only from a workdir"):
        ServiceDescriptor.model_validate(doc)
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_catalog.py
```

Expected: `4 failed` — the two dockerfile fixtures, `unknown-stack`, and the release-plus-dockerfile check.

**Step 3: Implement**

Replace `platform/src/cagent/catalog.py`:

```python
"""Service descriptors and middleware templates, and loading them from catalog directories."""

import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cagent.render import placeholders

# Names we accept for compose projects, services, and catalog entries.
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
Name = Annotated[str, Field(pattern=NAME_PATTERN)]
PortNumber = Annotated[int, Field(gt=0, lt=65536)]


class Strict(BaseModel):
    """Base model that rejects unknown keys, so a typo in a catalog file fails loudly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Port(Strict):
    name: str
    container: PortNumber


class Healthcheck(Strict):
    """Either http (a path checked with curl on a named port) or cmd (a compose test)."""

    http: str | None = None
    port: str | None = None  # port name for http; defaults to the first port
    expect: int = 200
    cmd: list[str] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "Healthcheck":
        if (self.http is None) == (self.cmd is None):
            raise ValueError("healthcheck needs exactly one of http or cmd")
        return self


class Env(Strict):
    required: list[str] = []
    defaults: dict[str, str] = {}


class Runtime(Strict):
    ports: list[Port] = []
    env: Env = Env()
    healthcheck: Healthcheck | None = None

    @model_validator(mode="after")
    def _http_port_exists(self) -> "Runtime":
        hc = self.healthcheck
        if hc is not None and hc.http is not None:
            names = [p.name for p in self.ports]
            if not names:
                raise ValueError("http healthcheck needs at least one port")
            if hc.port is not None and hc.port not in names:
                raise ValueError(f"healthcheck port {hc.port!r} is not one of {names}")
        return self


class NexusSource(Strict):
    nexus: str


class CiSource(Strict):
    jenkins: str
    artifact: str


def _inside(path: str) -> bool:
    """A relative path that stays inside its base lexically (symlinks are the Runner's job)."""
    if not path or posixpath.isabs(path):
        return False
    norm = posixpath.normpath(path)
    return norm != ".." and not norm.startswith("../")


class WorkdirSource(Strict):
    build: str | None = None  # the developer's command, run in the project root
    output: str | None = None  # java-springboot: glob matching exactly one jar
    dockerfile: str | None = None  # dockerfile: path to the project's Dockerfile
    context: str | None = None  # dockerfile: build context, default "."

    @model_validator(mode="after")
    def _paths_inside(self) -> "WorkdirSource":
        for path in (self.dockerfile, self.context):
            if path is not None and not _inside(path):
                raise ValueError(f"{path!r} must be a relative path inside the project")
        return self


class Sources(Strict):
    release: NexusSource | None = None
    snapshot: NexusSource | None = None
    ci: CiSource | None = None
    workdir: WorkdirSource | None = None


class Requirement(Strict):
    capability: str
    version: str | None = None  # PEP 440 specifier, e.g. ">=8.0"
    bind: dict[str, str] = {}  # env var -> template over {{host}}, {{port}}, provider connection

    @field_validator("version")
    @classmethod
    def _valid_specifier(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                SpecifierSet(v)
            except InvalidSpecifier as e:
                raise ValueError(f"invalid version specifier {v!r}") from e
        return v


class HttpProbe(Strict):
    path: str
    expect: int = 200
    port: str | None = None


class Probe(Strict):
    http: HttpProbe


class ServiceDescriptor(Strict):
    """An application's `.cagent/service.yaml` or `.cagent/services/<name>.yaml`.

    stack java-springboot images a jar with the Runner's template; dockerfile builds the
    project's own Dockerfile. The Runner's workdir package applies the same workdir rules.
    """

    name: Name
    kind: Literal["app"]
    stack: Literal["java-springboot", "dockerfile"]
    sources: Sources
    runtime: Runtime
    requires: list[Requirement] = []
    probes: list[Probe] = []

    @model_validator(mode="after")
    def _workdir_fits_stack(self) -> "ServiceDescriptor":
        w = self.sources.workdir
        if self.stack == "dockerfile":
            if self.sources.release or self.sources.snapshot or self.sources.ci:
                raise ValueError("stack dockerfile builds only from a workdir source")
            if w is None or w.dockerfile is None:
                raise ValueError("stack dockerfile needs sources.workdir.dockerfile")
        elif w is not None:
            if w.build is None or w.output is None:
                raise ValueError("sources.workdir needs build and output")
            if w.dockerfile is not None or w.context is not None:
                raise ValueError("sources.workdir: dockerfile and context need stack dockerfile")
        return self


class Provides(Strict):
    capability: str


class Preset(Strict):
    mem_limit: str
    cpus: float


class MiddlewareTemplate(Strict):
    """A platform-maintained middleware definition."""

    name: Name
    kind: Literal["middleware"]
    provides: Provides
    image: str  # "{version}" is replaced by the chosen version
    command: list[str] | None = None  # overrides the image's default command
    versions: list[str]
    default_version: str
    port: PortNumber
    connection: dict[str, str] = {}  # values bind templates may use besides host and port
    env: dict[str, str] = {}
    healthcheck: Healthcheck
    presets: dict[str, Preset]
    default_preset: str = "tiny"

    @model_validator(mode="after")
    def _consistent(self) -> "MiddlewareTemplate":
        if "{version}" not in self.image:
            raise ValueError("image must contain {version}")
        if self.default_version not in self.versions:
            raise ValueError(f"default_version {self.default_version!r} not in versions")
        if self.default_preset not in self.presets:
            raise ValueError(f"default_preset {self.default_preset!r} not in presets")
        known = {"host", "port", *self.connection}
        used = set().union(*(placeholders(v) for v in self.env.values()))
        used |= set().union(*(placeholders(c) for c in self.healthcheck.cmd or []))
        if unknown := used - known:
            raise ValueError(f"unknown placeholders: {', '.join(sorted(unknown))}")
        return self


class CatalogError(Exception):
    """A catalog file is missing, malformed, or duplicates another entry."""


@dataclass(frozen=True)
class Catalog:
    services: dict[str, ServiceDescriptor]
    middleware: dict[str, MiddlewareTemplate]


def load_catalog(*roots: Path) -> Catalog:
    """Load services/*.yaml and middleware/*.yaml from each root. Names must be unique."""
    services: dict[str, ServiceDescriptor] = {}
    middleware: dict[str, MiddlewareTemplate] = {}
    for root in roots:
        if not root.is_dir():
            raise CatalogError(f"{root}: not a directory")
        _load_dir(root / "services", ServiceDescriptor, services)
        _load_dir(root / "middleware", MiddlewareTemplate, middleware)
    return Catalog(services, middleware)


def _load_dir[M: Strict](directory: Path, model: type[M], into: dict[str, M]) -> None:
    for path in sorted(directory.glob("*.yaml")):
        try:
            item = model.model_validate(yaml.safe_load(path.read_text()))
        except (yaml.YAMLError, ValidationError) as e:
            raise CatalogError(f"{path}: {e}") from e
        if item.name in into:
            raise CatalogError(f"{path}: duplicate name {item.name!r}")
        into[item.name] = item
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `198 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/catalog.py platform/tests/test_catalog.py
git commit -m "feat(platform): workdir sources for the dockerfile stack"
```

---

### Task 8: Structured gaps from the generator

Each problem becomes a `Gap` carrying the answers that would close it. `str(gap)` is the old message and `GenerationError.problems` still lists strings, so the CLI and escalations read as before. Missing env keys and unknown publish ports are now one gap each.

**Files:**
- Create: `platform/src/cagent/gaps.py`
- Modify (full files below): `platform/src/cagent/generate.py`, `platform/src/cagent/matching.py`, `platform/tests/test_generate.py`, `platform/tests/test_matching.py`

**Step 1: Write the failing tests**

Replace `platform/tests/test_generate.py`:

```python
import os

import pytest

from cagent.catalog import load_catalog
from cagent.generate import GenerationError, generate, image_tag, to_yaml
from cagent.spec import Source

from .conftest import FIXTURES, REPO_CATALOG, spec_with

GOLDEN = FIXTURES / "orders-poc.compose.yaml"


def test_orders_poc_matches_golden(catalog, orders_spec):
    """Regenerate with UPDATE_GOLDEN=1 after an intended change, then review the diff."""
    text = to_yaml(generate(orders_spec, catalog))
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text()


def test_app_service_is_wired_to_its_providers(catalog, orders_spec):
    app = generate(orders_spec, catalog)["services"]["order-service"]
    assert app["environment"]["SPRING_DATASOURCE_URL"] == "jdbc:mysql://mysql:3306/app"
    assert app["environment"]["SPRING_KAFKA_BOOTSTRAP_SERVERS"] == "kafka:9092"
    assert app["environment"]["SPRING_PROFILES_ACTIVE"] == "dev"
    assert app["depends_on"] == {
        "kafka": {"condition": "service_healthy"},
        "mysql": {"condition": "service_healthy"},
    }
    assert app["ports"] == ["18080:8080"]


def test_http_healthcheck_escapes_dollar(catalog, orders_spec):
    app = generate(orders_spec, catalog)["services"]["order-service"]
    assert "$$(curl" in app["healthcheck"]["test"][1]


def test_every_problem_is_reported_at_once(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "release", "ref": "1.4.2"},
                "publish": {"grpc": 9090},
            },
            {"name": "billing", "source": {"kind": "release", "ref": "2.0"}},
        ],
        middleware=[{"template": "mysql", "version": "5.7"}, {"template": "kafka"}],
    )
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    assert err.value.problems == [
        "mysql: mysql 5.7 unsupported; use 8.0, 8.4",
        "order-service/mysql: needs version >=8.0; mysql is 5.7",
        "order-service: missing required env SPRING_PROFILES_ACTIVE",
        "order-service: cannot publish unknown port grpc; ports are http",
        "billing: not registered in the catalog",
    ]


@pytest.mark.parametrize(
    "kind, ref, want",
    [
        ("release", "1.4.2", "cagent/svc:release-1.4.2"),
        ("snapshot", "2.0-SNAPSHOT", "cagent/svc:snapshot-2.0-SNAPSHOT"),
        ("ci", "order-service/main#123", "cagent/svc:ci-order-service-main-123"),
        ("workdir", "/Users/dev/order-service", "cagent/svc:workdir-0b8f2b1c3a0d"),
    ],
)
def test_image_tag(kind, ref, want):
    got = image_tag("svc", Source(kind=kind, ref=ref))
    if kind == "workdir":
        assert got.startswith("cagent/svc:workdir-") and len(got) == len(want)
    else:
        assert got == want


def test_template_command_is_emitted():
    catalog = load_catalog(REPO_CATALOG, FIXTURES / "e2e-catalog")
    spec = spec_with(services=[], middleware=[{"template": "memhog"}])
    memhog = generate(spec, catalog)["services"]["memhog"]
    assert memhog["command"][:2] == ["sh", "-c"] and "httpd" in memhog["command"][2]
    assert (
        "command"
        not in generate(spec_with(services=[], middleware=[{"template": "redis"}]), catalog)[
            "services"
        ]["redis"]
    )


def test_gaps_carry_the_answers_that_would_close_them(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "ci", "ref": "main#1"},
                "publish": {"grpc": 9090},
            },
            {"name": "billing", "source": {"kind": "workdir", "ref": "/src/billing"}},
        ],
        middleware=[
            {"template": "mysq"},
            {"template": "kafka", "version": "2.0", "preset": "huge"},
            {"template": "redis", "preset": "huge"},
        ],
    )
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    gaps = {(g.kind, g.target, g.key): g for g in err.value.gaps}
    assert gaps[("unknown_template", "mysq", None)].options == ("kafka", "mysql", "redis")
    assert gaps[("unknown_template", "mysq", None)].default == "mysql"
    assert gaps[("unsupported_version", "kafka", None)].options == ("3.9.0", "4.0.0")
    assert gaps[("unsupported_version", "kafka", None)].default == "3.9.0"
    assert gaps[("unknown_preset", "redis", None)].options == ("tiny", "standard")
    assert gaps[("no_provider", "order-service/mysql", None)].options == ("mysql",)
    assert gaps[("missing_env", "order-service", "SPRING_PROFILES_ACTIVE")].options == ()
    assert gaps[("unknown_port", "order-service", "grpc")].default == "http"
    assert ("unregistered", "billing", None) in gaps
    # ci is declared by order-service, so the ci source is fine; no no_source gap.
    assert not any(k == "no_source" for k, _, _ in gaps)
    assert err.value.problems == [str(g) for g in err.value.gaps]


def test_undeclared_source_kind_offers_the_declared_ones(catalog):
    desc = catalog.services["order-service"]
    sources = desc.sources.model_copy(update={"ci": None, "workdir": None})
    catalog.services["order-service"] = desc.model_copy(update={"sources": sources})
    spec = spec_with(services=[{"name": "order-service", "source": {"kind": "ci", "ref": "x"}}])
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    gap = next(g for g in err.value.gaps if g.kind == "no_source")
    assert (gap.target, gap.options, gap.default) == (
        "order-service",
        ("release", "snapshot"),
        "release",
    )
```

Replace `platform/tests/test_matching.py`:

```python
from cagent.matching import match

from .conftest import spec_with


def providers(bindings):
    return {(b.consumer, b.requirement.capability): b.provider for b in bindings}


def test_each_requirement_binds_to_its_provider(catalog, orders_spec):
    bindings, gaps = match(orders_spec, catalog)
    assert gaps == []
    assert providers(bindings) == {
        ("order-service", "mysql"): "mysql",
        ("order-service", "kafka"): "kafka",
    }


def test_version_mismatch_is_reported(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql", "version": "8.0"},
            {"template": "kafka"},
        ]
    )
    # Tighten the requirement so 8.0 no longer satisfies it.
    desc = catalog.services["order-service"]
    req = desc.requires[0].model_copy(update={"version": ">=8.4"})
    catalog.services["order-service"] = desc.model_copy(
        update={"requires": [req, desc.requires[1]]}
    )
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == ["order-service/mysql: needs version >=8.4; mysql is 8.0"]
    assert (gaps[0].kind, gaps[0].key, gaps[0].options, gaps[0].default) == (
        "version_mismatch",
        "mysql",
        ("8.4",),
        "8.4",
    )


def test_missing_provider_is_reported(catalog):
    _, gaps = match(spec_with(middleware=[{"template": "mysql"}]), catalog)
    assert [str(g) for g in gaps] == [
        "order-service/kafka: no middleware in the spec provides kafka"
    ]
    assert (gaps[0].kind, gaps[0].options, gaps[0].default) == ("no_provider", ("kafka",), "kafka")


def test_two_providers_are_ambiguous(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ]
    )
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == [
        "order-service/mysql: ambiguous, candidates mysql, mysql-b; "
        "set bindings['order-service/mysql']"
    ]
    assert (gaps[0].kind, gaps[0].options, gaps[0].default) == (
        "binding",
        ("mysql", "mysql-b"),
        "mysql",
    )


def test_explicit_binding_resolves_ambiguity(catalog):
    spec = spec_with(
        middleware=[
            {"template": "mysql"},
            {"template": "mysql", "name": "mysql-b"},
            {"template": "kafka"},
        ],
        bindings={"order-service/mysql": "mysql-b"},
    )
    bindings, gaps = match(spec, catalog)
    assert gaps == []
    assert providers(bindings)[("order-service", "mysql")] == "mysql-b"


def test_binding_to_wrong_capability_is_reported(catalog):
    spec = spec_with(bindings={"order-service/mysql": "kafka"})
    _, gaps = match(spec, catalog)
    assert [str(g) for g in gaps] == [
        "order-service/mysql: binding names 'kafka', which does not provide it"
    ]
    assert (gaps[0].kind, gaps[0].options) == ("binding", ("mysql",))
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_generate.py tests/test_matching.py
```

Expected: `6 failed` — gaps have no `kind`, `options`, or `default` yet.

**Step 3: Implement**

Create `platform/src/cagent/gaps.py`:

```python
"""Gaps: what stops a ClusterSpec from generating, each with the answers that would close it.

The generator reports every gap at once (design 6.3); Clarify asks about one at a time
(M7b design 5.1). str(gap) is the generator's plain message, for the CLI and escalations.
"""

from dataclasses import asdict, dataclass
from typing import Any

# Gaps a person can close by answering a question. unregistered and bind_render cannot be:
# the first needs a descriptor, the second is a catalog bug.
ANSWERABLE = frozenset(
    {
        "unknown_template",
        "unsupported_version",
        "unknown_preset",
        "no_source",
        "missing_env",
        "unknown_port",
        "binding",
        "version_mismatch",
        "no_provider",
    }
)


@dataclass(frozen=True)
class Gap:
    kind: str
    target: str  # a service, a middleware instance, or "consumer/capability"
    message: str
    options: tuple[str, ...] = ()  # the valid answers; empty means a free value
    default: str | None = None
    # missing_env: the variable; unknown_port: the port name; version_mismatch: the provider
    # instance; bind_render: the variable
    key: str | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "options": list(self.options)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Gap":
        return cls(**{**d, "options": tuple(d.get("options", ()))})
```

Replace `platform/src/cagent/matching.py`:

```python
"""Resolve each service requirement to the middleware instance that provides it."""

from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from cagent.catalog import Catalog, Requirement
from cagent.gaps import Gap
from cagent.spec import ClusterSpec, MiddlewareRef


@dataclass(frozen=True)
class Binding:
    consumer: str  # service name
    requirement: Requirement
    provider: str  # middleware instance name


def instance_version(ref: MiddlewareRef, catalog: Catalog) -> str:
    return ref.version or catalog.middleware[ref.template].default_version


def _satisfies(version: str, specifier: str | None) -> bool:
    if specifier is None:
        return True
    try:
        return Version(version) in SpecifierSet(specifier)
    except InvalidVersion:
        return False


def _fitting_versions(versions: list[str], specifier: str | None) -> tuple[str, ...]:
    """The versions satisfying specifier, lowest first."""
    fit = [v for v in versions if _satisfies(v, specifier)]
    return tuple(sorted(fit, key=Version))


def match(spec: ClusterSpec, catalog: Catalog) -> tuple[list[Binding], list[Gap]]:
    """Return one binding per satisfied requirement, and a gap per unsatisfied one.

    Services and templates missing from the catalog are skipped here; the generator
    reports them.
    """
    instances = {m.instance: m for m in spec.middleware if m.template in catalog.middleware}
    bindings: list[Binding] = []
    gaps: list[Gap] = []
    for svc in spec.services:
        desc = catalog.services.get(svc.name)
        if desc is None:
            continue
        for req in desc.requires:
            key = f"{svc.name}/{req.capability}"
            providers = [
                name
                for name, ref in instances.items()
                if catalog.middleware[ref.template].provides.capability == req.capability
            ]
            if key in spec.bindings:
                chosen = spec.bindings[key]
                if chosen not in providers:
                    gaps.append(
                        Gap(
                            "binding",
                            key,
                            f"{key}: binding names {chosen!r}, which does not provide it",
                            tuple(providers),
                            providers[0] if providers else None,
                        )
                    )
                    continue
                providers = [chosen]
            fitting = [
                p
                for p in providers
                if _satisfies(instance_version(instances[p], catalog), req.version)
            ]
            if len(fitting) == 1:
                bindings.append(Binding(svc.name, req, fitting[0]))
            elif fitting:
                gaps.append(
                    Gap(
                        "binding",
                        key,
                        f"{key}: ambiguous, candidates {', '.join(fitting)}; set bindings[{key!r}]",
                        tuple(fitting),
                        fitting[0],
                    )
                )
            elif providers:
                found = ", ".join(
                    f"{p} is {instance_version(instances[p], catalog)}" for p in providers
                )
                tpl = catalog.middleware[instances[providers[0]].template]
                versions = _fitting_versions(tpl.versions, req.version)
                gaps.append(
                    Gap(
                        "version_mismatch",
                        key,
                        f"{key}: needs version {req.version}; {found}",
                        versions,
                        versions[-1] if versions else None,
                        providers[0],
                    )
                )
            else:
                templates = tuple(
                    sorted(
                        t.name
                        for t in catalog.middleware.values()
                        if t.provides.capability == req.capability
                        and _fitting_versions(t.versions, req.version)
                    )
                )
                gaps.append(
                    Gap(
                        "no_provider",
                        key,
                        f"{key}: no middleware in the spec provides {req.capability}",
                        templates,
                        templates[0] if templates else None,
                    )
                )
    return bindings, gaps
```

Replace `platform/src/cagent/generate.py`:

```python
"""Generate a compose document from a ClusterSpec and the catalog."""

import difflib
import hashlib
import re
from collections import defaultdict
from typing import Any

import yaml

from cagent.catalog import Catalog, Healthcheck
from cagent.gaps import Gap
from cagent.matching import Binding, match
from cagent.render import RenderError, escape_compose, render
from cagent.spec import ClusterSpec, Source

# Shared healthcheck timing: up to ~2.5 minutes after a 10 s grace period.
HEALTH_TIMING = {"interval": "5s", "timeout": "3s", "retries": 30, "start_period": "10s"}


class GenerationError(Exception):
    """The spec cannot be turned into a compose file. gaps lists every reason."""

    def __init__(self, gaps: list[Gap]):
        super().__init__("\n".join(str(g) for g in gaps))
        self.gaps = gaps

    @property
    def problems(self) -> list[str]:
        return [str(g) for g in self.gaps]


def image_tag(name: str, source: Source) -> str:
    """Tag the Runner builds for a service source (M4). Docker tags allow [A-Za-z0-9_.-]."""
    if source.kind == "workdir":
        ref = hashlib.sha256(source.ref.encode()).hexdigest()[:12]
    else:
        ref = re.sub(r"[^A-Za-z0-9_.-]", "-", source.ref)
    return f"cagent/{name}:{f'{source.kind}-{ref}'[:128]}"


def _healthcheck(hc: Healthcheck, ports: dict[str, int], values: dict[str, str]) -> dict[str, Any]:
    if hc.cmd is not None:
        test = [escape_compose(render(part, values)) for part in hc.cmd]
    else:
        port = ports[hc.port] if hc.port else next(iter(ports.values()))
        url = f"http://localhost:{port}{hc.http}"
        probe = f"test \"$(curl -s -o /dev/null -w '%{{http_code}}' {url})\" = {hc.expect}"
        test = ["CMD-SHELL", escape_compose(probe)]
    return {"test": test, **HEALTH_TIMING}


def generate(spec: ClusterSpec, catalog: Catalog) -> dict[str, Any]:
    """Return the compose document as a dict. Raises GenerationError listing every problem."""
    gaps: list[Gap] = []
    services: dict[str, dict[str, Any]] = {}

    for ref in spec.middleware:
        tpl = catalog.middleware.get(ref.template)
        if tpl is None:
            names = sorted(catalog.middleware)
            closest = difflib.get_close_matches(ref.template, names, n=1, cutoff=0)
            gaps.append(
                Gap(
                    "unknown_template",
                    ref.instance,
                    f"{ref.instance}: unknown middleware template {ref.template!r}",
                    tuple(names),
                    closest[0] if closest else None,
                )
            )
            continue
        version = ref.version or tpl.default_version
        if version not in tpl.versions:
            gaps.append(
                Gap(
                    "unsupported_version",
                    ref.instance,
                    f"{ref.instance}: {tpl.name} {version} unsupported; "
                    f"use {', '.join(tpl.versions)}",
                    tuple(tpl.versions),
                    tpl.default_version,
                )
            )
            continue
        preset = tpl.presets.get(ref.preset or tpl.default_preset)
        if preset is None:
            gaps.append(
                Gap(
                    "unknown_preset",
                    ref.instance,
                    f"{ref.instance}: unknown preset {ref.preset!r}; use {', '.join(tpl.presets)}",
                    tuple(tpl.presets),
                    tpl.default_preset,
                )
            )
            continue
        values = {"host": ref.instance, "port": str(tpl.port), **tpl.connection}
        svc: dict[str, Any] = {"image": tpl.image.replace("{version}", version)}
        if tpl.command:
            svc["command"] = [escape_compose(c) for c in tpl.command]
        if tpl.env:
            svc["environment"] = {
                k: escape_compose(render(v, values)) for k, v in sorted(tpl.env.items())
            }
        if ref.publish:
            svc["ports"] = [f"{ref.publish}:{tpl.port}"]
        svc["healthcheck"] = _healthcheck(tpl.healthcheck, {"default": tpl.port}, values)
        svc["mem_limit"] = preset.mem_limit
        svc["cpus"] = preset.cpus
        services[ref.instance] = svc

    bindings, match_gaps = match(spec, catalog)
    gaps += match_gaps
    by_consumer: dict[str, list[Binding]] = defaultdict(list)
    for b in bindings:
        by_consumer[b.consumer].append(b)
    instances = {m.instance: m for m in spec.middleware}

    for ref in spec.services:
        desc = catalog.services.get(ref.name)
        if desc is None:
            gaps.append(Gap("unregistered", ref.name, f"{ref.name}: not registered in the catalog"))
            continue
        if getattr(desc.sources, ref.source.kind) is None:
            declared = tuple(
                k for k in ("release", "snapshot", "ci", "workdir") if getattr(desc.sources, k)
            )
            gaps.append(
                Gap(
                    "no_source",
                    ref.name,
                    f"{ref.name}: its descriptor declares no {ref.source.kind} source",
                    declared,
                    declared[0] if declared else None,
                )
            )
        ports = {p.name: p.container for p in desc.runtime.ports}
        env = dict(desc.runtime.env.defaults)
        depends: dict[str, dict[str, str]] = {}
        for b in by_consumer[ref.name]:
            tpl = catalog.middleware[instances[b.provider].template]
            values = {"host": b.provider, "port": str(tpl.port), **tpl.connection}
            for var, template in b.requirement.bind.items():
                try:
                    env[var] = render(template, values)
                except RenderError as e:
                    key = f"{ref.name}/{b.requirement.capability}"
                    gaps.append(Gap("bind_render", key, f"{key}: {var}: {e}", key=var))
            depends[b.provider] = {"condition": "service_healthy"}
        env.update(ref.env)
        for var in desc.runtime.env.required:
            if var not in env:
                gaps.append(
                    Gap(
                        "missing_env",
                        ref.name,
                        f"{ref.name}: missing required env {var}",
                        key=var,
                    )
                )
        for name in sorted(set(ref.publish) - set(ports)):
            gaps.append(
                Gap(
                    "unknown_port",
                    ref.name,
                    f"{ref.name}: cannot publish unknown port {name}; "
                    f"ports are {', '.join(ports) or 'none'}",
                    tuple(ports),
                    next(iter(ports)) if len(ports) == 1 else None,
                    name,
                )
            )

        svc = {"image": image_tag(ref.name, ref.source)}
        if env:
            svc["environment"] = {k: escape_compose(v) for k, v in sorted(env.items())}
        published = [f"{host}:{ports[n]}" for n, host in sorted(ref.publish.items()) if n in ports]
        if published:
            svc["ports"] = published
        if depends:
            svc["depends_on"] = dict(sorted(depends.items()))
        if desc.runtime.healthcheck is not None:
            svc["healthcheck"] = _healthcheck(desc.runtime.healthcheck, ports, {})
        services[ref.name] = svc

    if gaps:
        raise GenerationError(gaps)
    return {"name": spec.name, "services": dict(sorted(services.items()))}


def to_yaml(compose: dict[str, Any]) -> str:
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False, allow_unicode=True)
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `200 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/gaps.py platform/src/cagent/generate.py platform/src/cagent/matching.py platform/tests/test_generate.py platform/tests/test_matching.py
git commit -m "feat(platform): report generator problems as gaps with their answers"
```

---

### Task 9: Turn thinking off per call

Measured while planning (see Measurements): with thinking on, `qwen3.5` spent minutes on extraction and drafting; with it off, seconds, and the answers were right. `think=False` maps to `reasoning_effort="none"`, which LiteLLM sends to Ollama as `think: false`; for other providers nothing is sent. Diagnosis (M7a) passes nothing, so it keeps the model's default.

**Files:**
- Modify (full files below): `platform/src/cagent/llm.py`, `platform/tests/fakellm.py`, `platform/tests/test_llm.py`

**Step 1: Write the failing tests**

Replace `platform/tests/fakellm.py`:

```python
"""A scripted stand-in for LiteLLM: each model answers from its own queue."""

from typing import Any

from pydantic import ValidationError

from cagent.llm import ModelConfig, SchemaError, TierConfig

CONFIG = ModelConfig(
    tiers={"small": TierConfig(model="fake/small"), "large": TierConfig(model="fake/large")}
)


class FakeCompleter:
    def __init__(self, script: dict[str, list[Any]]):
        """script maps a model to its answers in order: a dict (validated against the
        requested schema) or an exception instance to raise."""
        self.script = {model: list(answers) for model, answers in script.items()}
        self.calls: list[tuple[str, str]] = []  # (model, user prompt)
        self.thinks: list[bool | None] = []

    async def complete(self, model, system, user, schema, timeout_s, think=None):
        self.calls.append((model, user))
        self.thinks.append(think)
        answer = self.script[model].pop(0)
        if isinstance(answer, Exception):
            raise answer
        try:
            return schema.model_validate(answer)
        except ValidationError as e:  # as LiteLLMCompleter reports a bad answer
            raise SchemaError(str(e)) from e

    @property
    def models(self) -> list[str]:
        return [m for m, _ in self.calls]
```

Replace `platform/tests/test_llm.py`:

```python
import pytest
from pydantic import BaseModel

from cagent.llm import (
    LiteLLMCompleter,
    ModelConfig,
    ModelUnavailable,
    Router,
    SchemaError,
    TierConfig,
    load_model_config,
)

from .conftest import REPO
from .fakellm import CONFIG, FakeCompleter


class Answer(BaseModel):
    text: str


async def test_first_valid_answer_wins():
    fake = FakeCompleter({"fake/small": [{"text": "hi"}]})
    answer, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert (answer.text, tier) == ("hi", "small")


async def test_invalid_answer_is_retried_once_then_moves_up():
    fake = FakeCompleter(
        {
            "fake/small": [SchemaError("bad"), SchemaError("bad again")],
            "fake/large": [{"text": "ok"}],
        }
    )
    answer, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/small", "fake/small", "fake/large"]


async def test_transport_errors_move_up_without_retry():
    fake = FakeCompleter({"fake/small": [TimeoutError()], "fake/large": [{"text": "ok"}]})
    _, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/small", "fake/large"]


async def test_tiers_without_the_capability_are_skipped():
    config = ModelConfig(
        tiers={
            "small": TierConfig(model="fake/small", capabilities=[]),
            "large": TierConfig(model="fake/large"),
        }
    )
    fake = FakeCompleter({"fake/large": [{"text": "ok"}]})
    _, tier = await Router(config, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/large"]


async def test_top_tier_failing_raises():
    fake = FakeCompleter({"fake/large": [SchemaError("x"), SchemaError("y")]})
    with pytest.raises(ModelUnavailable, match="large: invalid answer"):
        await Router(CONFIG, fake).ask("sys", "user", Answer, start="large")


def test_tier_at_clamps_to_the_ends():
    router = Router(CONFIG, FakeCompleter({}))
    assert [router.tier_at(i) for i in (-1, 0, 1, 5)] == ["small", "small", "large", "large"]


def test_shipped_model_config_loads():
    config = load_model_config(REPO / "platform" / "models.yaml")
    assert config.order == ["small", "large"]
    assert all(t.model.startswith("ollama_chat/") for t in config.tiers.values())


async def test_think_is_passed_to_the_completer():
    fake = FakeCompleter({"fake/large": [{"text": "a"}, {"text": "b"}]})
    router = Router(CONFIG, fake)
    await router.ask("sys", "user", Answer, start="large", think=False)
    await router.ask("sys", "user", Answer, start="large")
    assert fake.thinks == [False, None]


@pytest.mark.parametrize(
    "model, think, sent",
    [
        ("ollama_chat/qwen3.5:latest", False, {"reasoning_effort": "none"}),
        ("ollama_chat/qwen3.5:latest", None, {}),
        ("bedrock/anthropic.claude", False, {}),  # Claude does not think unless asked
    ],
)
async def test_litellm_turns_thinking_off_only_for_ollama(monkeypatch, model, think, sent):
    import litellm

    seen = {}

    async def acompletion(**kwargs):
        seen.update(kwargs)
        message = type("M", (), {"content": '{"text": "ok"}'})
        return type("R", (), {"choices": [type("C", (), {"message": message})]})

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    await LiteLLMCompleter().complete(model, "sys", "user", Answer, 5, think=think)
    assert {k: seen[k] for k in ("reasoning_effort",) if k in seen} == sent
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_llm.py
```

Expected: `4 failed` — `ask` and `complete` take no `think`.

**Step 3: Implement**

Replace `platform/src/cagent/llm.py`:

```python
"""Model gateway and routing (design section 7).

Code names tiers, never models. A YAML file maps each tier to a LiteLLM model string, so
moving from local Ollama to Bedrock changes configuration, not code. Every answer must parse
into a Pydantic schema; an invalid answer is retried once on the same tier, then the call
moves up a tier, and only the top tier failing raises.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

M = TypeVar("M", bound=BaseModel)


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str  # LiteLLM model string, e.g. ollama_chat/qwen3.5:latest or bedrock/…
    capabilities: list[str] = ["json"]
    timeout_s: float = 300


class ModelConfig(BaseModel):
    """tiers: name → model, in escalation order (first is cheapest)."""

    model_config = ConfigDict(extra="forbid")

    tiers: dict[str, TierConfig]

    @property
    def order(self) -> list[str]:
        return list(self.tiers)


def load_model_config(path: Path) -> ModelConfig:
    return ModelConfig.model_validate(yaml.safe_load(path.read_text()))


class SchemaError(Exception):
    """The model's answer did not match the schema."""


class ModelUnavailable(Exception):
    """Every eligible tier failed to give a valid answer."""


class Completer(Protocol):
    """One structured completion on one model; LiteLLMCompleter, or a fake in tests.
    think=False asks the model not to reason first; None leaves the model's default."""

    async def complete(
        self,
        model: str,
        system: str,
        user: str,
        schema: type[M],
        timeout_s: float,
        think: bool | None = None,
    ) -> M: ...


class LiteLLMCompleter:
    async def complete(
        self,
        model: str,
        system: str,
        user: str,
        schema: type[M],
        timeout_s: float,
        think: bool | None = None,
    ) -> M:
        import litellm  # imported lazily: heavy, and tests never need it

        litellm.suppress_debug_info = True
        extra = {}
        if think is False and model.startswith("ollama"):
            # LiteLLM maps any effort but low/medium/high to Ollama's think=false. Claude on
            # Bedrock does not think unless asked, so nothing is sent there.
            extra["reasoning_effort"] = "none"
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            temperature=0,
            timeout=timeout_s,
            **extra,
        )
        try:
            return schema.model_validate_json(response.choices[0].message.content or "")
        except ValidationError as e:
            raise SchemaError(str(e)) from e


class Router:
    def __init__(self, config: ModelConfig, completer: Completer):
        self.config, self.completer = config, completer

    def tier_at(self, index: int) -> str:
        """The tier at a position in the escalation order, clamped to the ends."""
        order = self.config.order
        return order[max(0, min(index, len(order) - 1))]

    async def ask(
        self,
        system: str,
        user: str,
        schema: type[M],
        start: str,
        needs: Sequence[str] = ("json",),
        think: bool | None = None,
    ) -> tuple[M, str]:
        """Return the first valid answer and the tier that gave it, starting at `start`.
        think=False suits extraction-like calls: a reasoning model answered them as well in
        seconds instead of minutes (M7b plan, measurements)."""
        order = self.config.order
        errors: list[str] = []
        for tier in order[order.index(start) :]:
            cfg = self.config.tiers[tier]
            if not set(needs) <= set(cfg.capabilities):
                errors.append(f"{tier}: lacks {sorted(set(needs) - set(cfg.capabilities))}")
                continue
            for _ in range(2):  # one retry on the same tier before moving up
                try:
                    return await self.completer.complete(
                        cfg.model, system, user, schema, cfg.timeout_s, think
                    ), tier
                except SchemaError as e:
                    errors.append(f"{tier}: invalid answer: {str(e)[:200]}")
                except Exception as e:  # network, timeout, model not found: try the next tier
                    errors.append(f"{tier}: {type(e).__name__}: {str(e)[:200]}")
                    break
        raise ModelUnavailable("; ".join(errors))
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `204 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/llm.py platform/tests/fakellm.py platform/tests/test_llm.py
git commit -m "feat(platform): let a call turn model thinking off"
```

> **Checkpoint.** Tasks 7–9 done. Verify the files and `make platform-test`.

---

### Task 10: Clarify: questions, answers, and extraction

`extract` asks the top tier once, thinking off. `apply_answer` is pure: it checks an answer against the gap's options (or requires a value) and returns a new spec, so no model reads the user's answers.

**Files:**
- Create: `platform/src/cagent/clarify.py`, `platform/tests/test_clarify.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_clarify.py`:

```python
import pytest

from cagent.clarify import AnswerError, apply_answer, extract, question
from cagent.gaps import Gap
from cagent.generate import GenerationError, generate
from cagent.llm import ModelUnavailable, Router

from .conftest import spec_with
from .fakellm import CONFIG, FakeCompleter

ORDERS_RELEASE = {"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}


def gaps_of(spec, catalog) -> list[Gap]:
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    return err.value.gaps


def answer_all(spec, catalog, answers=None):
    """Close gaps one at a time, taking the default unless answers names a value."""
    answers = answers or {}
    for _ in range(20):
        try:
            generate(spec, catalog)
            return spec
        except GenerationError as e:
            gap = e.gaps[0]
            spec = apply_answer(spec, gap, answers.get((gap.kind, gap.target, gap.key)))
    raise AssertionError("did not converge")


def test_question_names_the_gap_and_what_to_choose():
    gap = Gap("unknown_template", "mysq", "mysq: unknown middleware template 'mysq'")
    assert question(gap) == (
        "mysq: unknown middleware template 'mysq'. Which template should mysq use?"
    )
    gap = Gap("missing_env", "web", "web: missing required env MODE", key="MODE")
    assert question(gap).endswith("What value should web get for MODE?")


def test_defaults_close_every_choice_gap(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "release", "ref": "1.4.2"},
                "env": {"SPRING_PROFILES_ACTIVE": "dev"},
                "publish": {"grpc": 18080},
            }
        ],
        middleware=[{"template": "mysq", "version": "5.7"}, {"template": "redis", "preset": "x"}],
    )
    done = answer_all(spec, catalog)
    assert {m.template for m in done.middleware} == {"mysql", "redis", "kafka"}
    assert done.services[0].publish == {"http": 18080}
    generate(done, catalog)


def test_missing_env_needs_a_value(catalog):
    spec = spec_with(services=[ORDERS_RELEASE])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "missing_env")
    with pytest.raises(AnswerError, match="an answer is required"):
        apply_answer(spec, gap, None)
    with pytest.raises(AnswerError, match="an answer is required"):
        apply_answer(spec, gap, "  ")
    after = apply_answer(spec, gap, "dev")
    assert after.services[0].env["SPRING_PROFILES_ACTIVE"] == "dev"
    assert spec.services[0].env == {}  # the input spec is unchanged


def test_choice_answers_must_be_an_option(catalog):
    spec = spec_with(middleware=[{"template": "mysql", "version": "5.7"}, {"template": "kafka"}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "unsupported_version")
    with pytest.raises(AnswerError, match="choose one of 8.0, 8.4"):
        apply_answer(spec, gap, "9.0")
    assert apply_answer(spec, gap, "8.0").middleware[0].version == "8.0"


@pytest.mark.parametrize(
    "middleware, bindings, kind, answer, check",
    [
        (
            [{"template": "mysql"}, {"template": "mysql", "name": "b"}, {"template": "kafka"}],
            {},
            "binding",
            "b",
            lambda s: s.bindings == {"order-service/mysql": "b"},
        ),
        (
            [{"template": "mysql", "version": "8.0"}, {"template": "kafka"}],
            {},
            "version_mismatch",
            "8.4",
            lambda s: s.middleware[0].version == "8.4",
        ),
        (
            [{"template": "mysql"}],
            {},
            "no_provider",
            "kafka",
            lambda s: [m.template for m in s.middleware] == ["mysql", "kafka"],
        ),
    ],
)
def test_matching_gaps(catalog, middleware, bindings, kind, answer, check):
    if kind == "version_mismatch":
        desc = catalog.services["order-service"]
        req = desc.requires[0].model_copy(update={"version": ">=8.4"})
        catalog.services["order-service"] = desc.model_copy(
            update={"requires": [req, desc.requires[1]]}
        )
    spec = spec_with(middleware=middleware, bindings=bindings)
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == kind)
    assert check(apply_answer(spec, gap, answer))


def test_no_provider_names_a_second_instance_apart(catalog):
    spec = spec_with(middleware=[{"template": "kafka", "name": "mysql"}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "no_provider")
    after = apply_answer(spec, gap, "mysql")
    assert [m.instance for m in after.middleware] == ["mysql", "mysql-2"]


def test_no_source_switches_the_source_kind(catalog):
    desc = catalog.services["order-service"]
    catalog.services["order-service"] = desc.model_copy(
        update={"sources": desc.sources.model_copy(update={"ci": None})}
    )
    spec = spec_with(services=[{"name": "order-service", "source": {"kind": "ci", "ref": "1.4.2"}}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "no_source")
    assert apply_answer(spec, gap, "release").services[0].source.kind == "release"


@pytest.mark.parametrize("kind", ["unregistered", "bind_render"])
def test_unanswerable_gaps_are_refused(kind):
    with pytest.raises(AnswerError, match="cannot be answered"):
        apply_answer(spec_with(), Gap(kind, "x", "x"), "y")


def test_choice_without_options_cannot_be_answered():
    gap = Gap("binding", "order-service/mysql", "no candidates")
    with pytest.raises(AnswerError, match="cannot be answered"):
        apply_answer(spec_with(), gap, "mysql")


PURPOSE = "Run order-service 1.4.2 against MySQL 8.0 and Kafka; check that orders are stored."


def extraction(**overrides):
    return {
        "name": "Orders POC",
        "services": [{"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}],
        "middleware": [{"template": "mysql", "version": "8.0"}, {"template": "kafka"}],
        "checks": ["orders are stored"],
        **overrides,
    }


async def test_extract_turns_the_answer_into_a_spec(catalog):
    fake = FakeCompleter({"fake/large": [extraction()]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert spec.name == "orders-poc" and spec.purpose == PURPOSE
    assert [(s.name, s.source.kind, s.source.ref) for s in spec.services] == [
        ("order-service", "release", "1.4.2")
    ]
    assert [(m.template, m.version) for m in spec.middleware] == [("mysql", "8.0"), ("kafka", None)]
    assert spec.checks == ["orders are stored"]
    assert fake.models == ["fake/large"]  # extraction starts on the top tier
    assert fake.thinks == [False]


async def test_extract_prompt_shows_the_catalog_and_the_purpose(catalog):
    fake = FakeCompleter({"fake/large": [extraction()]})
    await extract(Router(CONFIG, fake), PURPOSE, catalog)
    prompt = fake.calls[0][1]
    assert "- order-service: sources release, snapshot, ci, workdir" in prompt
    assert "- mysql: provides mysql, versions 8.0, 8.4" in prompt
    assert prompt.endswith(PURPOSE)


async def test_extract_keeps_unknown_names_so_they_become_questions(catalog):
    answer = extraction(
        services=[
            {"name": "Billing Service", "source": {"kind": "workdir", "ref": "/src/billing"}},
            {"name": "billing-service", "source": {"kind": "workdir", "ref": "/src/billing"}},
        ],
        middleware=[{"template": "Postgres"}, {"template": "postgres"}, {"template": "mysql"}],
    )
    fake = FakeCompleter({"fake/large": [answer]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert [s.name for s in spec.services] == ["billing-service"]
    assert [m.template for m in spec.middleware] == ["postgres", "mysql"]
    gaps = {g.kind for g in gaps_of(spec, catalog)}
    assert {"unregistered", "unknown_template"} <= gaps


async def test_extract_moves_middleware_listed_as_a_service(catalog):
    # Seen from qwen3.5 without thinking: MySQL listed as a service with a release source.
    answer = extraction(
        services=[ORDERS_RELEASE, {"name": "mysql", "source": {"kind": "release", "ref": "8.0"}}],
        middleware=[{"template": "mysql", "version": "8.0"}, {"template": "order-service"}],
    )
    spec = await extract(Router(CONFIG, FakeCompleter({"fake/large": [answer]})), "x", catalog)
    assert [s.name for s in spec.services] == ["order-service"]
    assert [(m.template, m.version) for m in spec.middleware] == [("mysql", "8.0")]


async def test_extract_gives_up_when_every_tier_fails(catalog):
    bad = {"name": "x", "services": "not a list"}
    fake = FakeCompleter({"fake/large": [bad, bad]})
    with pytest.raises(ModelUnavailable):
        await extract(Router(CONFIG, fake), PURPOSE, catalog)


async def test_extract_retries_an_empty_ref(catalog):
    empty = extraction(
        services=[{"name": "order-service", "source": {"kind": "release", "ref": ""}}]
    )
    fake = FakeCompleter({"fake/large": [empty, extraction()]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert spec.services[0].source.ref == "1.4.2" and len(fake.calls) == 2
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_clarify.py
```

Expected: collection error — `No module named 'cagent.clarify'`.

**Step 3: Implement**

Create `platform/src/cagent/clarify.py`:

```python
"""Clarify: a purpose sentence becomes a ClusterSpec (M7b design 5).

A model reads the purpose once and drafts the spec from catalog names. After that, every
question comes from a structured generator gap, with its options and default, and every
answer is a plain value checked here: no model reads the user's answers.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from cagent.catalog import Catalog
from cagent.gaps import ANSWERABLE, Gap
from cagent.llm import Router
from cagent.spec import ClusterSpec

_ASK = {
    "unknown_template": "Which template should {target} use?",
    "unsupported_version": "Which version should {target} use?",
    "unknown_preset": "Which preset should {target} use?",
    "no_source": "Which source should {target} build from?",
    "missing_env": "What value should {target} get for {key}?",
    "unknown_port": "Which port should {target} publish instead of {key}?",
    "binding": "Which instance should {target} use?",
    "version_mismatch": "Which version should {key} use?",
    "no_provider": "Which middleware should provide it?",
}


class AnswerError(ValueError):
    """The answer does not close the gap; ask again with this message."""


def question(gap: Gap) -> str:
    return f"{gap.message}. {_ASK[gap.kind].format(target=gap.target, key=gap.key)}"


def _middleware(doc: dict[str, Any], instance: str) -> dict[str, Any]:
    return next(m for m in doc["middleware"] if (m.get("name") or m["template"]) == instance)


def _service(doc: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in doc["services"] if s["name"] == name)


def apply_answer(spec: ClusterSpec, gap: Gap, answer: str | None) -> ClusterSpec:
    """Return a new spec with the answer applied. None takes the gap's default."""
    if gap.kind not in ANSWERABLE or (gap.kind != "missing_env" and not gap.options):
        raise AnswerError(f"{gap.message}: this gap cannot be answered here")
    value = gap.default if answer is None else answer.strip()
    if not value:
        raise AnswerError("an answer is required")
    if gap.options and value not in gap.options:
        raise AnswerError(f"choose one of {', '.join(gap.options)}")

    doc = spec.model_dump()
    match gap.kind:
        case "unknown_template":
            _middleware(doc, gap.target)["template"] = value
        case "unsupported_version":
            _middleware(doc, gap.target)["version"] = value
        case "version_mismatch":
            _middleware(doc, gap.key)["version"] = value
        case "unknown_preset":
            _middleware(doc, gap.target)["preset"] = value
        case "no_source":
            _service(doc, gap.target)["source"]["kind"] = value
        case "missing_env":
            _service(doc, gap.target)["env"][gap.key] = value
        case "unknown_port":
            publish = _service(doc, gap.target)["publish"]
            publish[value] = publish.pop(gap.key)
        case "binding":
            doc["bindings"][gap.target] = value
        case "no_provider":
            taken = {s["name"] for s in doc["services"]} | {
                m.get("name") or m["template"] for m in doc["middleware"]
            }
            name, n = value, 1
            while name in taken:
                n += 1
                name = f"{value}-{n}"
            doc["middleware"].append({"template": value, "name": None if n == 1 else name})
    return ClusterSpec.model_validate(doc)


class _Source(BaseModel):
    kind: Literal["release", "snapshot", "ci", "workdir"]
    ref: str = Field(min_length=1)  # an empty ref is a schema error, so the model retries


class _Service(BaseModel):
    name: str
    source: _Source


class _Middleware(BaseModel):
    template: str
    version: str | None = None


class Extraction(BaseModel):
    """The model's draft. Names are normalized and checked after it answers."""

    name: str
    services: list[_Service] = []
    middleware: list[_Middleware] = []
    checks: list[str] = []


EXTRACT_SYSTEM = (
    "You turn a developer's description of a throwaway test cluster into a cluster spec. "
    "Use catalog names when the text means them, and keep any other name as written. "
    "A service built from a local directory has source kind workdir with the directory as "
    "ref; a released version is kind release with the version as ref; a snapshot version is "
    "kind snapshot. List only the middleware the text asks for: what services need is added "
    "later. checks are the acceptance checks the text states, one sentence each. "
    "Answer only with JSON matching the schema."
)


def _slug(text: str) -> str:
    """A name matching the catalog's pattern: lowercase letters, digits, - and _."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.strip().lower()).strip("-_")
    return slug or "cluster"


def extract_prompt(purpose: str, catalog: Catalog) -> str:
    kinds = ("release", "snapshot", "ci", "workdir")
    services = [
        f"- {d.name}: sources {', '.join(k for k in kinds if getattr(d.sources, k))}"
        for d in sorted(catalog.services.values(), key=lambda d: d.name)
    ]
    templates = [
        f"- {t.name}: provides {t.provides.capability}, versions {', '.join(t.versions)}"
        for t in sorted(catalog.middleware.values(), key=lambda t: t.name)
    ]
    return "\n".join(
        [
            "Services in the catalog:",
            *(services or ["- (none)"]),
            "Middleware templates:",
            *(templates or ["- (none)"]),
            "Purpose:",
            purpose,
        ]
    )


async def extract(router: Router, purpose: str, catalog: Catalog) -> ClusterSpec:
    """Draft a spec from the purpose with one call on the top tier (design 4, step 1).
    Unknown names stay, so the generator reports them as gaps to ask about."""
    draft, _ = await router.ask(
        EXTRACT_SYSTEM,
        extract_prompt(purpose, catalog),
        Extraction,
        start=router.config.order[-1],
        think=False,
    )
    services: dict[str, dict[str, Any]] = {}
    middleware: dict[str, dict[str, Any]] = {}
    for m in draft.middleware:
        template = _slug(m.template)
        if template not in catalog.services:
            middleware.setdefault(template, {"template": template, "version": m.version})
    for svc in draft.services:
        name = _slug(svc.name)
        if name in catalog.middleware and name not in catalog.services:
            # Models listed MySQL or Kafka as a service too: it is the template.
            middleware.setdefault(name, {"template": name, "version": None})
            continue
        services.setdefault(name, {"name": name, "source": svc.source.model_dump()})
    return ClusterSpec.model_validate(
        {
            "name": _slug(draft.name),
            "purpose": purpose,
            "services": list(services.values()),
            "middleware": list(middleware.values()),
            "checks": [c for c in draft.checks if c.strip()],
        }
    )
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `222 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/clarify.py platform/tests/test_clarify.py
git commit -m "feat(platform): clarify a purpose with one extraction and plain questions"
```

---

### Task 11: Draft a descriptor for an unregistered project

The skeleton (module, name, stack, build, output) is code. The model answers a flat schema for how the service runs; code converts and checks it, drops a bound variable's default, and asks once more with the reasons when a check fails.

**Files:**
- Create: `platform/src/cagent/draft.py`, `platform/tests/test_draft.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_draft.py`:

```python
import pytest
import yaml

from cagent.catalog import ServiceDescriptor
from cagent.draft import (
    DraftError,
    draft,
    env_placeholders,
    pick,
    read_back,
    resource_note,
    skeletons,
)
from cagent.llm import ModelUnavailable, Router

from .fakellm import CONFIG, FakeCompleter

BOOT_POM = (
    "<project xmlns='http://maven.apache.org/POM/4.0.0'><artifactId>{name}</artifactId>"
    "<build><plugins><plugin><groupId>org.springframework.boot</groupId>"
    "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build></project>"
)
PLAIN_POM = "<project><artifactId>{name}</artifactId></project>"
AGGREGATOR = (
    "<project><artifactId>shop</artifactId><packaging>pom</packaging>"
    "<modules><module>api</module><module>lib</module><module>worker</module></modules>"
    "<build><pluginManagement><plugins><plugin>"
    "<artifactId>spring-boot-maven-plugin</artifactId>"
    "</plugin></plugins></pluginManagement></build></project>"
)
APP_YML = (
    "server:\n  port: 8081\nspring:\n  datasource:\n    url: ${DB_URL}\n"
    "    password: ${DB_PASSWORD}\n  kafka:\n    bootstrap-servers: ${KAFKA:localhost:9092}\n"
    "app:\n  name: ${spring.application.name:orders}\n"
)


def inspection(files, modules=(), resources=(), truncated=()):
    return {
        "modules": list(modules),
        "files": files,
        "resources": list(resources),
        "truncated": list(truncated),
    }


SHOP = inspection(
    {
        "pom.xml": AGGREGATOR,
        "api/pom.xml": BOOT_POM.format(name="orders-api"),
        "api/src/main/resources/application.yml": APP_YML,
        "lib/pom.xml": PLAIN_POM.format(name="lib"),
        "worker/pom.xml": PLAIN_POM.format(name="worker"),
        "worker/Dockerfile": "FROM eclipse-temurin:21-jre\nCOPY target/worker.jar /app.jar\n",
    },
    modules=["api", "lib", "worker"],
    resources=["api/src/main/resources/db/migration/V1__init.sql", "cql/keyspace.cql"],
)


def test_single_spring_boot_project():
    [sk] = skeletons(inspection({"pom.xml": BOOT_POM.format(name="orders")}))
    assert (sk.name, sk.module, sk.stack) == ("orders", ".", "java-springboot")
    assert sk.workdir == {"build": "mvn -q package -DskipTests", "output": "target/*.jar"}


def test_maven_modules_skip_the_aggregator_and_libraries():
    api, worker = skeletons(SHOP)
    assert (api.name, api.stack, api.workdir) == (
        "orders-api",
        "java-springboot",
        {"build": "mvn -q -pl api -am package -DskipTests", "output": "api/target/*.jar"},
    )
    # The worker's Dockerfile copies a jar from target/, so the module is built first.
    assert (worker.name, worker.stack, worker.workdir) == (
        "worker",
        "dockerfile",
        {
            "build": "mvn -q -pl worker -am package -DskipTests",
            "dockerfile": "worker/Dockerfile",
            "context": "worker",
        },
    )


def test_gradle_modules_and_a_self_contained_dockerfile():
    files = {
        "settings.gradle.kts": 'include("web", ":lib:core", "ui")',
        "web/build.gradle.kts": 'plugins { id("org.springframework.boot") version "3.3.0" }',
        "lib/core/build.gradle.kts": "plugins { `java-library` }",
        "ui/package.json": "{}",
        "ui/Dockerfile": "FROM node:22\nCOPY . .\nRUN npm ci\n",
    }
    web, ui = skeletons(inspection(files, modules=["web", "lib/core", "ui"]))
    assert web.workdir == {"build": "./gradlew -q :web:bootJar", "output": "web/build/libs/*.jar"}
    assert (ui.stack, ui.workdir) == (
        "dockerfile",
        {"dockerfile": "ui/Dockerfile", "context": "ui"},
    )


def test_pick_matches_the_service_to_a_module():
    sks = skeletons(SHOP)
    assert pick(sks, "orders-api").module == "api"
    assert pick(sks, "orders_api").module == "api"  # close enough
    with pytest.raises(
        DraftError, match="no deployable module matches billing: orders-api, worker"
    ):
        pick(sks, "billing")
    single = skeletons(inspection({"Dockerfile": "FROM busybox"}))
    assert pick(single, "anything").module == "."
    with pytest.raises(DraftError, match="add a Dockerfile"):
        pick(skeletons(inspection({"package.json": "{}"})), "ui")


def test_env_placeholders_are_upper_case_variables():
    assert env_placeholders(APP_YML) == (["DB_PASSWORD", "DB_URL"], ["KAFKA"])


def answer(**overrides):
    return {
        "ports": [{"name": "http", "container": 8081}],
        "health_path": "/actuator/health",
        "requires": [
            {
                "capability": "mysql",
                "bind": {
                    "DB_URL": "jdbc:mysql://{{host}}:{{port}}/{{db}}",
                    "DB_PASSWORD": "{{password}}",
                },
            },
            {"capability": "kafka", "bind": {"KAFKA": "{{host}}:{{port}}"}},
        ],
        "probe_paths": ["/actuator/health"],
        **overrides,
    }


async def test_draft_combines_the_skeleton_and_the_models_runtime(catalog):
    fake = FakeCompleter({"fake/large": [answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.path == ".cagent/services/orders-api.yaml"
    assert d.descriptor.name == "orders-api" and d.descriptor.stack == "java-springboot"
    assert d.descriptor.sources.workdir.build == "mvn -q -pl api -am package -DskipTests"
    assert d.descriptor.requires[0].bind["DB_PASSWORD"] == "{{password}}"
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
    assert d.descriptor.probes[0].http.path == "/actuator/health"
    assert ServiceDescriptor.model_validate(yaml.safe_load(d.text)) == d.descriptor
    assert d.text.startswith("# Drafted by cagent")
    assert fake.thinks == [False]
    prompt = fake.calls[0][1]
    assert "--- api/src/main/resources/application.yml" in prompt
    assert "worker/pom.xml" not in prompt and "lib/pom.xml" not in prompt
    assert "without a default (set each one): DB_PASSWORD, DB_URL" in prompt
    assert "- mysql: {{host}}, {{port}}, {{db}}, {{password}}, {{user}}" in prompt


@pytest.mark.parametrize(
    "bad, why",
    [
        (answer(requires=[{"capability": "postgres"}]), "no template provides postgres"),
        (
            answer(requires=[{"capability": "mysql", "bind": {"DB_URL": "{{database}}"}}]),
            "mysql bind DB_URL uses unknown {{database}}",
        ),
        (answer(requires=[]), "DB_PASSWORD, DB_URL"),
        # Seen from qwen3.5: bind keyed by the template's values instead of the variables.
        (
            answer(requires=[{"capability": "mysql", "bind": {"host": "{{host}}"}}]),
            "bind keys are environment variables the service reads, such as DB_URL; host is not",
        ),
        (
            answer(env_defaults={"CACHE": "127.0.0.1:6379"}),
            "CACHE=127.0.0.1:6379 points at the container itself",
        ),
    ],
)
async def test_an_invalid_draft_is_retried_with_the_reason(catalog, bad, why):
    fake = FakeCompleter({"fake/large": [bad, answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.requires and len(fake.calls) == 2
    assert why in fake.calls[1][1]


async def test_a_bound_variable_drops_its_default_and_required_entry(catalog):
    copied = answer(env_defaults={"KAFKA": "localhost:9092"}, env_required=["DB_URL", "MODE"])
    fake = FakeCompleter({"fake/large": [copied]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.runtime.env.defaults == {} and d.descriptor.runtime.env.required == ["MODE"]
    assert len(fake.calls) == 1


async def test_a_path_wins_over_a_command_healthcheck(catalog):
    both = answer(health_cmd=["CMD", "curl", "-f", "http://localhost:8081/actuator/health"])
    d = await draft(
        Router(CONFIG, FakeCompleter({"fake/large": [both]})), "orders-api", SHOP, catalog
    )
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
    assert d.descriptor.runtime.healthcheck.cmd is None


async def test_a_descriptor_rule_broken_by_the_answer_is_retried(catalog):
    no_ports = answer(ports=[])  # an HTTP healthcheck needs a port
    fake = FakeCompleter({"fake/large": [no_ports, answer()]})
    d = await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)
    assert d.descriptor.runtime.ports and "needs at least one port" in fake.calls[1][1]


async def test_draft_gives_up_after_two_invalid_answers(catalog):
    bad = answer(requires=[{"capability": "postgres"}])
    fake = FakeCompleter({"fake/large": [bad, bad]})
    with pytest.raises(ModelUnavailable, match="no template provides postgres"):
        await draft(Router(CONFIG, fake), "orders-api", SHOP, catalog)


def saved(path, text):
    return inspection({path: text})


DOCKERFILE_DESC = (
    "name: ui\nkind: app\nstack: dockerfile\nsources:\n  workdir: { dockerfile: Dockerfile }\n"
    "runtime: {}\n"
)


RELEASE_ONLY = (
    "name: ui\nkind: app\nstack: java-springboot\n"
    "sources:\n  release: { nexus: 'a:b:{version}:jar' }\nruntime: {}\n"
)


def test_read_back_validates_the_file_on_disk():
    desc = read_back("ui", saved(".cagent/services/ui.yaml", DOCKERFILE_DESC))
    assert desc.stack == "dockerfile"
    # The single-service file is the fallback, as for the Runner.
    assert read_back("ui", saved(".cagent/service.yaml", DOCKERFILE_DESC)).name == "ui"


@pytest.mark.parametrize(
    "files, why",
    [
        ({}, "not saved yet"),
        ({".cagent/services/ui.yaml": "name: [unclosed"}, "not valid YAML"),
        ({".cagent/services/ui.yaml": DOCKERFILE_DESC.replace("ui", "web")}, "names web, not ui"),
        ({".cagent/services/ui.yaml": "name: ui\nkind: app\n"}, "Field required"),
        (
            {
                ".cagent/services/ui.yaml": DOCKERFILE_DESC.replace(
                    "runtime: {}", "runtime:\n  env: { defaults: { API_TOKEN: '******' } }"
                )
            },
            "masked secret",
        ),
        ({".cagent/services/ui.yaml": RELEASE_ONLY}, "declares no workdir source"),
    ],
)
def test_read_back_refuses_what_cannot_be_used(files, why):
    with pytest.raises(DraftError, match=why):
        read_back("ui", inspection(files))


def test_resource_note():
    note = resource_note(SHOP["resources"])
    assert "Flyway or Liquibase migrations run when the application starts" in note
    assert "cql/keyspace.cql" in note and "M7c" in note
    assert resource_note([]) == ""
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_draft.py
```

Expected: collection error — `No module named 'cagent.draft'`.

**Step 3: Implement**

Create `platform/src/cagent/draft.py`:

```python
"""Draft a descriptor for a project with none (M7b design 5.4).

The Runner's read-only inspect returns the project's build files. A deterministic skeleton
decides what is built and how: the module, name, stack, build command, and output. A model
fills in only how the service runs (ports, environment, healthcheck, requirements, probes),
and its answer is checked before the user sees it. The user saves the draft into the project;
read_back then validates the file actually on disk, which is what the Runner builds from.
"""

import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from cagent.catalog import (
    Catalog,
    Env,
    Healthcheck,
    HttpProbe,
    Port,
    Probe,
    Requirement,
    Runtime,
    ServiceDescriptor,
)
from cagent.llm import ModelUnavailable, Router
from cagent.render import placeholders

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[^}]*)?\}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_LOCAL = re.compile(r"\b(localhost|127\.0\.0\.1)\b")
_JAR_IN_DOCKERFILE = re.compile(r"target/|build/libs")
MASK = "******"  # what the Runner's redaction leaves in place of a secret


class DraftError(Exception):
    """No usable descriptor: say why, so the user can act."""


@dataclass(frozen=True)
class Skeleton:
    name: str
    module: str  # "." for the project root
    stack: str
    workdir: dict[str, str]


@dataclass(frozen=True)
class Draft:
    path: str  # where the user saves it, relative to the project root
    text: str
    descriptor: ServiceDescriptor


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_") or "app"


def _pom(text: str) -> tuple[str | None, bool, bool]:
    """(artifactId, packaging is pom, Spring Boot plugin in build/plugins)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, False, False
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]  # drop the Maven namespace
    plugins = root.findall("./build/plugins/plugin/artifactId")
    return (
        root.findtext("artifactId"),
        root.findtext("packaging") == "pom",
        any(p.text == "spring-boot-maven-plugin" for p in plugins),
    )


def _skeleton(module: str, files: dict[str, str]) -> Skeleton | None:
    def f(name: str) -> str | None:
        return files.get(name if module == "." else f"{module}/{name}")

    def path(name: str) -> str:
        return name if module == "." else f"{module}/{name}"

    pom, gradle = f("pom.xml"), f("build.gradle.kts") or f("build.gradle")
    artifact, aggregator, maven_boot = _pom(pom) if pom else (None, False, False)
    gradle_boot = gradle is not None and "org.springframework.boot" in gradle
    if module == ".":
        name, maven_build = artifact or "app", "mvn -q package -DskipTests"
        gradle_task = "./gradlew -q bootJar"
    else:
        name = artifact or module.rsplit("/", 1)[-1]
        maven_build = f"mvn -q -pl {module} -am package -DskipTests"
        gradle_task = f"./gradlew -q :{module.replace('/', ':')}:bootJar"
    if pom and maven_boot and not aggregator:
        return Skeleton(
            _slug(name),
            module,
            "java-springboot",
            {"build": maven_build, "output": path("target/*.jar")},
        )
    if gradle_boot and not pom:
        return Skeleton(
            _slug(name),
            module,
            "java-springboot",
            {"build": gradle_task, "output": path("build/libs/*.jar")},
        )
    if (dockerfile := f("Dockerfile")) is not None:
        workdir = {"dockerfile": path("Dockerfile"), "context": module}
        if _JAR_IN_DOCKERFILE.search(dockerfile) and (pom or gradle):
            workdir = {"build": maven_build if pom else gradle_task, **workdir}
        return Skeleton(_slug(name), module, "dockerfile", workdir)
    return None


def skeletons(inspection: dict[str, Any]) -> list[Skeleton]:
    """One skeleton per deployable module: a Spring Boot build or its own Dockerfile. With
    modules, the root is an aggregator and is skipped."""
    modules = inspection["modules"] or ["."]
    found = [_skeleton(m, inspection["files"]) for m in modules]
    return [s for s in found if s is not None]


def pick(found: list[Skeleton], service: str) -> Skeleton:
    if not found:
        raise DraftError(
            f"{service}: no Spring Boot build or Dockerfile found; add a Dockerfile to the project"
        )
    if len(found) == 1:
        return found[0]
    names = [s.name for s in found]
    close = difflib.get_close_matches(service, names, n=1, cutoff=0.8)
    if not close:
        raise DraftError(f"no deployable module matches {service}: {', '.join(names)}")
    return found[names.index(close[0])]


def env_placeholders(text: str) -> tuple[list[str], list[str]]:
    """Environment variables a config references: (without a default, with a default)."""
    found = _PLACEHOLDER.findall(text)
    required = sorted({n for n, default in found if not default})
    return required, sorted({n for n, default in found if default} - set(required))


class _Port(BaseModel):
    name: str
    container: int


class _Requirement(BaseModel):
    capability: str
    version: str | None = None
    bind: dict[str, str] = {}


class DraftAnswer(BaseModel):
    """What the model fills in; the skeleton supplies everything else. It is flatter than a
    descriptor: a JSON schema cannot say "exactly one of http or cmd", and a local model
    filled both."""

    ports: list[_Port] = []
    env_required: list[str] = []
    env_defaults: dict[str, str] = {}
    health_path: str | None = None  # an HTTP path that answers 200 when healthy
    health_cmd: list[str] | None = None  # a compose healthcheck test, when there is no path
    requires: list[_Requirement] = []
    probe_paths: list[str] = []  # HTTP paths that must answer 200 once the cluster is up

    def bound(self) -> set[str]:
        return {var for req in self.requires for var in req.bind}

    def without_bound(self) -> "DraftAnswer":
        """A bound variable needs no default or required entry: the bind sets it. Models
        copied the config's own default (often localhost) next to the bind."""
        bound = self.bound()
        return self.model_copy(
            update={
                "env_required": [v for v in self.env_required if v not in bound],
                "env_defaults": {k: v for k, v in self.env_defaults.items() if k not in bound},
            }
        )

    def runtime(self) -> Runtime:
        if self.health_path:
            health = Healthcheck(http=self.health_path)
        elif self.health_cmd:
            health = Healthcheck(cmd=self.health_cmd)
        else:
            health = None
        return Runtime(
            ports=[Port(name=p.name, container=p.container) for p in self.ports],
            env=Env(required=self.env_required, defaults=self.env_defaults),
            healthcheck=health,
        )


DRAFT_SYSTEM = (
    "You describe how one service runs in a docker compose test cluster. From its build file "
    "and configuration, give its container ports, its environment, an HTTP health path, the "
    "middleware capabilities it requires, and HTTP smoke probe paths.\n"
    "requires[].bind maps an environment variable the service reads to a template over the "
    "capability's values. For a service whose configuration reads ${DATABASE_URL}, "
    "${DATABASE_PASSWORD}, ${BROKERS} and ${CACHE_HOST}, requires is:\n"
    '[{"capability": "mysql", "bind": {"DATABASE_URL": "jdbc:mysql://{{host}}:{{port}}/{{db}}", '
    '"DATABASE_PASSWORD": "{{password}}"}}, {"capability": "kafka", "bind": {"BROKERS": '
    '"{{host}}:{{port}}"}}, {"capability": "redis", "bind": {"CACHE_HOST": "{{host}}"}}]\n'
    "Bind every placeholder that holds a connection setting (URL, host, port, user, password); "
    "list other placeholders without a default in env_required. Set version only when the "
    "build file pins one, as a specifier such as >=8.0. Never default a setting to localhost: "
    "inside a container that is the service itself. A Spring Boot service with actuator "
    "answers /actuator/health. Answer only with JSON matching the schema."
)


def _capabilities(catalog: Catalog) -> dict[str, set[str]]:
    """capability -> the values every template providing it offers a bind template."""
    caps: dict[str, set[str]] = {}
    for t in catalog.middleware.values():
        values = {"host", "port", *t.connection}
        cap = t.provides.capability
        caps[cap] = caps[cap] & values if cap in caps else values
    return caps


def _module_files(sk: Skeleton, files: dict[str, str]) -> dict[str, str]:
    if sk.module == ".":
        return {p: c for p, c in files.items() if not p.startswith(".cagent/")}
    return {p: c for p, c in files.items() if p.startswith(f"{sk.module}/")}


def draft_prompt(sk: Skeleton, files: dict[str, str], catalog: Catalog) -> str:
    texts = _module_files(sk, files)
    required, optional = env_placeholders("\n".join(texts.values()))
    caps = [
        f"- {cap}: "
        + ", ".join("{{" + v + "}}" for v in ["host", "port", *sorted(values - {"host", "port"})])
        for cap, values in sorted(_capabilities(catalog).items())
    ]
    return "\n".join(
        [
            f"Service `{sk.name}`, stack {sk.stack}, module `{sk.module}`.",
            "Files:",
            *(f"--- {p}\n{c}" for p, c in sorted(texts.items())),
            f"Environment placeholders without a default (set each one): "
            f"{', '.join(required) or 'none'}",
            f"Environment placeholders with a default (optional): {', '.join(optional) or 'none'}",
            "Capabilities you may require, and the values their bind templates may use:",
            *caps,
        ]
    )


def _problems(answer: DraftAnswer, required: list[str], catalog: Catalog) -> list[str]:
    caps = _capabilities(catalog)
    problems = []
    for req in answer.requires:
        if req.capability not in caps:
            problems.append(f"no template provides {req.capability}; use {', '.join(sorted(caps))}")
            continue
        for var, template in req.bind.items():
            if not _ENV_NAME.fullmatch(var):
                problems.append(
                    "bind keys are environment variables the service reads, "
                    f"such as DB_URL; {var} is not"
                )
            elif unknown := placeholders(template) - caps[req.capability]:
                names = ", ".join("{{" + u + "}}" for u in sorted(unknown))
                problems.append(f"{req.capability} bind {var} uses unknown {names}")
    for var, value in answer.env_defaults.items():
        if _LOCAL.search(value):
            problems.append(
                f"{var}={value} points at the container itself; in a cluster, bind {var} "
                "to a capability instead"
            )
    covered = set(answer.env_required) | set(answer.env_defaults) | answer.bound()
    if missing := [v for v in required if v not in covered]:
        problems.append(
            f"placeholders {', '.join(missing)} are set nowhere: "
            "add each to env_required or to a requires bind"
        )
    return problems


def _render(sk: Skeleton, service: str, descriptor: ServiceDescriptor) -> str:
    body = yaml.safe_dump(
        descriptor.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    return (
        f"# Drafted by cagent for {service} from module {sk.module}. Review it, then save it\n"
        f"# as .cagent/services/{service}.yaml in the project root.\n{body}"
    )


async def draft(
    router: Router, service: str, inspection: dict[str, Any], catalog: Catalog
) -> Draft:
    """Draft service's descriptor on the top tier. An invalid answer is asked again once,
    with the reasons; a second invalid answer raises ModelUnavailable."""
    sk = pick(skeletons(inspection), service)
    files = _module_files(sk, inspection["files"])
    required, _ = env_placeholders("\n".join(files.values()))
    prompt = draft_prompt(sk, inspection["files"], catalog)
    errors: list[str] = []
    for _ in range(2):
        feedback = f"\nYour previous answer was invalid: {errors[-1]}" if errors else ""
        answer, _ = await router.ask(
            DRAFT_SYSTEM, prompt + feedback, DraftAnswer, start=router.config.order[-1], think=False
        )
        answer = answer.without_bound()
        problems = _problems(answer, required, catalog)
        if not problems:
            try:
                descriptor = ServiceDescriptor(
                    name=service,
                    kind="app",
                    stack=sk.stack,
                    sources={"workdir": sk.workdir},
                    runtime=answer.runtime(),
                    requires=[Requirement(**r.model_dump()) for r in answer.requires],
                    probes=[Probe(http=HttpProbe(path=p)) for p in answer.probe_paths],
                )
            except ValidationError as e:
                problems = [str(e)]
            else:
                path = f".cagent/services/{service}.yaml"
                return Draft(path, _render(sk, service, descriptor), descriptor)
        errors.append("; ".join(problems))
    raise ModelUnavailable(f"no valid draft for {service}: {' | '.join(errors)}")


def read_back(service: str, inspection: dict[str, Any]) -> ServiceDescriptor:
    """The descriptor the user saved, as the Runner will find it."""
    files = inspection["files"]
    path = f".cagent/services/{service}.yaml"
    if path not in files:
        path = ".cagent/service.yaml"
    if path not in files:
        raise DraftError(f"{service}: .cagent/services/{service}.yaml is not saved yet")
    try:
        doc = yaml.safe_load(files[path])
    except yaml.YAMLError as e:
        raise DraftError(f"{path} is not valid YAML: {e}") from e
    if isinstance(doc, dict) and doc.get("name") != service:
        raise DraftError(f"{path} names {doc.get('name')}, not {service}")
    if MASK in files[path]:
        raise DraftError(
            f"{path} holds a masked secret: a literal password or token cannot be read back; "
            "take it from a requires bind or remove it"
        )
    try:
        desc = ServiceDescriptor.model_validate(doc)
    except ValidationError as e:
        raise DraftError(f"{path}: {e}") from e
    if desc.sources.workdir is None:
        raise DraftError(f"{path} declares no workdir source")
    return desc


def resource_note(resources: list[str]) -> str:
    """What the user should know about init resources the inspection found."""
    if not resources:
        return ""
    migrations = [r for r in resources if "db/migration/" in r or "db/changelog/" in r]
    scripts = [r for r in resources if r not in migrations]
    lines = []
    if migrations:
        lines.append("Flyway or Liquibase migrations run when the application starts.")
    if scripts:
        lines.append(
            "These scripts are not run yet; M7c will run them before the service starts: "
            + ", ".join(scripts)
        )
    return " ".join(lines)
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `245 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/draft.py platform/tests/test_draft.py
git commit -m "feat(platform): draft descriptors with a deterministic skeleton"
```

---

### Task 12: Orchestrator: extract, ask, inspect, save, read back

`ask` and `save` are interrupts and do nothing before `interrupt()`. The thread's catalog is the shared one plus the descriptors read back from its projects, kept in the checkpoint as YAML.

**Files:**
- Modify (full files below): `platform/src/cagent/orchestrator.py`, `platform/tests/simcluster.py`
- Create: `platform/tests/test_orchestrator_clarify.py`

**Step 1: Teach SimCluster `inspect`, and write the failing tests**

Replace `platform/tests/simcluster.py`:

```python
"""A fake Runner and Docker for orchestrator tests.

It answers the Runner's tools from a tiny model of a cluster: which host ports are taken, how
much memory each service needs, which services crash, and how long healthchecks stay in
"starting". That is enough to drive every branch of the state machine deterministically.
"""

from typing import Any

import yaml

from cagent.hub import ToolResult
from cagent.rules import to_mb


class SimCluster:
    def __init__(
        self,
        *,
        occupied_ports: set[int] | None = None,
        needs_mb: dict[str, int] | None = None,
        crash: dict[str, int] | None = None,
        findings: list[dict[str, str]] | None = None,
        starting_snapshots: int = 0,
        requires_env: dict[str, dict[str, str]] | None = None,
        errors: dict[str, str] | None = None,
        projects: dict[str, dict[str, Any]] | None = None,
    ):
        self.occupied = occupied_ports or set()
        self.needs_mb = needs_mb or {}  # below this mem_limit the service is OOM-killed
        self.crash = crash or {}  # service -> exit code, a failure no T0 rule fixes
        self.findings = findings or []  # lint findings until the user allows them
        self.starting_snapshots = starting_snapshots
        # service -> environment it needs; until it has it, it exits 1 logging errors[service]
        self.requires_env = requires_env or {}
        self.errors = errors or {}
        # workdir -> what inspect reports; tests add a saved descriptor to its "files"
        self.projects = projects or {}
        self.compose: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.builds: list[dict[str, Any]] = []

    async def call(self, runner_id: str, method: str, params: dict[str, Any], timeout=None):
        self.calls.append(method)
        return getattr(self, f"_{method}")(params)

    @staticmethod
    def _host_ports(svc: dict[str, Any]) -> list[int]:
        return [int(str(p).split(":")[0]) for p in svc.get("ports") or [] if ":" in str(p)]

    def _validate(self, p: dict[str, Any]) -> ToolResult:
        doc = yaml.safe_load(p["compose"])
        conflicts = [
            {"service": name, "port": port, "reason": "in-use"}
            for name, svc in doc["services"].items()
            for port in self._host_ports(svc)
            if port in self.occupied
        ]
        allowed = set(p.get("allow") or [])
        findings = [f for f in self.findings if f"{f['service']}:{f['rule']}" not in allowed]
        out = {"project": p["name"], "findings": findings, "port_conflicts": conflicts}
        return ToolResult(1 if findings or conflicts else 0, out)

    def _inspect(self, p: dict[str, Any]) -> ToolResult:
        if p["workdir"] not in self.projects:
            return ToolResult(2, None, f"{p['workdir']} is not a directory")
        return ToolResult(0, self.projects[p["workdir"]])

    def _build(self, p: dict[str, Any]) -> ToolResult:
        self.builds.append(p)
        return ToolResult(0, {"image": p["tag"], "cached": False})

    def _up(self, p: dict[str, Any]) -> ToolResult:
        self.compose = yaml.safe_load(p["compose"])
        self._starting = self.starting_snapshots
        return ToolResult(0, {"project": f"cagent-{p['name']}"})

    def _service(self, name: str, svc: dict[str, Any]) -> dict[str, Any]:
        s = {
            "service": name,
            "state": "running",
            "exit_code": 0,
            "oom_killed": False,
            "restart_count": 0,
            "error_lines": [],
            "tail": [],
            "signatures": [],
        }
        need, limit = self.needs_mb.get(name), to_mb(svc.get("mem_limit", "0"))
        if need and (limit is None or limit < need):
            s.update(
                state="exited", exit_code=137, oom_killed=True, signatures=[f"oom:killed@{name}"]
            )
        elif name in self.crash:
            code = self.crash[name]
            s.update(state="exited", exit_code=code, signatures=[f"exit:{code}@{name}"])
        elif (want := self.requires_env.get(name)) and any(
            str((svc.get("environment") or {}).get(k)) != v for k, v in want.items()
        ):
            line = self.errors.get(name, f"FATAL: {name} is misconfigured")
            s.update(
                state="exited",
                exit_code=1,
                error_lines=[line],
                tail=[line],
                signatures=[f"exit:1@{name}"],
            )
        elif "healthcheck" in svc:
            s["health"] = "starting" if self._starting > 0 else "healthy"
        return s

    def _snapshot(self, p: dict[str, Any]) -> ToolResult:
        services = [self._service(n, s) for n, s in sorted(self.compose["services"].items())]
        self._starting = max(0, self._starting - 1)
        return ToolResult(0, {"project": f"cagent-{p['name']}", "services": services})

    def _verify(self, p: dict[str, Any]) -> ToolResult:
        services = [self._service(n, s) for n, s in sorted(self.compose["services"].items())]
        ok = all(s["state"] == "running" and s.get("health") in (None, "healthy") for s in services)
        reached = ("L3" if p.get("probes") else "L2") if ok else ""
        return ToolResult(0 if ok else 1, {"passed": ok, "reached": reached, "results": []})
```

Create `platform/tests/test_orchestrator_clarify.py`:

```python
"""Clarify and project inspection in the state machine, against SimCluster and a scripted model."""

import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.llm import Router
from cagent.orchestrator import Orchestrator, Settings

from .conftest import spec_with
from .fakellm import CONFIG, FakeCompleter
from .simcluster import SimCluster
from .test_draft import SHOP, answer

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
PURPOSE = "Run order-service 1.4.2 against MySQL 5.7."


async def no_sleep(_seconds: float) -> None:
    return None


def orchestrator(sim, catalog, script=None, saver=None, settings=FAST) -> Orchestrator:
    router = Router(CONFIG, FakeCompleter(script)) if script is not None else None
    return Orchestrator(
        sim, catalog, saver or InMemorySaver(), settings, sleep=no_sleep, router=router
    )


def extraction(services, middleware=()):
    return {"name": "orders", "services": services, "middleware": list(middleware)}


ORDERS = {"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}


async def test_a_purpose_becomes_a_spec_one_question_at_a_time(catalog):
    sim = SimCluster()
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    o = orchestrator(sim, catalog, script)
    status = await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    q = status.waiting
    assert (q["kind"], q["gap"]["kind"], q["options"], q["default"]) == (
        "question",
        "unsupported_version",
        ["8.0", "8.4"],
        "8.4",
    )
    assert q["question"].endswith("Which version should mysql use?")
    status = await o.resume("t1", {"answer": None})  # take the default
    assert status.waiting["gap"]["kind"] == "no_provider"
    status = await o.resume("t1", {"answer": "kafka"})
    assert status.waiting["gap"] == {
        "kind": "missing_env",
        "target": "order-service",
        "message": "order-service: missing required env SPRING_PROFILES_ACTIVE",
        "options": [],
        "default": None,
        "key": "SPRING_PROFILES_ACTIVE",
    }
    status = await o.resume("t1", {"answer": " "})
    assert status.waiting["kind"] == "question" and status.waiting["error"] == (
        "an answer is required"
    )
    status = await o.resume("t1", {"answer": "dev"})
    assert status.waiting["kind"] == "approval"
    assert sim.calls == ["validate"]  # nothing ran on the user's machine before approval
    assert (await o.resume("t1", {"approve": True})).outcome == "healthy"
    assert sim.builds[0]["maven"] == "com.acme:order-service:1.4.2:jar"


async def test_aborting_a_question_ends_the_run(catalog):
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    o = orchestrator(SimCluster(), catalog, script)
    await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    assert (await o.resume("t1", {"action": "abort"})).outcome == "rejected"


async def test_too_many_questions_escalate(catalog):
    script = {"fake/large": [extraction([ORDERS], [{"template": "mysql", "version": "5.7"}])]}
    settings = Settings(poll_interval_s=0, stability_window_s=0, max_questions=1)
    o = orchestrator(SimCluster(), catalog, script, settings=settings)
    await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    status = await o.resume("t1", {"answer": None})
    assert status.waiting["kind"] == "escalation" and status.waiting["reason"] == "clarify"
    assert (
        "order-service/kafka: no middleware in the spec provides kafka"
        in (status.waiting["problems"])
    )


async def test_a_purpose_needs_a_model(catalog):
    with pytest.raises(ValueError, match="needs a model router"):
        await orchestrator(SimCluster(), catalog).start_from_purpose("t1", PURPOSE, "laptop-1")


async def test_extraction_failure_escalates(catalog):
    o = orchestrator(SimCluster(), catalog, {"fake/large": [ConnectionError("down")]})
    status = await o.start_from_purpose("t1", PURPOSE, "laptop-1")
    assert status.waiting["reason"] == "model_unavailable"


API = {"name": "orders-api", "source": {"kind": "workdir", "ref": "/src/shop"}}
SPEC = spec_with(services=[API], middleware=[])


def shop():
    return copy.deepcopy(SHOP)


async def test_an_unregistered_project_is_drafted_saved_read_back_and_built(catalog):
    sim = SimCluster(projects={"/src/shop": shop()})
    saver = InMemorySaver()
    o = orchestrator(sim, catalog, {"fake/large": [answer()]}, saver)
    status = await o.start("t1", SPEC, "laptop-1")
    w = status.waiting
    assert (w["kind"], w["service"], w["workdir"]) == (
        "save_descriptors",
        "orders-api",
        "/src/shop",
    )
    [(path, text)] = w["files"].items()
    assert path == ".cagent/services/orders-api.yaml" and "mvn -q -pl api -am package" in text
    assert "cql/keyspace.cql" in w["note"] and w["error"] is None

    status = await o.resume("t1", {"saved": True})  # said so, but did not save
    assert status.waiting["kind"] == "save_descriptors"
    assert "not saved yet" in status.waiting["error"]

    sim.projects["/src/shop"]["files"][path] = text  # the user saves the draft
    status = await o.resume("t1", {"saved": True})
    # The project's requirements now drive the questions: mysql and kafka are missing.
    assert [status.waiting["kind"], status.waiting["gap"]["kind"]] == ["question", "no_provider"]
    status = await o.resume("t1", {"answer": None})
    status = await o.resume("t1", {"answer": None})
    assert status.waiting["kind"] == "approval"

    # A restarted platform still knows the saved descriptor: it lives in the checkpoint.
    o2 = orchestrator(sim, catalog, saver=saver)
    assert (await o2.resume("t1", {"approve": True})).outcome == "healthy"
    assert sim.builds == [
        {
            "name": "orders-poc",
            "service": "orders-api",
            "stack": "java-springboot",
            "tag": status.compose["services"]["orders-api"]["image"],
            "workdir": "/src/shop",
        }
    ]
    env = status.compose["services"]["orders-api"]["environment"]
    assert env["DB_PASSWORD"] == "app" and env["KAFKA"] == "kafka:9092"


async def test_a_descriptor_already_on_disk_needs_no_model(catalog):
    project = shop()
    project["files"][".cagent/services/orders-api.yaml"] = (
        "name: orders-api\nkind: app\nstack: dockerfile\n"
        "sources:\n  workdir: { dockerfile: api/Dockerfile }\nruntime: {}\n"
    )
    sim = SimCluster(projects={"/src/shop": project})
    status = await orchestrator(sim, catalog).start("t1", SPEC, "laptop-1")
    assert status.waiting["kind"] == "approval"
    # Same checkpointer is not needed: the run continues in this orchestrator.
    o = orchestrator(sim, catalog)
    await o.start("t2", SPEC, "laptop-1")
    assert (await o.resume("t2", {"approve": True})).outcome == "healthy"
    assert sim.builds[-1]["stack"] == "dockerfile"


@pytest.mark.parametrize(
    "project, script, reason, detail",
    [
        (shop(), None, "clarify", "no model to draft"),
        (
            {**SHOP, "modules": [], "files": {"package.json": "{}"}},
            {},
            "clarify",
            "add a Dockerfile",
        ),
        (shop(), {"fake/large": [ConnectionError("down")]}, "model_unavailable", "down"),
        (None, {}, "runner_error", "not a directory"),
    ],
)
async def test_inspection_failures_escalate(catalog, project, script, reason, detail):
    sim = SimCluster(projects={"/src/shop": project} if project else {})
    status = await orchestrator(sim, catalog, script).start("t1", SPEC, "laptop-1")
    assert status.waiting["kind"] == "escalation" and status.waiting["reason"] == reason
    assert detail in str(status.waiting)
```

**Step 2: Run them to verify they fail**

```bash
cd platform && uv run pytest -q tests/test_orchestrator_clarify.py
```

Expected: `11 failed`.

**Step 3: Implement**

Replace `platform/src/cagent/orchestrator.py`:

```python
"""Deterministic orchestrator: the design's state machine (section 4) as a LangGraph graph.

    extract ─→ generate ──gap──→ ask ⏸ ──answer──→ generate
                  │ └──unregistered workdir──→ inspect → save ⏸ → readback → generate
                  ▼
    generate → validate → approve ⏸ → build → up → watch → stabilize → done
                  ↑                              │        │
                  └────────── diagnose ←─────────┴────────┘
                     (T0 rules, then model tiers)  │ undeclared env key / JVM agent
                                 │                 ▼
                                 │          approve_patch ⏸
                                 │ no rule / no fix / budget / oscillation
                                 ▼
                             escalate ⏸

Only `ask`, `save`, `approve`, `approve_patch`, and `escalate` wait for a person, via
LangGraph interrupts;
they do nothing before interrupting, because a node that called interrupt() runs again from
the top when resumed. All cluster work goes through the Runner's tools, so the orchestrator
never touches Docker.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypedDict

import yaml
from jsonpatch import JsonPatchException
from jsonpointer import JsonPointerException
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from cagent.catalog import Catalog, ServiceDescriptor
from cagent.clarify import AnswerError, apply_answer, extract, question
from cagent.diagnose import declared_env, propose
from cagent.draft import DraftError, draft, read_back, resource_note
from cagent.gaps import ANSWERABLE, Gap
from cagent.generate import GenerationError, generate, to_yaml
from cagent.hub import ToolResult
from cagent.llm import ModelUnavailable, Router
from cagent.patches import apply, fingerprint, within_environment
from cagent.rules import (
    Problem,
    first_fix,
    problems_from_snapshot,
    problems_from_validate,
    upstream_first,
)
from cagent.spec import ClusterSpec


class ToolCaller(Protocol):
    """What the orchestrator needs from the hub: RunnerHub, or a fake in tests."""

    async def call(
        self, runner_id: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> ToolResult: ...


@dataclass(frozen=True)
class Settings:
    """Budgets and timings. Defaults follow design section 5.5."""

    max_attempts_per_signature: int = 3
    max_rounds: int = 10
    deadline_s: float = 1800
    startup_timeout_s: float = 300
    poll_interval_s: float = 3
    stability_window_s: float = 120
    max_questions: int = 20


class State(TypedDict, total=False):
    cluster: str
    runner: str
    purpose: str | None  # set when the run starts from a sentence instead of a spec
    spec: dict[str, Any] | None
    question: dict[str, Any] | None  # the gap being asked about, and the last answer's error
    asked: int
    drafting: dict[str, Any] | None  # a descriptor draft waiting to be saved
    descriptors: dict[str, str]  # service -> descriptor read back from the user's project
    compose: dict[str, Any] | None
    allow: list[str]
    approved: bool
    built: bool
    findings: list[dict[str, Any]]
    problems: list[dict[str, Any]]
    snapshot: dict[str, Any]  # evidence for the model tiers
    proposal: dict[str, Any] | None  # a model fix waiting for approval
    rejected: list[dict[str, Any]]  # model fixes that could not be used
    unchecked: dict[str, Any] | None  # a model fix applied but not yet validated
    restarts: dict[str, int]
    attempts: dict[str, int]
    rounds: int
    seen: list[str]
    fixes: list[dict[str, Any]]
    report: dict[str, Any] | None
    escalation: dict[str, Any] | None
    deadline: float
    outcome: str | None


@dataclass(frozen=True)
class Status:
    """Where one cluster run stands."""

    outcome: str | None  # healthy, rejected, escalated; None while running or waiting
    waiting: dict[str, Any] | None  # the approval or escalation a person must answer
    compose: dict[str, Any] | None
    fixes: list[dict[str, Any]]


_BIND = re.compile(r"Bind for [0-9.:\[\]]*?:(\d+) failed|0\.0\.0\.0:(\d+).*address already in use")


class Orchestrator:
    def __init__(
        self,
        hub: ToolCaller,
        catalog: Catalog,
        checkpointer: Any,
        settings: Settings | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        router: Router | None = None,
    ):
        """Without a router the orchestrator stays deterministic: T0 rules only."""
        self.hub, self.catalog, self.settings = hub, catalog, settings or Settings()
        self.sleep, self.clock, self.router = sleep, clock, router
        self.graph = self._graph().compile(checkpointer=checkpointer)

    # --- public API -------------------------------------------------------------------------

    async def start(self, thread_id: str, spec: ClusterSpec, runner: str) -> Status:
        await self.graph.ainvoke(self._initial(runner, spec=spec), self._config(thread_id))
        return await self.status(thread_id)

    async def start_from_purpose(self, thread_id: str, purpose: str, runner: str) -> Status:
        """Start from a sentence: a model drafts the spec, then questions close its gaps."""
        if self.router is None:
            raise ValueError("starting from a purpose needs a model router")
        await self.graph.ainvoke(self._initial(runner, purpose=purpose), self._config(thread_id))
        return await self.status(thread_id)

    def _initial(
        self, runner: str, spec: ClusterSpec | None = None, purpose: str | None = None
    ) -> State:
        return {
            "cluster": spec.name if spec else "",
            "runner": runner,
            "purpose": purpose,
            "spec": spec.model_dump() if spec else None,
            "question": None,
            "asked": 0,
            "drafting": None,
            "descriptors": {},
            "compose": None,
            "allow": [],
            "approved": False,
            "built": False,
            "findings": [],
            "problems": [],
            "snapshot": {"services": []},
            "proposal": None,
            "rejected": [],
            "unchecked": None,
            "restarts": {},
            "attempts": {},
            "rounds": 0,
            "seen": [],
            "fixes": [],
            "report": None,
            "escalation": None,
            "outcome": None,
            "deadline": self.clock() + self.settings.deadline_s,
        }

    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Status:
        """Answer what is pending: a question ({"answer": value | None}), a save
        ({"saved": True}), an approval ({"approve": bool, "allow": [...]}), or an escalation
        ({"action": "retry"}). {"action": "abort"} ends a question, save, or escalation."""
        await self.graph.ainvoke(Command(resume=decision), self._config(thread_id))
        return await self.status(thread_id)

    async def status(self, thread_id: str) -> Status:
        snap = await self.graph.aget_state(self._config(thread_id))
        values = snap.values
        waiting = snap.interrupts[0].value if snap.interrupts else None
        return Status(
            values.get("outcome"), waiting, values.get("compose"), values.get("fixes", [])
        )

    @staticmethod
    def _config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    # --- graph ------------------------------------------------------------------------------

    def _graph(self) -> StateGraph:
        g = StateGraph(State)
        for name in (
            "extract",
            "ask",
            "inspect",
            "save",
            "readback",
            "generate",
            "validate",
            "approve",
            "build",
            "up",
            "watch",
            "stabilize",
            "diagnose",
            "approve_patch",
            "escalate",
            "done",
        ):
            g.add_node(name, getattr(self, f"_step_{name}"))
        g.add_conditional_edges(
            START, lambda s: "generate" if s["spec"] else "extract", ["generate", "extract"]
        )
        g.add_edge("done", END)
        return g

    async def _call(self, state: State, method: str, **params: Any) -> ToolResult:
        return await self.hub.call(state["runner"], method, {"name": state["cluster"], **params})

    @staticmethod
    def _escalation(kind: str, **detail: Any) -> Command:
        return Command(update={"escalation": {"reason": kind, **detail}}, goto="escalate")

    def _catalog(self, state: State) -> Catalog:
        """The shared catalog plus the descriptors read back from this run's projects."""
        saved = state.get("descriptors") or {}
        if not saved:
            return self.catalog
        services = {
            name: ServiceDescriptor.model_validate(yaml.safe_load(text))
            for name, text in saved.items()
        }
        return Catalog({**self.catalog.services, **services}, self.catalog.middleware)

    async def _step_extract(self, state: State) -> Command:
        try:
            spec = await extract(self.router, state["purpose"], self.catalog)
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", step="extract", detail=str(e))
        return Command(update={"spec": spec.model_dump(), "cluster": spec.name}, goto="generate")

    async def _step_generate(self, state: State) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        try:
            compose = generate(spec, self._catalog(state))
        except GenerationError as e:
            return self._close_gap(state, spec, e)
        return Command(update={"compose": compose, "seen": [fingerprint(compose)]}, goto="validate")

    def _close_gap(self, state: State, spec: ClusterSpec, e: GenerationError) -> Command:
        """Ask about the first gap, inspect an unregistered project, or escalate."""
        gap = e.gaps[0]
        if gap.kind == "unregistered":
            ref = next(s for s in spec.services if s.name == gap.target)
            if ref.source.kind == "workdir":
                drafting = {"service": ref.name, "workdir": ref.source.ref, "error": None}
                return Command(update={"drafting": drafting}, goto="inspect")
        answerable = gap.kind in ANSWERABLE and (gap.options or gap.kind == "missing_env")
        if answerable and state["asked"] < self.settings.max_questions:
            return Command(update={"question": {"gap": gap.to_dict(), "error": None}}, goto="ask")
        return self._escalation("clarify", problems=e.problems)

    async def _step_ask(self, state: State) -> Command:
        q = state["question"]
        gap = Gap.from_dict(q["gap"])
        decision = interrupt(
            {
                "kind": "question",
                "gap": q["gap"],
                "question": question(gap),
                "options": q["gap"]["options"],
                "default": gap.default,
                "error": q["error"],
            }
        )
        if decision.get("action") == "abort":
            return Command(update={"outcome": "rejected"}, goto=END)
        asked = state["asked"] + 1
        try:
            spec = apply_answer(
                ClusterSpec.model_validate(state["spec"]), gap, decision.get("answer")
            )
        except AnswerError as err:
            return Command(
                update={"asked": asked, "question": {**q, "error": str(err)}}, goto="ask"
            )
        return Command(
            update={"spec": spec.model_dump(), "asked": asked, "question": None}, goto="generate"
        )

    async def _inspect(self, state: State) -> ToolResult:
        workdir = state["drafting"]["workdir"]
        return await self.hub.call(state["runner"], "inspect", {"workdir": workdir})

    async def _step_inspect(self, state: State) -> Command:
        """Use a descriptor the project already has; otherwise draft one for the user."""
        d = state["drafting"]
        res = await self._inspect(state)
        if not res.ok or not isinstance(res.output, dict):
            return self._escalation("runner_error", step="inspect", stderr=res.stderr)
        try:
            read_back(d["service"], res.output)
        except DraftError:
            pass
        else:
            return self._saved(state, res.output)
        if self.router is None:
            return self._escalation(
                "clarify",
                problems=[f"{d['service']}: not registered, and no model to draft a descriptor"],
            )
        try:
            drafted = await draft(self.router, d["service"], res.output, self.catalog)
        except DraftError as e:
            return self._escalation("clarify", problems=[str(e)])
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", step="draft", detail=str(e))
        return Command(
            update={
                "drafting": {
                    **d,
                    "files": {drafted.path: drafted.text},
                    "note": resource_note(res.output["resources"]),
                }
            },
            goto="save",
        )

    async def _step_save(self, state: State) -> Command:
        d = state["drafting"]
        decision = interrupt(
            {
                "kind": "save_descriptors",
                "service": d["service"],
                "workdir": d["workdir"],
                "files": d["files"],
                "note": d["note"],
                "error": d["error"],
            }
        )
        if decision.get("action") == "abort":
            return Command(update={"outcome": "rejected"}, goto=END)
        return Command(goto="readback")

    async def _step_readback(self, state: State) -> Command:
        """Validate the descriptor the user saved, as the Runner will read it."""
        res = await self._inspect(state)
        if not res.ok or not isinstance(res.output, dict):
            return self._escalation("runner_error", step="inspect", stderr=res.stderr)
        try:
            read_back(state["drafting"]["service"], res.output)
        except DraftError as e:
            return Command(update={"drafting": {**state["drafting"], "error": str(e)}}, goto="save")
        return self._saved(state, res.output)

    @staticmethod
    def _saved(state: State, inspection: dict[str, Any]) -> Command:
        service = state["drafting"]["service"]
        files = inspection["files"]
        path = f".cagent/services/{service}.yaml"
        text = files.get(path, files.get(".cagent/service.yaml"))
        return Command(
            update={"descriptors": {**state["descriptors"], service: text}, "drafting": None},
            goto="generate",
        )

    async def _step_validate(self, state: State) -> Command:
        res = await self._call(
            state, "validate", compose=to_yaml(state["compose"]), allow=state["allow"]
        )
        if res.exit_code not in (0, 1) or not isinstance(res.output, dict):
            if unchecked := state.get("unchecked"):
                # A model patch passed the gate but broke the document: undo it and let the
                # next attempt start a tier higher, instead of escalating.
                return Command(
                    update={
                        "compose": unchecked["previous"],
                        "fixes": state["fixes"][:-1],
                        "unchecked": None,
                        "rejected": [
                            *state["rejected"],
                            {**unchecked["fix"], "why": f"invalid compose: {res.stderr[-300:]}"},
                        ],
                    },
                    goto="diagnose",
                )
            return self._escalation("runner_error", step="validate", stderr=res.stderr)
        checked = {"unchecked": None}
        if problems := problems_from_validate(res.output):
            return Command(
                update={**checked, "problems": [asdict(p) for p in problems]}, goto="diagnose"
            )
        if res.output.get("findings"):
            return Command(
                update={**checked, "findings": res.output["findings"], "approved": False},
                goto="approve",
            )
        if not state["approved"]:
            return Command(update={**checked, "findings": []}, goto="approve")
        return Command(update=checked, goto="up" if state["built"] else "build")

    async def _step_approve(self, state: State) -> Command:
        decision = interrupt(
            {
                "kind": "approval",
                "compose": to_yaml(state["compose"]),
                "findings": state["findings"],
                "fixes": state["fixes"],
            }
        )
        if not decision.get("approve"):
            return Command(update={"outcome": "rejected"}, goto=END)
        return Command(
            update={"approved": True, "allow": decision.get("allow", state["allow"])},
            goto="validate",
        )

    async def _step_build(self, state: State) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        catalog = self._catalog(state)
        for ref in spec.services:
            desc = catalog.services[ref.name]
            params: dict[str, Any] = {
                "service": ref.name,
                "stack": desc.stack,
                "tag": state["compose"]["services"][ref.name]["image"],
            }
            kind = ref.source.kind
            if kind in ("release", "snapshot"):
                pattern = getattr(desc.sources, kind).nexus
                params["maven"] = pattern.replace(
                    "{version}", ref.source.ref.removesuffix("-SNAPSHOT")
                )
            elif kind == "workdir":
                params["workdir"] = ref.source.ref
            else:
                return self._escalation("unsupported_source", service=ref.name, source=kind)
            res = await self._call(state, "build", **params)
            if not res.ok:
                return self._escalation(
                    "build_failed", service=ref.name, result=res.output, stderr=res.stderr
                )
        return Command(update={"built": True}, goto="up")

    async def _step_up(self, state: State) -> Command:
        res = await self._call(state, "up", compose=to_yaml(state["compose"]), allow=state["allow"])
        if res.ok:
            return Command(goto="watch")
        if problems := self._bind_problems(state["compose"], res.stderr):
            return Command(update={"problems": [asdict(p) for p in problems]}, goto="diagnose")
        return self._escalation("up_failed", stderr=res.stderr)

    @staticmethod
    def _bind_problems(compose: dict[str, Any], stderr: str) -> list[Problem]:
        """Map docker's 'port is already allocated' errors to the services publishing it."""
        ports = {int(a or b) for a, b in _BIND.findall(stderr)}
        return [
            Problem("bind:address_in_use", name)
            for name, svc in compose["services"].items()
            if any(
                ":" in str(p) and int(str(p).split(":")[0]) in ports for p in svc.get("ports") or []
            )
        ]

    def _probes(self, state: State) -> list[dict[str, Any]]:
        """L3 probes from each app service's descriptor."""
        probes = []
        catalog = self._catalog(state)
        for ref in ClusterSpec.model_validate(state["spec"]).services:
            desc = catalog.services[ref.name]
            ports = {p.name: p.container for p in desc.runtime.ports}
            for pr in desc.probes:
                port = ports[pr.http.port] if pr.http.port else next(iter(ports.values()))
                probes.append(
                    {
                        "service": ref.name,
                        "http": {"port": port, "path": pr.http.path, "expect": pr.http.expect},
                    }
                )
        return probes

    async def _snapshot(self, state: State) -> dict[str, Any]:
        return (await self._call(state, "snapshot")).output or {"services": []}

    async def _step_watch(self, state: State) -> Command:
        """Poll until every service runs and is healthy, then verify; diagnose on failure."""
        deadline = self.clock() + self.settings.startup_timeout_s
        while True:
            snap = await self._snapshot(state)
            if problems := problems_from_snapshot(snap):
                return Command(
                    update={"problems": [asdict(p) for p in problems], "snapshot": snap},
                    goto="diagnose",
                )
            services = snap.get("services", [])
            settled = services and all(
                s["state"] == "running" and s.get("health") != "starting" for s in services
            )
            if settled:
                rep = (await self._call(state, "verify", probes=self._probes(state))).output
                if rep and rep.get("passed"):
                    restarts = {s["service"]: s.get("restart_count", 0) for s in services}
                    return Command(update={"report": rep, "restarts": restarts}, goto="stabilize")
                if self.clock() > deadline:
                    return self._escalation("verify_failed", report=rep)
            elif self.clock() > deadline:
                return self._escalation("startup_timeout", snapshot=snap)
            await self.sleep(self.settings.poll_interval_s)

    async def _step_stabilize(self, state: State) -> Command:
        """Healthy must stay healthy: no restarts and still passing after the window."""
        await self.sleep(self.settings.stability_window_s)
        snap = await self._snapshot(state)
        problems = problems_from_snapshot(snap)
        for s in snap.get("services", []):
            if s.get("restart_count", 0) != state["restarts"].get(s["service"], 0):
                problems.append(Problem("restart:unstable", s["service"]))
        if problems:
            return Command(
                update={"problems": [asdict(p) for p in problems], "snapshot": snap},
                goto="diagnose",
            )
        rep = (await self._call(state, "verify", probes=self._probes(state))).output
        if not (rep and rep.get("passed")):
            return self._escalation("verify_failed", report=rep)
        return Command(update={"report": rep}, goto="done")

    async def _step_diagnose(self, state: State) -> Command:
        """Apply the first T0 fix the budgets allow; for problems no rule covers, ask the
        model tiers when a router is configured; escalate when neither helps."""
        compose = state["compose"]
        problems = upstream_first([Problem(**p) for p in state["problems"]], compose)
        if state["rounds"] >= self.settings.max_rounds or self.clock() > state["deadline"]:
            return self._escalation("budget", problems=[p.signature for p in problems])
        exhausted, unruled = [], []
        for p in problems:
            if state["attempts"].get(p.signature, 0) >= self.settings.max_attempts_per_signature:
                exhausted.append(p.signature)
                continue
            fix = first_fix(p, compose)
            if fix is None or not within_environment(fix.patch):
                unruled.append(p)
                continue
            return self._apply_fix(state, p.signature, asdict(fix), fix.patch)
        if unruled and self.router is not None:
            return await self._ask_model(state, unruled[0])
        reason = "budget" if exhausted else "no_rule"
        return self._escalation(reason, problems=[p.signature for p in problems])

    def _apply_fix(
        self, state: State, signature: str, fix: dict[str, Any], patch: list[dict[str, Any]]
    ) -> Command:
        try:
            patched = apply(state["compose"], patch)
        except (JsonPatchException, JsonPointerException) as e:
            return self._failed_attempt(state, signature, fix, f"patch does not apply: {e}")
        if fingerprint(patched) in state["seen"]:
            if fix["rule"].startswith("llm:"):
                # A model repeating a state already tried is stuck, not oscillating: let the
                # next attempt start a tier higher, within the per-signature budget.
                return self._failed_attempt(state, signature, fix, "repeats a state already tried")
            return self._escalation("oscillation", fix=fix)
        return Command(
            update={
                "compose": patched,
                "seen": [*state["seen"], fingerprint(patched)],
                "attempts": {
                    **state["attempts"],
                    signature: state["attempts"].get(signature, 0) + 1,
                },
                "rounds": state["rounds"] + 1,
                "fixes": [*state["fixes"], fix],
                "proposal": None,
                "unchecked": (
                    {"signature": signature, "fix": fix, "previous": state["compose"]}
                    if fix["rule"].startswith("llm:")
                    else None
                ),
            },
            goto="validate",
        )

    def _failed_attempt(
        self, state: State, signature: str, fix: dict[str, Any], why: str
    ) -> Command:
        """Count a model fix that could not be used; the next try starts a tier higher."""
        return Command(
            update={
                "attempts": {
                    **state["attempts"],
                    signature: state["attempts"].get(signature, 0) + 1,
                },
                "rounds": state["rounds"] + 1,
                "rejected": [*state["rejected"], {**fix, "why": why}],
            },
            goto="diagnose",
        )

    async def _ask_model(self, state: State, problem: Problem) -> Command:
        spec = ClusterSpec.model_validate(state["spec"])
        try:
            proposal = await propose(
                self.router,
                problem,
                state["snapshot"],
                state["compose"],
                declared_env(spec, self._catalog(state)),
                state["attempts"].get(problem.signature, 0),
            )
        except ModelUnavailable as e:
            return self._escalation("model_unavailable", problem=problem.signature, detail=str(e))
        fix = {
            "rule": f"llm:{proposal.tier}",
            "signature": proposal.signature,
            "patch": proposal.patch,
            "reason": proposal.root_cause,
        }
        if proposal.verdict == "give_up":
            return self._escalation(
                "no_fix",
                problem=problem.signature,
                root_cause=proposal.root_cause,
                tier=proposal.tier,
            )
        if proposal.verdict == "reject":
            return self._failed_attempt(
                state, problem.signature, fix, f"{proposal.tier}: {'; '.join(proposal.reasons)}"
            )
        if proposal.verdict == "approve":
            return Command(
                update={"proposal": {**asdict(proposal), "fix": fix}}, goto="approve_patch"
            )
        return self._apply_fix(state, problem.signature, fix, proposal.patch)

    async def _step_approve_patch(self, state: State) -> Command:
        p = state["proposal"]
        decision = interrupt(
            {
                "kind": "patch_approval",
                "signature": p["signature"],
                "tier": p["tier"],
                "root_cause": p["root_cause"],
                "patch": p["patch"],
                "reasons": p["reasons"],
            }
        )
        if not decision.get("approve"):
            return self._escalation(
                "patch_rejected", problem=p["signature"], root_cause=p["root_cause"]
            )
        return self._apply_fix(state, p["signature"], p["fix"], p["patch"])

    async def _step_escalate(self, state: State) -> Command:
        decision = interrupt(
            {"kind": "escalation", **(state["escalation"] or {}), "fixes": state["fixes"]}
        )
        if decision.get("action") != "retry":
            return Command(update={"outcome": "escalated"}, goto=END)
        return Command(
            update={
                "attempts": {},
                "rounds": 0,
                "escalation": None,
                "deadline": self.clock() + self.settings.deadline_s,
            },
            goto="validate" if state["compose"] else "generate",
        )

    async def _step_done(self, state: State) -> dict[str, Any]:
        return {"outcome": "healthy"}
```

**Step 4: Run all platform tests**

```bash
make platform-test
```

Expected: ruff clean, `256 passed, 4 skipped`.

**Step 5: Commit**

```bash
git add platform/src/cagent/orchestrator.py platform/tests/simcluster.py platform/tests/test_orchestrator_clarify.py
git commit -m "feat(platform): clarify and inspect projects in the orchestrator"
```

> **Checkpoint.** Tasks 10–12 done. Verify the files, `make platform-test`, and that `start(thread_id, spec, runner)` behaves as before for registered specs.

---

### Task 13: Real Runner: an unregistered project goes from inspection to healthy

The model is scripted, so CI runs this too. The test plays the user: it writes the drafted file into the project between `save` and `{"saved": true}`.

**Files:**
- Create: `platform/tests/test_inspect_e2e.py`
- Modify: `Makefile`

**Step 1: Write the test**

Create `platform/tests/test_inspect_e2e.py`:

```python
"""M7b's acceptance on real Docker: an unregistered two-module project is inspected by the real
Runner, gets a drafted descriptor, and builds and runs once the user saves it.

The model is scripted, so this runs in CI; test_llm_e2e.py asks real models. Skipped unless
CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.catalog import load_catalog
from cagent.hub import RunnerHub, start
from cagent.llm import Router
from cagent.orchestrator import Orchestrator, Settings
from cagent.spec import ClusterSpec

from .conftest import REPO_CATALOG
from .fakellm import CONFIG, FakeCompleter
from .test_hub_e2e import TOKEN, free_port

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

# The deployable module has its own Dockerfile; the library module has none.
PROJECT = {
    "pom.xml": "<project><modules><module>api</module><module>lib</module></modules></project>",
    "api/pom.xml": "<project><artifactId>api</artifactId></project>",
    "lib/pom.xml": "<project><artifactId>lib</artifactId></project>",
    "api/Dockerfile": (
        "FROM busybox:1.37\n"
        "RUN mkdir -p /www && echo ok > /www/index.html\n"
        'CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]\n'
    ),
}
RUNTIME = {
    "ports": [{"name": "http", "container": 8080}],
    # busybox has wget, not the curl an HTTP healthcheck uses
    "health_cmd": ["CMD-SHELL", "wget -q -O /dev/null http://localhost:8080/"],
    "probe_paths": ["/"],
}


async def test_unregistered_project_is_drafted_saved_built_and_run(tmp_path):
    root = tmp_path / "projects"
    project = root / "shop"
    for rel, body in PROJECT.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(body)
    name = f"m7b-e2e-{int(time.time()) % 100000}"
    spec = ClusterSpec.model_validate(
        {
            "name": name,
            "purpose": "run the shop api from its working directory",
            "services": [{"name": "api", "source": {"kind": "workdir", "ref": str(project)}}],
        }
    )
    fake = FakeCompleter({"fake/large": [RUNTIME]})
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
    tag = None
    try:
        await hub.wait_for_runner("e2e", timeout=10)
        o = Orchestrator(
            hub,
            load_catalog(REPO_CATALOG),
            InMemorySaver(),
            Settings(poll_interval_s=1, stability_window_s=5, startup_timeout_s=120),
            router=Router(CONFIG, fake),
        )
        status = await o.start("run-1", spec, "e2e")
        assert status.waiting["kind"] == "save_descriptors", status.waiting
        [(path, text)] = status.waiting["files"].items()
        assert path == ".cagent/services/api.yaml"
        assert "dockerfile: api/Dockerfile" in text

        Path(project, path).parent.mkdir(parents=True)
        Path(project, path).write_text(text)  # what the user does after reviewing the draft
        status = await o.resume("run-1", {"saved": True})
        assert status.waiting["kind"] == "approval", status.waiting

        status = await o.resume("run-1", {"approve": True})
        assert status.outcome == "healthy", status.waiting
        tag = status.compose["services"]["api"]["image"]
    finally:
        await hub.call("e2e", "down", {"name": name})
        runner.terminate()
        runner.wait(timeout=10)
        server.close()
        await server.wait_closed()
        if tag:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
```

**Step 2: Add it to `make e2e`**

In `Makefile`, replace:

```makefile
# Generated compose files must pass the real validate command, the real Runner must
# answer the hub and reconnect after a hub restart, and the orchestrator must heal a real
# cluster with T0 rules alone.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py
```

with:

```makefile
# Generated compose files must pass the real validate command, the real Runner must
# answer the hub and reconnect after a hub restart, the orchestrator must heal a real
# cluster with T0 rules alone, and an unregistered project must be inspected, drafted, and
# built.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py tests/test_inspect_e2e.py
```

**Step 3: Run the end-to-end tests with a host Runner**

`make e2e` builds a Linux binary; on this Mac, build a darwin one in the container and run the same tests.

```bash
docker run --rm -v "$PWD":/repo -w /repo -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build -e GOOS=darwin -e GOARCH=arm64 golang:1.24 make runner-build && (cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner uv run pytest -q tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py tests/test_inspect_e2e.py) && rm -rf bin && test -z "$(docker ps -aq --filter label=cagent.managed)"
```

Expected: all pass (about 40 s), nothing left running.

**Step 4: Commit**

```bash
git add platform/tests/test_inspect_e2e.py Makefile
git commit -m "test(platform): an unregistered project is inspected, drafted, built, and run"
```

---

### Task 14: Real local models: extraction and a Spring Boot draft

**Files:**
- Create: `platform/tests/test_clarify_llm_e2e.py`
- Modify: `Makefile`

**Step 1: Write the test**

Create `platform/tests/test_clarify_llm_e2e.py`:

```python
"""M7b's model calls with real local models: a purpose sentence becomes a spec that generates
after answering only the questions that need a person, and a Spring Boot module gets a runtime
that binds its placeholders to middleware.

Skipped unless CAGENT_LLM_E2E=1 (needs the models in platform/models.yaml served by a local
Ollama). CI does not run it. On failure, each test prints the exact prompt first.
"""

import os

import pytest

from cagent.clarify import apply_answer, extract, extract_prompt
from cagent.draft import draft, draft_prompt, pick, skeletons
from cagent.generate import GenerationError, generate
from cagent.llm import LiteLLMCompleter, Router, load_model_config

from .conftest import REPO
from .test_draft import SHOP

pytestmark = pytest.mark.skipif(
    os.environ.get("CAGENT_LLM_E2E") != "1",
    reason="set CAGENT_LLM_E2E=1 (needs local Ollama models)",
)

PURPOSE = (
    "I want to check that order-service 1.4.2 stores orders in MySQL 8.0 and publishes an "
    "event to Kafka for each one. Creating an order should return 201."
)


def router() -> Router:
    return Router(load_model_config(REPO / "platform" / "models.yaml"), LiteLLMCompleter())


async def test_a_purpose_sentence_becomes_a_spec(catalog):
    print(extract_prompt(PURPOSE, catalog))
    spec = await extract(router(), PURPOSE, catalog)
    print(spec.model_dump_json(indent=2))
    assert [(s.name, s.source.kind, s.source.ref) for s in spec.services] == [
        ("order-service", "release", "1.4.2")
    ]
    assert {m.template: m.version for m in spec.middleware}.get("mysql") == "8.0"
    assert spec.checks
    asked = []
    for _ in range(10):
        try:
            generate(spec, catalog)
            break
        except GenerationError as e:
            gap = e.gaps[0]
            asked.append(gap.kind)
            spec = apply_answer(spec, gap, "dev" if gap.kind == "missing_env" else None)
    generate(spec, catalog)
    # Only the value no catalog can know is asked: the Spring profile.
    assert asked.count("missing_env") == 1 and "unknown_template" not in asked, asked


async def test_a_spring_boot_module_gets_a_runtime(catalog):
    sk = pick(skeletons(SHOP), "orders-api")
    print(draft_prompt(sk, SHOP["files"], catalog))
    d = await draft(router(), "orders-api", SHOP, catalog)
    print(d.text)
    binds = {r.capability: r.bind for r in d.descriptor.requires}
    assert binds["mysql"]["DB_URL"] == "jdbc:mysql://{{host}}:{{port}}/{{db}}"
    assert binds["mysql"]["DB_PASSWORD"] == "{{password}}"
    assert binds["kafka"]["KAFKA"] == "{{host}}:{{port}}"
    assert [p.container for p in d.descriptor.runtime.ports] == [8081]
    assert d.descriptor.runtime.healthcheck.http == "/actuator/health"
```

**Step 2: Add it to `make e2e-llm`**

In `Makefile`, replace:

```makefile
# Model tiers on real Docker. Needs a local Ollama serving the models in platform/models.yaml;
# CI does not run it.
e2e-llm: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner CAGENT_LLM_E2E=1 uv run pytest -s tests/test_llm_e2e.py
```

with:

```makefile
# Model tiers on real Docker, and Clarify's model calls. Needs a local Ollama serving the
# models in platform/models.yaml; CI does not run it.
e2e-llm: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner CAGENT_LLM_E2E=1 uv run pytest -s tests/test_llm_e2e.py tests/test_clarify_llm_e2e.py
```

**Step 3: Run it three times**

Needs Ollama at `localhost:11434` serving the models in `platform/models.yaml`.

```bash
cd platform && for i in 1 2 3; do CAGENT_LLM_E2E=1 uv run pytest -q tests/test_clarify_llm_e2e.py || exit 1; done
```

Expected: `2 passed` each time, each run well under a minute. On a failure, read the printed prompt before changing anything.

**Step 4: Commit**

```bash
git add platform/tests/test_clarify_llm_e2e.py Makefile
git commit -m "test(platform): clarify with real local models"
```

---

### Task 15: Docs

**Files:**
- Modify (full file below): `docs/plans/2026-10-18-m7b-clarify-inspection-design.md`
- Modify: `docs/plans/2026-10-17-container-agent-design.md`

**Step 1: Record what changed while planning — replace the M7b design**

Write `docs/plans/2026-10-18-m7b-clarify-inspection-design.md`:

````markdown
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
| Redaction keeps references | `{{password}}` and `${DB_PASSWORD}` name a secret without holding it. Masking them broke read-back (a bind template became `******`) and hid which variable a draft must set. A reference's default (`${X:value}`) is still masked. |
| Extraction and drafting run with thinking off | Measured on `qwen3.5` (section 8): with thinking on, each call took minutes and drafting still failed; with thinking off, both took seconds and were right. Diagnosis (M7a) keeps the model's default. |
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

`Orchestrator.start(thread_id, spec, runner)` keeps working; `start_from_purpose(thread_id, purpose, runner)` enters at `extract`. Everything from `validate` on is unchanged.

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
- Contents pass through `redact`, which keeps references (`{{name}}`, `${VAR}`) and masks a reference's default.
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
| `unknown_template` | Which template should X use? | All templates; the closest name | `template` |
| `unsupported_version` | Which version should X use? | The template's versions; its default | `version` |
| `unknown_preset` | Which preset should X use? | The template's presets; its default | `preset` |
| `no_provider` | Which middleware should provide C? | Templates providing C; the first | adds a `MiddlewareRef` |
| `binding` | Which instance should X use for C? | Instances providing C; the first | `bindings` |
| `version_mismatch` | Which version should the provider use? | Versions satisfying the requirement; the highest | the provider's `version` |
| `missing_env` | What value should X get for K? | none; must be answered | `env` |
| `unknown_port` | Which port should X publish instead of P? | The descriptor's ports; the only one | `publish` |
| `no_source` | Which source should X build from? | Source kinds the descriptor declares; the first | `source.kind` |
| `unregistered` | — workdir: inspection; otherwise escalate | | |
| `bind_render` | — escalate (catalog bug) | | |

A choice gap with no options cannot be answered and escalates.

### 5.2 Question protocol

- Interrupt: `{"kind": "question", "gap": {...}, "question": str, "options": [...], "default": ..., "error": str | None}`, like the existing `approval` and `escalation` interrupts.
- Resume: `{"answer": value}`; `{"answer": None}` takes the default; `{"action": "abort"}` stops.
- `apply_answer(spec, gap, answer) -> ClusterSpec` is pure. An invalid answer is asked again with `error` set. After each answer `generate` runs again, since one answer can add or remove gaps.
- At most 20 questions per thread, then escalate `clarify`.

### 5.3 Extraction

`extract(router, purpose, catalog)` asks the top tier once, thinking off. The prompt holds the purpose and a catalog summary: services with their source kinds, templates with capability and versions. The answer schema is `{name, services: [{name, source: {kind, ref}}], middleware: [{template, version?}], checks: [...]}`. Names outside the catalog are kept so they surface as gaps; a "service" named like a middleware template (models listed MySQL as a service) becomes middleware. An empty `ref` is a schema error, so the model is asked again. When the model gives no valid answer, escalate `model_unavailable`.

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
- The model answers a flat schema (`ports`, `env_required`, `env_defaults`, `health_path` or `health_cmd`, `requires`, `probe_paths`), converted to a descriptor in code: a JSON schema cannot say "exactly one of http or cmd", and a model filled both. A health path wins over a command.
- The prompt lists the module's files, its `${PLACEHOLDER}`s with and without defaults, and each capability's bind values, and the system prompt shows one worked `requires` example.
- Checks before the user sees a draft: every capability is provided by a template; bind keys are environment variable names and use only the provider's values; every placeholder without a default is bound or listed; no default points at `localhost`. A variable that is bound is dropped from defaults and required (models copied the config's `localhost` default next to the bind).
- An invalid draft is asked again once with the reasons; a second invalid draft escalates `model_unavailable`.
- Save interrupt: `{"kind": "save_descriptors", "service", "workdir", "files": {".cagent/services/<n>.yaml": "<yaml>"}, "note": str, "error": str | None}`; resume `{"saved": true}` or `{"action": "abort"}`. The note says, for example, that Flyway migrations need nothing, and that SQL/CQL scripts are wired up by M7c.
- A project that already has a valid descriptor for the service is used as is: no model, no save.
- Read-back validates what the user actually saved, including their edits, and refuses a masked literal secret.

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

## 8. Model measurements

Measured on local Ollama, 2026-10-18, with `qwen3.5:latest` (the top tier) on a 24 GB Apple Silicon Mac. Extraction reads the purpose sentence of the live-model test; drafting reads the Spring Boot fixture `orders-api`, which needs MySQL and Kafka.

| Thinking | Prompts | Extraction | Drafting |
|---|---|---|---|
| on (model default), 2 runs | final | correct, 146–167 s | failed after 186–214 s: HTTP healthcheck but no ports |
| off | first version | wrong: MySQL listed as a service | wrong: `bind` keyed by `host`/`port` |
| off, 3 runs | final | correct, 6.6–14.4 s (first run loads the model) | correct, 6.9–8.4 s |

Thinking made each call minutes long without making drafts right. The structural mistakes it did not prevent are what the flat answer schema, the draft checks, and the worked example fix. With them, thinking off is both fast and correct, so extraction and drafting turn it off. Diagnosis keeps the model's default until M9's eval measures it.
````

**Step 2: Design section 8: redaction keeps references**

In `docs/plans/2026-10-17-container-agent-design.md`, replace:

```markdown
- **Credentials and redaction**: credentials never leave the machine. The Runner redacts diagnostic snapshots (passwords, tokens, AK/SK, passwords inside JDBC URLs) before upload, so neither the platform nor any model sees raw logs.
```

with:

```markdown
- **Credentials and redaction**: credentials never leave the machine. The Runner redacts diagnostic snapshots (passwords, tokens, AK/SK, passwords inside JDBC URLs) before upload, so neither the platform nor any model sees raw logs. References such as `{{password}}` or `${DB_PASSWORD}` are kept: they name a secret without holding it. A reference's default is masked.
```

**Step 3: Check the Mermaid diagrams render**

Or paste each block into https://mermaid.live.

```bash
cd docs/plans && for f in 2026-10-18-m7b-clarify-inspection-design.md; do python3 -c "import re,sys;[open(f'/tmp/m7b-{i}.mmd','w').write(b) for i,b in enumerate(re.findall(r'\`\`\`mermaid\n(.*?)\`\`\`', open('$f').read(), re.S))]"; done && for m in /tmp/m7b-*.mmd; do docker run --rm -v /tmp:/data minlag/mermaid-cli -i /data/$(basename $m) -o /data/$(basename $m .mmd).svg >/dev/null || exit 1; done && rm -f /tmp/m7b-*
```

Expected: no errors.

**Step 4: Run everything once more**

```bash
docker run --rm -v "$PWD":/repo -w /repo -v cagent-gomod:/go/pkg/mod -v cagent-gocache:/root/.cache/go-build golang:1.24 make runner-test && make platform-test
```

Expected: every Go package `ok`; ruff clean, `256 passed, 7 skipped`.

**Step 5: Commit**

```bash
git add docs/plans/2026-10-18-m7b-clarify-inspection-design.md docs/plans/2026-10-17-container-agent-design.md
git commit -m "docs: record M7b's planning decisions and model measurements"
```

> **Checkpoint.** All tasks done. Verify the files against this plan, rerun every check, confirm no containers or `cagent/*` test images are left, then report.

---

## Milestone 7b done when

- [ ] `make runner-test` passes in the container (fourteen packages `ok`)
- [ ] `make platform-test` passes: ruff clean, `256 passed, 7 skipped`
- [ ] Tasks 6 and 13 pass on real Docker, nothing left behind
- [ ] Task 14 passes three times in a row with local Ollama
- [ ] `runner` and `platform` workflows green on the PR (CI runs Task 13's test, skips Task 14's)

## Not in M7b

- Init resources — Kafka topics, Cassandra keyspaces and tables, SQL schema scripts — and a Cassandra template (M7c)
- Free-text answers mapped by a model, and the chat UI (M8)
- Builders that generate a Dockerfile for Node or Go; such projects need their own Dockerfile
- Topics or schemas defined only in source code: `inspect` does not read source
- A question for a missing source `ref`: the extraction schema requires one
