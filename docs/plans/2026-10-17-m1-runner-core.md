# M1 Runner Offline Core Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the Runner's offline safety core and ship it as a `cagent-runner validate` command that checks a compose file before anything starts.

**Architecture:** A Go module under `runner/`. Pure packages with no Docker daemon dependency: `guard` (path confinement), `compose` (load, lint, label, port check via compose-go), `redact` (secret masking), and `fingerprint` (stable error signatures). A thin CLI wires `compose` into `validate`. Later milestones reuse every package.

**Tech Stack:** Go 1.24+, `github.com/compose-spec/compose-go/v2` v2.16.1, standard library only otherwise. GitHub Actions for CI.

- Roadmap: [2026-10-17-poc-roadmap.md](2026-10-17-poc-roadmap.md)
- Design: [2026-10-17-container-agent-design.md](2026-10-17-container-agent-design.md) — sections 3.2, 5.1, 5.2, 8

---

## Package map

```mermaid
flowchart LR
  CLI["cmd/cagent-runner<br/>validate subcommand"] --> COMPOSE["internal/compose<br/>Load · Lint · labels · ports"]
  COMPOSE --> GUARD["internal/guard<br/>Within · CheckRead · CheckWrite"]
  COMPOSE --> CG[("compose-go v2")]
  REDACT["internal/redact<br/>String"]
  FP["internal/fingerprint<br/>FromLog · FromExit · Normalize"]
  M3["M3: signal collection"] -.->|uses later| REDACT
  M3 -.->|uses later| FP
  M3 -.->|uses later| GUARD
```

`redact` and `fingerprint` have no callers in M1. They land now because they are pure, easy to test in isolation, and M3's signal collection depends on them.

## What `validate` does

```mermaid
flowchart TD
  IN["cagent-runner validate -f compose.yaml"] --> LOAD{"compose-go load<br/>+ schema validation"}
  LOAD -->|error| E2["stderr message · exit 2"]
  LOAD -->|ok| LINT["Security lint<br/>privileged · host network · cap_add<br/>docker.sock · host mounts outside workspace"]
  LINT --> PORTS["Host-port check<br/>in use on host · duplicated in project"]
  PORTS --> REP["JSON report on stdout"]
  REP --> ANY{"Findings or<br/>conflicts?"}
  ANY -->|yes| E1["exit 1"]
  ANY -->|no| E0["exit 0"]
```

## Prerequisites

- Go 1.24 or newer on `PATH`. On macOS: `brew install go`. Check with `go version`.
- No Go on the host? Run any `go` command in this plan inside a container instead, from the repository root:
  `docker run --rm -v "$PWD/runner":/src -w /src golang:1.24 <go command>`
- Work on a feature branch, not `main`: `git switch -c m1-runner-core`.

All commands below run from the repository root unless a step says otherwise.

---

### Task 1: Scaffold the Go module

**Files:**
- Create: `runner/go.mod` (via `go mod init`)
- Create: `Makefile`
- Create: `.gitignore`

**Step 1: Initialise the module**

Run:
```bash
mkdir -p runner && cd runner && go mod init github.com/yutaofr/container-agent/runner && cd ..
```
Expected: `go: creating new go.mod: module github.com/yutaofr/container-agent/runner`

**Step 2: Add the Makefile**

```make
.PHONY: runner-test runner-build

runner-test:
	cd runner && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

runner-build:
	cd runner && go build -o ../bin/cagent-runner ./cmd/cagent-runner
```

**Step 3: Add `.gitignore`**

```
/bin/
```

**Step 4: Commit**

```bash
git add runner/go.mod Makefile .gitignore
git commit -m "build: scaffold runner Go module"
```

---

### Task 2: Path guard

The Runner may write only inside a cluster's workspace and read only there or in declared working directories. Symlinks must not provide a way out.

**Files:**
- Create: `runner/internal/guard/guard.go`
- Test: `runner/internal/guard/guard_test.go`

**Step 1: Write the failing test**

```go
package guard

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestWithin(t *testing.T) {
	cases := []struct {
		root, path string
		want       bool
	}{
		{"/ws", "/ws", true},
		{"/ws", "/ws/a/b", true},
		{"/ws", "/ws2/a", false},
		{"/ws", "/other", false},
		{"/ws", "/ws/../etc", false},
	}
	for _, c := range cases {
		if got := Within(c.root, filepath.Clean(c.path)); got != c.want {
			t.Errorf("Within(%q, %q) = %v, want %v", c.root, c.path, got, c.want)
		}
	}
}

func TestGuard(t *testing.T) {
	ws := t.TempDir()
	src := t.TempDir()
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A symlink inside the workspace that points outside it.
	if err := os.Symlink(outside, filepath.Join(ws, "escape")); err != nil {
		t.Fatal(err)
	}
	g, err := New(ws, src)
	if err != nil {
		t.Fatal(err)
	}

	if _, err := g.CheckWrite(filepath.Join(ws, "compose.yaml")); err != nil {
		t.Errorf("write new file in workspace: %v", err)
	}
	if _, err := g.CheckRead(filepath.Join(src, "pom.xml")); err != nil {
		t.Errorf("read in read-only root: %v", err)
	}
	if _, err := g.CheckWrite(filepath.Join(src, "pom.xml")); !errors.Is(err, ErrOutside) {
		t.Errorf("write in read-only root: got %v, want ErrOutside", err)
	}
	if _, err := g.CheckRead(filepath.Join(ws, "..", "x")); !errors.Is(err, ErrOutside) {
		t.Errorf("dot-dot traversal: got %v, want ErrOutside", err)
	}
	if _, err := g.CheckRead(filepath.Join(ws, "escape", "secret")); !errors.Is(err, ErrOutside) {
		t.Errorf("symlink escape: got %v, want ErrOutside", err)
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/guard/`
Expected: FAIL — build errors `undefined: Within`, `undefined: New`, `undefined: ErrOutside`.

**Step 3: Write the implementation**

```go
// Package guard confines Runner file access to approved directories.
package guard

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrOutside is returned when a path resolves outside every allowed root.
var ErrOutside = errors.New("path outside allowed roots")

// Guard allows reads and writes inside a workspace, and reads inside extra read-only roots.
type Guard struct {
	workspace string
	readOnly  []string
}

// New builds a Guard. Roots are resolved through symlinks so later checks compare real paths.
func New(workspace string, readOnly ...string) (*Guard, error) {
	ws, err := resolve(workspace)
	if err != nil {
		return nil, fmt.Errorf("workspace: %w", err)
	}
	g := &Guard{workspace: ws}
	for _, r := range readOnly {
		rr, err := resolve(r)
		if err != nil {
			return nil, fmt.Errorf("read-only root %s: %w", r, err)
		}
		g.readOnly = append(g.readOnly, rr)
	}
	return g, nil
}

// CheckRead returns the resolved path if it lies in the workspace or a read-only root.
func (g *Guard) CheckRead(path string) (string, error) {
	p, err := resolve(path)
	if err != nil {
		return "", err
	}
	if Within(g.workspace, p) {
		return p, nil
	}
	for _, r := range g.readOnly {
		if Within(r, p) {
			return p, nil
		}
	}
	return "", fmt.Errorf("%w: %s", ErrOutside, path)
}

// CheckWrite returns the resolved path if it lies in the workspace.
func (g *Guard) CheckWrite(path string) (string, error) {
	p, err := resolve(path)
	if err != nil {
		return "", err
	}
	if !Within(g.workspace, p) {
		return "", fmt.Errorf("%w: %s", ErrOutside, path)
	}
	return p, nil
}

// Within reports whether path equals root or sits below it. It compares paths lexically.
func Within(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// resolve makes path absolute and follows symlinks. A missing final element is allowed
// so callers can check a file before creating it.
func resolve(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	real, err := filepath.EvalSymlinks(abs)
	if err == nil {
		return real, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	parent, err := filepath.EvalSymlinks(filepath.Dir(abs))
	if err != nil {
		return "", err
	}
	return filepath.Join(parent, filepath.Base(abs)), nil
}
```

Why `resolve` falls back to the parent: `CheckWrite` must accept a file that does not exist yet (`compose.yaml` before the first write), but the parent directory must still be resolved through symlinks.

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/guard/`
Expected: `ok  github.com/yutaofr/container-agent/runner/internal/guard`

**Step 5: Commit**

```bash
git add runner/internal/guard
git commit -m "feat(runner): add path guard confining file access to approved roots"
```

---

### Task 3: Load and validate compose files

**Files:**
- Create: `runner/internal/compose/load.go`
- Test: `runner/internal/compose/load_test.go`
- Modify: `runner/go.mod`, `runner/go.sum` (dependency)

**Step 1: Add the dependency**

Run: `cd runner && go get github.com/compose-spec/compose-go/v2@v2.16.1`
Expected: `go: added github.com/compose-spec/compose-go/v2 v2.16.1`

**Step 2: Write the failing test**

`writeCompose` is shared by every test file in this package.

```go
package compose

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// writeCompose writes content to compose.yaml in a fresh temp dir and returns its path.
func writeCompose(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadValid(t *testing.T) {
	path := writeCompose(t, `
services:
  web:
    image: nginx:1.27
    volumes: ["./data:/data:ro"]
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	web, ok := p.Services["web"]
	if !ok {
		t.Fatal("service web missing")
	}
	want := filepath.Join(filepath.Dir(path), "data")
	if got := web.Volumes[0].Source; got != want {
		t.Errorf("bind source = %q, want resolved %q", got, want)
	}
}

func TestLoadRejectsUnknownKey(t *testing.T) {
	path := writeCompose(t, `
services:
  web:
    image: nginx:1.27
    bogus_key: 1
`)
	if _, err := Load(context.Background(), path, "demo"); err == nil {
		t.Fatal("want schema error for unknown key, got nil")
	}
}
```

**Step 3: Run it to verify it fails**

Run: `cd runner && go test ./internal/compose/`
Expected: FAIL — `undefined: Load`.

**Step 4: Write the implementation**

```go
// Package compose loads, checks, and labels compose projects using compose-go,
// the same loader docker compose uses.
package compose

import (
	"context"
	"fmt"
	"sort"

	"github.com/compose-spec/compose-go/v2/cli"
	"github.com/compose-spec/compose-go/v2/types"
)

// Load parses and schema-validates a compose file. Relative paths become absolute.
func Load(ctx context.Context, path, name string) (*types.Project, error) {
	opts, err := cli.NewProjectOptions([]string{path},
		cli.WithName(name),
		cli.WithResolvedPaths(true),
	)
	if err != nil {
		return nil, fmt.Errorf("compose options: %w", err)
	}
	p, err := opts.LoadProject(ctx)
	if err != nil {
		return nil, fmt.Errorf("load %s: %w", path, err)
	}
	return p, nil
}

// serviceNames returns service names in a stable order so reports are deterministic.
func serviceNames(p *types.Project) []string {
	names := make([]string, 0, len(p.Services))
	for n := range p.Services {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}
```

`WithResolvedPaths(true)` makes bind-mount sources absolute, which lets Task 4 compare them with the workspace. compose-go rejects unknown keys during load, so schema errors never reach Docker.

**Step 5: Run tests to verify they pass**

Run: `cd runner && go mod tidy && go test ./internal/compose/`
Expected: `ok  github.com/yutaofr/container-agent/runner/internal/compose`

**Step 6: Commit**

```bash
git add runner/go.mod runner/go.sum runner/internal/compose
git commit -m "feat(runner): load and schema-validate compose files with compose-go"
```

---

### Task 4: Security lint

Rules come from design section 8. A user approves an exception per service and rule, e.g. `web:privileged`.

**Files:**
- Create: `runner/internal/compose/lint.go`
- Test: `runner/internal/compose/lint_test.go`

**Step 1: Write the failing test**

```go
package compose

import (
	"context"
	"path/filepath"
	"reflect"
	"testing"
)

const riskyCompose = `
services:
  web:
    image: nginx:1.27
    privileged: true
    network_mode: host
    cap_add: [NET_ADMIN]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /etc:/host-etc:ro
      - ./data:/data
`

func TestLintFindsRiskySettings(t *testing.T) {
	path := writeCompose(t, riskyCompose)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	got := Lint(p, []string{filepath.Dir(path)}, nil)
	want := []Finding{
		{"web", RulePrivileged, "privileged: true"},
		{"web", RuleHostNetwork, "network_mode: host"},
		{"web", RuleCapAdd, "NET_ADMIN"},
		{"web", RuleDockerSock, "/var/run/docker.sock"},
		{"web", RuleHostMount, "/etc"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Lint() =\n%v\nwant\n%v", got, want)
	}
}

func TestLintHonoursApprovals(t *testing.T) {
	path := writeCompose(t, riskyCompose)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	allow := map[string]bool{
		"web:privileged": true, "web:host-network": true, "web:cap-add": true,
		"web:docker-sock": true, "web:host-mount": true,
	}
	if got := Lint(p, []string{filepath.Dir(path)}, allow); len(got) != 0 {
		t.Errorf("Lint() with all approvals = %v, want none", got)
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/compose/ -run Lint`
Expected: FAIL — `undefined: Lint`, `undefined: Finding`, `undefined: RulePrivileged`.

**Step 3: Write the implementation**

```go
package compose

import (
	"strings"

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/guard"
)

// Lint rule IDs. Users approve exceptions as "service:rule".
const (
	RulePrivileged  = "privileged"
	RuleDockerSock  = "docker-sock"
	RuleHostNetwork = "host-network"
	RuleHostMount   = "host-mount"
	RuleCapAdd      = "cap-add"
)

// Finding is one risky setting in one service.
type Finding struct {
	Service string `json:"service"`
	Rule    string `json:"rule"`
	Detail  string `json:"detail"`
}

// Lint reports risky settings. Bind mounts must sit under one of allowedRoots.
// allow holds "service:rule" pairs the user approved; those are not reported.
func Lint(p *types.Project, allowedRoots []string, allow map[string]bool) []Finding {
	out := []Finding{}
	add := func(svc, rule, detail string) {
		if !allow[svc+":"+rule] {
			out = append(out, Finding{Service: svc, Rule: rule, Detail: detail})
		}
	}
	for _, name := range serviceNames(p) {
		s := p.Services[name]
		if s.Privileged {
			add(name, RulePrivileged, "privileged: true")
		}
		if s.NetworkMode == "host" {
			add(name, RuleHostNetwork, "network_mode: host")
		}
		if len(s.CapAdd) > 0 {
			add(name, RuleCapAdd, strings.Join(s.CapAdd, ","))
		}
		for _, v := range s.Volumes {
			if v.Type != types.VolumeTypeBind {
				continue
			}
			if strings.HasSuffix(v.Source, "docker.sock") {
				add(name, RuleDockerSock, v.Source)
				continue
			}
			if !withinAny(v.Source, allowedRoots) {
				add(name, RuleHostMount, v.Source)
			}
		}
	}
	return out
}

func withinAny(path string, roots []string) bool {
	for _, r := range roots {
		if guard.Within(r, path) {
			return true
		}
	}
	return false
}
```

The `docker.sock` check runs before the host-mount check and `continue`s, so a socket mount reports one precise finding instead of two.

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/compose/`
Expected: `ok`

**Step 5: Commit**

```bash
git add runner/internal/compose/lint.go runner/internal/compose/lint_test.go
git commit -m "feat(runner): lint compose projects for risky settings"
```

---

### Task 5: Managed labels

Every container the Runner starts carries `cagent.managed=true`; M3's Docker operations refuse anything without it.

**Files:**
- Create: `runner/internal/compose/labels.go`
- Test: `runner/internal/compose/labels_test.go`

**Step 1: Write the failing test**

```go
package compose

import (
	"context"
	"strings"
	"testing"
)

func TestApplyManagedLabels(t *testing.T) {
	path := writeCompose(t, `
services:
  web: { image: nginx:1.27, labels: { team: core } }
  db:  { image: mysql:8.0 }
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	ApplyManagedLabels(p, "orders-poc")

	for name, s := range p.Services {
		if !IsManaged(s.Labels) {
			t.Errorf("%s: not managed: %v", name, s.Labels)
		}
		if s.Labels[LabelCluster] != "orders-poc" {
			t.Errorf("%s: cluster label = %q", name, s.Labels[LabelCluster])
		}
	}
	if p.Services["web"].Labels["team"] != "core" {
		t.Error("existing label was dropped")
	}

	out, err := p.MarshalYAML()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), "cagent.managed: \"true\"") {
		t.Errorf("marshalled YAML lacks managed label:\n%s", out)
	}
}

func TestIsManaged(t *testing.T) {
	if IsManaged(map[string]string{"other": "x"}) {
		t.Error("unlabelled container reported as managed")
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/compose/ -run Managed`
Expected: FAIL — `undefined: ApplyManagedLabels`, `undefined: IsManaged`, `undefined: LabelCluster`.

**Step 3: Write the implementation**

```go
package compose

import "github.com/compose-spec/compose-go/v2/types"

// Labels the Runner puts on every service it starts. Docker operations only
// ever target containers that carry LabelManaged.
const (
	LabelManaged = "cagent.managed"
	LabelCluster = "cagent.cluster"
)

// ApplyManagedLabels marks every service in p as belonging to cluster.
func ApplyManagedLabels(p *types.Project, cluster string) {
	for name, s := range p.Services {
		if s.Labels == nil {
			s.Labels = types.Labels{}
		}
		s.Labels[LabelManaged] = "true"
		s.Labels[LabelCluster] = cluster
		p.Services[name] = s
	}
}

// IsManaged reports whether a container's labels mark it as Runner-managed.
func IsManaged(labels map[string]string) bool {
	return labels[LabelManaged] == "true"
}
```

`p.Services` is a map of structs, so the loop must write the modified copy back with `p.Services[name] = s`.

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/compose/`
Expected: `ok`

**Step 5: Commit**

```bash
git add runner/internal/compose/labels.go runner/internal/compose/labels_test.go
git commit -m "feat(runner): label managed services"
```

---

### Task 6: Host-port pre-check

Port conflicts are the most common start-up failure. Catching them before `up` removes the need for a self-healing round.

**Files:**
- Create: `runner/internal/compose/ports.go`
- Test: `runner/internal/compose/ports_test.go`

**Step 1: Write the failing test**

The test injects `free` so it never depends on what is running on the machine. Service `c` covers two skipped cases: a UDP port and an unpublished port (`"80"`).

```go
package compose

import (
	"context"
	"net"
	"reflect"
	"testing"
)

func TestHostPortConflicts(t *testing.T) {
	path := writeCompose(t, `
services:
  a: { image: nginx, ports: ["8080:80", "9000-9001:9000-9001"] }
  b: { image: nginx, ports: ["8080:80"] }
  c: { image: nginx, ports: ["5353:53/udp", "80"] }
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	inUse := map[int]bool{9001: true}
	got := HostPortConflicts(p, func(port int) bool { return !inUse[port] })
	want := []PortConflict{
		{"a", 9001, "in-use"},
		{"b", 8080, "duplicate"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("HostPortConflicts() = %v, want %v", got, want)
	}
}

func TestTCPPortFree(t *testing.T) {
	l, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	if TCPPortFree(l.Addr().(*net.TCPAddr).Port) {
		t.Error("port held by a listener reported free")
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/compose/ -run Port`
Expected: FAIL — `undefined: HostPortConflicts`, `undefined: PortConflict`, `undefined: TCPPortFree`.

**Step 3: Write the implementation**

```go
package compose

import (
	"net"
	"strconv"
	"strings"

	"github.com/compose-spec/compose-go/v2/types"
)

// PortConflict is a published host port that cannot be bound.
type PortConflict struct {
	Service string `json:"service"`
	Port    int    `json:"port"`
	Reason  string `json:"reason"` // "in-use" or "duplicate"
}

// HostPortConflicts lists published TCP host ports that are taken on the host
// or published twice within the project. free reports whether a port can be bound.
func HostPortConflicts(p *types.Project, free func(port int) bool) []PortConflict {
	out := []PortConflict{}
	seen := map[int]string{}
	for _, name := range serviceNames(p) {
		for _, pc := range p.Services[name].Ports {
			if pc.Protocol != "" && pc.Protocol != "tcp" {
				continue
			}
			for _, port := range expandPorts(pc.Published) {
				if owner, dup := seen[port]; dup && owner != name {
					out = append(out, PortConflict{name, port, "duplicate"})
					continue
				}
				seen[port] = name
				if !free(port) {
					out = append(out, PortConflict{name, port, "in-use"})
				}
			}
		}
	}
	return out
}

// TCPPortFree reports whether a TCP port can be bound on all interfaces.
func TCPPortFree(port int) bool {
	l, err := net.Listen("tcp", ":"+strconv.Itoa(port))
	if err != nil {
		return false
	}
	_ = l.Close()
	return true
}

// expandPorts turns "8080" or "8000-8002" into ports. Empty or invalid input yields none.
func expandPorts(published string) []int {
	if published == "" {
		return nil
	}
	lo, hi, isRange := strings.Cut(published, "-")
	start, err := strconv.Atoi(lo)
	if err != nil {
		return nil
	}
	end := start
	if isRange {
		if end, err = strconv.Atoi(hi); err != nil || end < start {
			return nil
		}
	}
	ports := make([]int, 0, end-start+1)
	for p := start; p <= end; p++ {
		ports = append(ports, p)
	}
	return ports
}
```

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/compose/`
Expected: `ok`

**Step 5: Commit**

```bash
git add runner/internal/compose/ports.go runner/internal/compose/ports_test.go
git commit -m "feat(runner): detect host port conflicts before start-up"
```

---

### Task 7: Secret redaction

Snapshots are redacted on the Runner before upload (design section 8). Rule order matters: URL credentials and bearer tokens are masked before the generic `key=value` rule could mangle them.

**Files:**
- Create: `runner/internal/redact/redact.go`
- Test: `runner/internal/redact/redact_test.go`

**Step 1: Write the failing test**

```go
package redact

import "testing"

func TestString(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"jdbc password param",
			"jdbc:mysql://db:3306/app?user=root&password=s3cret&useSSL=false",
			"jdbc:mysql://db:3306/app?user=root&password=******&useSSL=false"},
		{"env var",
			"SPRING_DATASOURCE_PASSWORD=hunter2",
			"SPRING_DATASOURCE_PASSWORD=******"},
		{"json field",
			`{"api_key": "abc123", "user": "bob"}`,
			`{"api_key": "******", "user": "bob"}`},
		{"url credentials",
			"connecting to postgres://admin:pa55@db:5432/x",
			"connecting to postgres://admin:******@db:5432/x"},
		{"bearer token",
			"Authorization: Bearer eyJhbGciOi.abc.def",
			"Authorization: Bearer ******"},
		{"aws access key id",
			"key AKIAIOSFODNN7EXAMPLE loaded",
			"key ****** loaded"},
		{"aws secret with spaces",
			"aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
			"aws_secret_access_key = ******"},
		{"private key",
			"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
			"-----PRIVATE KEY ******-----"},
		{"plain log line untouched",
			"Started Application in 3.2 seconds (tokenizer ready)",
			"Started Application in 3.2 seconds (tokenizer ready)"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := String(c.in); got != c.want {
				t.Errorf("String(%q)\n got %q\nwant %q", c.in, got, c.want)
			}
		})
	}
}

func TestStringIsIdempotent(t *testing.T) {
	in := "password=s3cret token: abc"
	once := String(in)
	if twice := String(once); twice != once {
		t.Errorf("second pass changed output: %q -> %q", once, twice)
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/redact/`
Expected: FAIL — `undefined: String`.

**Step 3: Write the implementation**

```go
// Package redact masks secrets in text before it leaves the user's machine.
package redact

import "regexp"

// Mask replaces every secret value.
const Mask = "******"

// rules run in order; earlier rules handle shapes later rules would mangle.
var rules = []struct {
	re   *regexp.Regexp
	repl string
}{
	// PEM private key blocks.
	{regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`), "-----PRIVATE KEY " + Mask + "-----"},
	// AWS access key IDs.
	{regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`), Mask},
	// Credentials embedded in URLs: scheme://user:password@host.
	{regexp.MustCompile(`(://[^:/@\s]+:)[^@\s]+@`), "${1}" + Mask + "@"},
	// Bearer tokens.
	{regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+`), "${1}" + Mask},
	// key=value, key: value, "key": "value" where the key names a secret.
	{regexp.MustCompile(`(?i)("?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*"?\s*[:=]\s*"?)([^"&;,\s]+)`), "${1}" + Mask},
}

// String returns s with every recognised secret replaced by Mask.
func String(s string) string {
	for _, r := range rules {
		s = r.re.ReplaceAllString(s, r.repl)
	}
	return s
}
```

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/redact/`
Expected: `ok`

**Step 5: Commit**

```bash
git add runner/internal/redact
git commit -m "feat(runner): redact secrets from log text"
```

---

### Task 8: Error fingerprinting

Signatures drive the per-problem retry budget and repeat detection (design sections 5.2 and 5.5). Known patterns get readable kinds; anything else gets a hash of the normalized line, so the same failure in two runs yields the same signature.

**Files:**
- Create: `runner/internal/fingerprint/fingerprint.go`
- Test: `runner/internal/fingerprint/fingerprint_test.go`

**Step 1: Write the failing test**

```go
package fingerprint

import (
	"strings"
	"testing"
)

func TestFromLogKnownPatterns(t *testing.T) {
	cases := []struct{ line, kind string }{
		{"bind: Address already in use", "bind:address_in_use"},
		{"java.net.ConnectException: Connection refused (Connection refused)", "conn:refused"},
		{"java.net.UnknownHostException: mysql", "dns:unresolved"},
		{"java.lang.OutOfMemoryError: Java heap space", "oom:app"},
		{"Access denied for user 'root'@'172.18.0.3'", "auth:denied"},
		{"exec /app/server: exec format error", "image:arch_mismatch"},
		{"open /config/app.yml: no such file or directory", "fs:not_found"},
	}
	for _, c := range cases {
		if got := FromLog("app", c.line); got.Kind != c.kind {
			t.Errorf("FromLog(%q).Kind = %q, want %q", c.line, got.Kind, c.kind)
		}
	}
}

func TestFromLogUnknownIsStableAcrossRuns(t *testing.T) {
	a := FromLog("app", "2026-10-17T08:01:02.123Z ERROR worker 42 failed at 10.0.0.7:9092 id=3f2a9c1b0d4e5f60")
	b := FromLog("app", "2026-10-18 09:11:12,456 ERROR worker 7 failed at 10.0.0.9:9093 id=aa2a9c1b0d4e5f61")
	if a != b {
		t.Errorf("same failure, different signatures: %v vs %v", a, b)
	}
	if !strings.HasPrefix(a.Kind, "unknown:") || len(a.Kind) != len("unknown:")+12 {
		t.Errorf("unexpected unknown kind %q", a.Kind)
	}
	if c := FromLog("app", "ERROR something else entirely"); c == a {
		t.Error("different failures share a signature")
	}
}

func TestFromExit(t *testing.T) {
	cases := []struct {
		code int
		oom  bool
		want string
	}{
		{137, false, "oom:killed@app"},
		{1, true, "oom:killed@app"},
		{1, false, "exit:1@app"},
		{0, false, "exit:0@app"},
	}
	for _, c := range cases {
		if got := FromExit("app", c.code, c.oom).String(); got != c.want {
			t.Errorf("FromExit(%d, %v) = %q, want %q", c.code, c.oom, got, c.want)
		}
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./internal/fingerprint/`
Expected: FAIL — `undefined: FromLog`, `undefined: FromExit`.

**Step 3: Write the implementation**

```go
// Package fingerprint turns container failures into stable error signatures,
// so the self-healing loop can count attempts per problem and spot repeats.
package fingerprint

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
)

// Signature identifies one kind of failure in one service.
type Signature struct {
	Kind    string `json:"kind"`    // e.g. "bind:address_in_use", "unknown:3f2a9c1b0d4e"
	Service string `json:"service"` // compose service name
}

// String renders the signature as kind@service.
func (s Signature) String() string { return s.Kind + "@" + s.Service }

// classifiers map well-known log patterns to kinds. First match wins.
var classifiers = []struct {
	re   *regexp.Regexp
	kind string
}{
	{regexp.MustCompile(`(?i)address already in use`), "bind:address_in_use"},
	{regexp.MustCompile(`(?i)connection refused`), "conn:refused"},
	{regexp.MustCompile(`(?i)unknownhostexception|no such host|name or service not known|could not resolve`), "dns:unresolved"},
	{regexp.MustCompile(`(?i)outofmemoryerror|out of memory`), "oom:app"},
	{regexp.MustCompile(`(?i)access denied for user|authentication failed`), "auth:denied"},
	{regexp.MustCompile(`(?i)exec format error`), "image:arch_mismatch"},
	{regexp.MustCompile(`(?i)permission denied`), "fs:permission_denied"},
	{regexp.MustCompile(`(?i)no such file or directory`), "fs:not_found"},
}

// FromLog fingerprints one error log line from service.
func FromLog(service, line string) Signature {
	for _, c := range classifiers {
		if c.re.MatchString(line) {
			return Signature{Kind: c.kind, Service: service}
		}
	}
	sum := sha1.Sum([]byte(Normalize(line)))
	return Signature{Kind: "unknown:" + hex.EncodeToString(sum[:])[:12], Service: service}
}

// FromExit fingerprints a container that stopped.
func FromExit(service string, exitCode int, oomKilled bool) Signature {
	if oomKilled || exitCode == 137 {
		return Signature{Kind: "oom:killed", Service: service}
	}
	return Signature{Kind: fmt.Sprintf("exit:%d", exitCode), Service: service}
}

// normalizers strip values that vary between runs of the same failure. Order matters:
// timestamps and UUIDs go before the generic number rule eats their digits.
var normalizers = []struct {
	re   *regexp.Regexp
	repl string
}{
	{regexp.MustCompile(`\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?`), "<ts>"},
	{regexp.MustCompile(`(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b`), "<uuid>"},
	{regexp.MustCompile(`\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b`), "<ip>"},
	{regexp.MustCompile(`(?i)\b[0-9a-f]{12,}\b`), "<hex>"},
	{regexp.MustCompile(`\d+`), "<n>"},
	{regexp.MustCompile(`\s+`), " "},
}

// Normalize lowercases line and replaces run-specific values with placeholders.
func Normalize(line string) string {
	s := line
	for _, n := range normalizers {
		s = n.re.ReplaceAllString(s, n.repl)
	}
	return strings.ToLower(strings.TrimSpace(s))
}
```

Note the classifier order: "permission denied" precedes "no such file or directory" because some runtimes print both on one line, and permission is the root cause.

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./internal/fingerprint/`
Expected: `ok`

**Step 5: Commit**

```bash
git add runner/internal/fingerprint
git commit -m "feat(runner): fingerprint container failures into stable signatures"
```

---

### Task 9: `cagent-runner validate`

**Files:**
- Create: `runner/cmd/cagent-runner/main.go`
- Test: `runner/cmd/cagent-runner/main_test.go`

**Step 1: Write the failing test**

`run` takes its writers and port checker as arguments, so tests never touch real stdout or real ports.

```go
package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func allFree(int) bool { return true }

func writeFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestValidateClean(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, ports: [\"8080:80\"] }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitOK {
		t.Fatalf("exit %d, stderr: %s", code, errOut.String())
	}
	var rep Report
	if err := json.Unmarshal(out.Bytes(), &rep); err != nil {
		t.Fatalf("bad JSON %q: %v", out.String(), err)
	}
	if rep.Project != "validate" || len(rep.Findings) != 0 || len(rep.PortConflicts) != 0 {
		t.Errorf("unexpected report %+v", rep)
	}
	// Consumers expect empty arrays, never null.
	for _, field := range []string{`"findings": []`, `"port_conflicts": []`} {
		if !bytes.Contains(out.Bytes(), []byte(field)) {
			t.Errorf("report lacks %s:\n%s", field, out.String())
		}
	}
}

func TestValidateReportsFindings(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, privileged: true }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitFindings {
		t.Fatalf("exit %d, want %d", code, exitFindings)
	}
	if !bytes.Contains(out.Bytes(), []byte(`"rule": "privileged"`)) {
		t.Errorf("report lacks privileged finding:\n%s", out.String())
	}
}

func TestValidateAllowFlag(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, privileged: true }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path, "-allow", "web:privileged"}, &out, &errOut, allFree); code != exitOK {
		t.Fatalf("exit %d, want %d; out: %s", code, exitOK, out.String())
	}
}

func TestValidateInvalidFile(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx, bogus_key: 1 }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitError {
		t.Fatalf("exit %d, want %d", code, exitError)
	}
	if errOut.Len() == 0 {
		t.Error("no error message on stderr")
	}
}
```

**Step 2: Run it to verify it fails**

Run: `cd runner && go test ./cmd/cagent-runner/`
Expected: FAIL — `undefined: run`, `undefined: Report`, `undefined: exitOK`.

**Step 3: Write the implementation**

```go
// Command cagent-runner executes whitelisted cluster operations on the user's machine.
// Milestone 1 ships only the offline "validate" subcommand.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/compose"
)

// Exit codes for validate.
const (
	exitOK       = 0
	exitFindings = 1
	exitError    = 2
)

// Report is the JSON that validate prints.
type Report struct {
	Project       string                 `json:"project"`
	Findings      []compose.Finding      `json:"findings"`
	PortConflicts []compose.PortConflict `json:"port_conflicts"`
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, compose.TCPPortFree))
}

// run is main without process globals, so tests can drive it.
func run(args []string, stdout, stderr io.Writer, free func(int) bool) int {
	if len(args) == 0 || args[0] != "validate" {
		fmt.Fprintln(stderr, "usage: cagent-runner validate -f compose.yaml [-name N] [-workspace DIR] [-allow svc:rule,...]")
		return exitError
	}
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	file := fs.String("f", "compose.yaml", "compose file")
	name := fs.String("name", "validate", "compose project name")
	workspace := fs.String("workspace", "", "directory bind mounts may use (default: compose file dir)")
	allowFlag := fs.String("allow", "", "approved exceptions, comma-separated service:rule")
	if err := fs.Parse(args[1:]); err != nil {
		return exitError
	}

	abs, err := filepath.Abs(*file)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	ws := *workspace
	if ws == "" {
		ws = filepath.Dir(abs)
	}
	allow := map[string]bool{}
	for _, a := range strings.Split(*allowFlag, ",") {
		if a = strings.TrimSpace(a); a != "" {
			allow[a] = true
		}
	}

	p, err := compose.Load(context.Background(), abs, *name)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	rep := Report{
		Project:       p.Name,
		Findings:      compose.Lint(p, []string{ws}, allow),
		PortConflicts: compose.HostPortConflicts(p, free),
	}
	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(rep); err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	if len(rep.Findings) > 0 || len(rep.PortConflicts) > 0 {
		return exitFindings
	}
	return exitOK
}
```

**Step 4: Run tests to verify they pass**

Run: `cd runner && go test ./cmd/cagent-runner/`
Expected: `ok`

**Step 5: Try it by hand**

```bash
make runner-build
printf 'services:\n  web: { image: nginx:1.27, privileged: true }\n' > /tmp/risky.yaml
./bin/cagent-runner validate -f /tmp/risky.yaml; echo "exit=$?"
```
Expected: a JSON report with one `"rule": "privileged"` finding, then `exit=1`.

**Step 6: Commit**

```bash
git add runner/cmd
git commit -m "feat(runner): add validate command"
```

---

### Task 10: Continuous integration

**Files:**
- Create: `.github/workflows/runner.yml`

**Step 1: Add the workflow**

```yaml
name: runner

on:
  push:
    paths: ["runner/**", ".github/workflows/runner.yml"]
  pull_request:
    paths: ["runner/**", ".github/workflows/runner.yml"]

jobs:
  test:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: runner
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version-file: runner/go.mod
      - name: gofmt
        run: test -z "$(gofmt -l .)"
      - run: go vet ./...
      - run: go test ./...
```

**Step 2: Run the same checks locally**

Run: `cd runner && test -z "$(gofmt -l .)" && go vet ./... && go test ./...`
Expected: five `ok` lines (cmd/cagent-runner, compose, fingerprint, guard, redact) and no gofmt output.

**Step 3: Commit**

```bash
git add .github/workflows/runner.yml
git commit -m "ci: test runner on push and pull request"
```

Pushing a workflow file needs a GitHub credential with the `workflow` scope. If the push is rejected for that reason, ask the user to refresh their credential (`gh auth refresh -s workflow`) rather than dropping the file.

---

### Task 11: Security hardening

Added after Task 10 in response to automated security review of Tasks 2 and 4. Each step was test-first, in separate commits. The `validate` report shape did not change, so the CLI tests are unchanged.

**Files:**
- Modify: `runner/internal/guard/guard.go`, `runner/internal/guard/guard_test.go`
- Modify: `runner/internal/compose/lint.go`, `runner/internal/compose/lint_test.go`
- Create: `runner/internal/compose/lint_rules_test.go`
- Modify: `docs/plans/2026-10-17-container-agent-design.md` (section 8 only)

**What was done:**

1. **Dangling symlinks in the guard** (`b328e82`, during the first batch). A symlink inside the workspace whose target did not exist made `EvalSymlinks` fail with `ErrNotExist`, and the parent-directory fallback then reported the path as inside the workspace. A symlink as the final element is now refused with `ErrOutside`.
2. **`guard.Resolve`** (`3a220d6`). The resolution logic is exported so lint reuses it. It walks up to the nearest existing ancestor, so a not-yet-created nested path in the workspace resolves, and it refuses a dangling symlink anywhere on the path.
3. **Symlink-safe bind checks** (`5ff2fd1`). `Lint` resolves each bind source and each allowed root before comparing. A source that cannot be resolved is reported as `host-mount` (fail closed). `docker-sock` now also fires when the resolved source is a socket or a directory containing one, such as `/var/run` or `/run`. `Finding.Detail` keeps the path as declared. The socket paths live in the package variable `dockerSocketPaths` so tests can substitute paths that exist on every machine.
4. **Extended rule set** (`9c993eb`). Seven new rule IDs, approvable as `service:rule`:

   | Rule ID | Catches |
   | --- | --- |
   | `pid-host` | `pid: host` |
   | `ipc-host` | `ipc: host` |
   | `userns-host` | `userns_mode: host` |
   | `devices` | any `devices` entry |
   | `security-opt` | `seccomp` or `apparmor` unconfined, `label=disable` / `label:disable`, `no-new-privileges:false` |
   | `cgroup-parent` | any `cgroup_parent` |
   | `volume-driver` | a top-level volume with a non-`local` driver, or a `local` volume whose `device` is not a bind path, `nfs`/`nfs4`/`cifs`, or `tmpfs` (for example `type: ext4` on `/dev/sda1`) |

   Top-level local volumes whose `driver_opts` make `device` a bind-mounted host path (`o` contains `bind` or `rbind`, or `type` is `none` or `bind`) get the `host-mount` and `docker-sock` checks. External volumes are not inspected. For findings on a top-level volume the `service` field holds the volume name, so approvals read `data:volume-driver`. Output order: per service in name order (`privileged`, `host-network`, `pid-host`, `ipc-host`, `userns-host`, `cap-add`, `devices`, `security-opt`, `cgroup-parent`, then volume mounts), then top-level volumes in name order.
5. **Design doc** (`3982ae4`). Section 8 now has a table of every rule ID, the mount-source check flow, and the statement that sources are checked after symlink resolution and fail closed.

**Verification:** `make runner-test` passes in the `golang:1.24` container. `cagent-runner validate` exits 1 on the risky sample and on a file using the new rules, and 0 on a clean file.

**Known gaps, not covered:** `security_opt: systempaths=unconfined` and non-default `seccomp` / `label` values, `device_cgroup_rules`, `cgroupns_mode: host`, `uts: host`, a non-default `runtime`, `sysctls`, `pid` / `ipc` modes other than `host` (for example `container:<id>`), and the TOCTOU window between the lint check and `docker compose up`. Whether to move `security_opt` to an allowlist and add rules for the others is a design decision for section 8.

---

## Milestone 1 done when

- [ ] `make runner-test` passes locally
- [ ] The `runner` workflow is green on GitHub
- [ ] `cagent-runner validate` exits 1 on the risky sample and 0 on a clean file
- [ ] All eleven tasks committed on `m1-runner-core`; branch ready for review via superpowers:requesting-code-review
