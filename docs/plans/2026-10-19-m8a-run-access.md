# M8a Run and Access Interfaces Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make runs durable, recoverable, and access-controlled behind an HTTP and SSE interface, so that every pending item can be answered over HTTP alone and a crash never consumes a pending item twice or advances a graph on its own.

**Architecture:** One process (`cagent-platform serve`) holds the hub, a FastAPI app, and `RunService`. `RunService` is the only code that changes runs: it accepts each command in one SQLite transaction, runs the LangGraph orchestrator in a background task, commits whitelisted events, and records the result only if the run still belongs to that execution. A dispatch log written before every Runner call, plus a Runner that runs one call at a time per process and reports `status`, lets a restarted platform tell what may have happened and send unclear runs to `reconcile`, from which only an approved recovery plan continues.

**Tech Stack:** Go 1.24 (in Docker), Python 3.12, LangGraph 1.2 (`astream` v2, `AsyncSqliteSaver`), aiosqlite, FastAPI, uvicorn, httpx (tests).

**Design:** [2026-10-19-m8-web-ui-design.md](2026-10-19-m8-web-ui-design.md) — sections 4–13 are the contract this plan implements. Read them before Task 1.

---

## Before you start

- **Work in your own worktree from `main`** (at or after `2b6a946`). Run every command from the repository root unless it starts with `cd`.
- **This machine has no Go.** Go runs in Docker: `docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo golang:1.24 …` (the named volume caches modules). Task 19 builds a macOS Runner binary into `bin/` (ignored by git) to run on the host.
- **Python:** `cd platform && uv sync` once before Task 1.
- **Copy code exactly.** Every block in this plan was implemented and tested first, then replayed on a fresh copy of `main` (see "How this plan was verified" below). "In `file`, replace: … with: …" means an exact text replacement; each "replace" block occurs exactly once in the file at that point. If a block does not match, stop and report — do not improvise.
- **Fail first.** Step 2 of each task must fail as described before you implement. Expected outputs below are the replay's real output without timings; test counts must match exactly.
- **Checkpoints:** stop and report after Tasks 3, 7, 10, 12, 15, 18, and 20. Do not push and do not open a PR.
- **Security findings** outside a task's scope: record them in your report; do not fix them.

## How this plan was verified

- The implementation was written task by task on a scratch branch, one commit per task.
- A generator turned each commit into the actions shown here (create a file, or unique replace blocks) and checked that applying them to the parent commit reproduces the commit byte for byte.
- The same actions were then replayed on a fresh worktree of `main`: every fail-first run failed, every test run passed with the counts shown, every commit left a clean tree, and the final tree was identical to the verified branch.
- Final results on the replayed tree: 15 Go packages `ok`; Runner integration tests pass on real Docker; Platform lint and tests — 434 passed, 11 skipped; Every platform test with the real Runner — 441 passed, 4 skipped; With a local Ollama serving the tiers in platform/models.yaml — 4 passed. Nothing was left running.


## Tasks

### Task 1: Runner: one serial worker per process, and `status`

Today `serveOnce` starts a new worker for every connection, and subcommands run on `context.Background()`, so after a reconnect the old connection's call — and its queue — keep running beside new calls (design section 4). Move the queue and the worker into a process-wide `executor`: each call carries its connection's generation, a closed connection's queued calls are dropped, a started call runs to completion and answers only on its own connection, and the read loop answers `status` (instance, active call, queued calls per cluster) without queueing. The docker `Fake` gains a `Block` channel so tests control exactly when a call ends.

**Files:**

- Create: `runner/cmd/cagent-runner/executor.go`
- Test (create): `runner/cmd/cagent-runner/executor_test.go`
- Modify: `runner/cmd/cagent-runner/serve.go`
- Modify: `runner/internal/dockercli/fake.go`

**Step 1: Write the failing tests**

Create `runner/cmd/cagent-runner/executor_test.go`:

```go
package main

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
)

func psKey(cluster string) string {
	return "ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-" + cluster
}

// send writes one request without waiting for its answer.
func send(t *testing.T, c *websocket.Conn, id int, method string, params any) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req := map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params}
	if err := wsjson.Write(ctx, c, req); err != nil {
		t.Fatal(err)
	}
}

// waitSeen waits until the fake Docker has received the command.
func waitSeen(t *testing.T, f *dockercli.Fake, key string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !f.Seen(key) {
		if time.Now().After(deadline) {
			t.Fatalf("docker never saw %q", key)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func statusOf(t *testing.T, resp rpcResponse) runnerStatus {
	t.Helper()
	if resp.Error != nil || resp.Result == nil || resp.Result.ExitCode != exitOK {
		t.Fatalf("status: %+v", resp)
	}
	var st runnerStatus
	if err := json.Unmarshal(resp.Result.Output, &st); err != nil {
		t.Fatal(err)
	}
	return st
}

// reconnect drops the hub side of c and returns the runner's next connection, after its hello.
func reconnect(t *testing.T, c *websocket.Conn, conns <-chan *websocket.Conn) *websocket.Conn {
	t.Helper()
	c.Close(websocket.StatusGoingAway, "hub restarting")
	c2 := next(t, conns)
	var hello rpcRequest
	readJSON(t, c2, &hello)
	return c2
}

func TestServeNeverRunsAnOldCallBesideANewOne(t *testing.T) {
	release := make(chan struct{})
	d, f, _ := withDocker(t, map[string]dockercli.FakeResponse{
		psKey("old"): {Block: release},
		psKey("new"): {},
	})
	c, conns := startServe(t, d, nil)
	send(t, c, 1, "snapshot", map[string]any{"name": "old"})
	waitSeen(t, f, psKey("old"))

	c2 := reconnect(t, c, conns)
	send(t, c2, 2, "snapshot", map[string]any{"name": "new"})
	st := statusOf(t, call(t, c2, 3, "status", map[string]any{}))
	if st.Active == nil || st.Active.Method != "snapshot" || st.Active.Cluster != "old" {
		t.Errorf("active = %+v, want the old snapshot", st.Active)
	}
	if st.Queued["new"] != 1 {
		t.Errorf("queued = %v, want new:1", st.Queued)
	}
	if f.Seen(psKey("new")) {
		t.Fatal("the new call started while the old one was still running")
	}

	close(release)
	var resp rpcResponse
	readJSON(t, c2, &resp) // the old call's answer must not arrive here
	if string(resp.ID) != "2" || resp.Result == nil {
		t.Fatalf("got %+v, want the answer to call 2", resp)
	}
}

func TestServeDropsQueuedCallsWhenTheConnectionEnds(t *testing.T) {
	release := make(chan struct{})
	d, f, _ := withDocker(t, map[string]dockercli.FakeResponse{
		psKey("a"): {Block: release},
		psKey("b"): {},
	})
	c, conns := startServe(t, d, nil)
	send(t, c, 1, "snapshot", map[string]any{"name": "a"})
	waitSeen(t, f, psKey("a"))
	send(t, c, 2, "snapshot", map[string]any{"name": "b"}) // queued behind a

	c2 := reconnect(t, c, conns)
	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for id := 10; ; id++ {
		st := statusOf(t, call(t, c2, id, "status", nil))
		if st.Active == nil && len(st.Queued) == 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("runner never went idle: %+v", st)
		}
		time.Sleep(5 * time.Millisecond)
	}
	if f.Seen(psKey("b")) {
		t.Error("a call queued on the closed connection ran after the reconnect")
	}
}

func TestServeAnswersStatusDuringALongCallAndWithAFullQueue(t *testing.T) {
	release := make(chan struct{})
	d, f, _ := withDocker(t, map[string]dockercli.FakeResponse{
		psKey("a"): {Block: release},
		psKey("b"): {},
	})
	c, _ := startServe(t, d, func(cfg *serveConfig) { cfg.queueSize = 1 })
	send(t, c, 1, "snapshot", map[string]any{"name": "a"})
	waitSeen(t, f, psKey("a"))
	send(t, c, 2, "snapshot", map[string]any{"name": "b"}) // fills the queue

	if resp := call(t, c, 3, "snapshot", map[string]any{"name": "c"}); resp.Error == nil || resp.Error.Code != rpcBusy {
		t.Fatalf("third call: %+v, want busy", resp)
	}
	st := statusOf(t, call(t, c, 4, "status", nil))
	if st.Active == nil || st.Active.Cluster != "a" || st.Queued["b"] != 1 || st.InstanceID == "" {
		t.Errorf("status = %+v", st)
	}
	if resp := call(t, c, 5, "status", map[string]any{"extra": 1}); resp.Error == nil || resp.Error.Code != rpcInvalidParams {
		t.Errorf("status with params: %+v", resp)
	}
	close(release)
	for _, want := range []string{"1", "2"} {
		var resp rpcResponse
		readJSON(t, c, &resp)
		if string(resp.ID) != want || resp.Result == nil {
			t.Fatalf("got %+v, want the answer to call %s", resp, want)
		}
	}
}

func TestServeKeepsItsInstanceAcrossReconnects(t *testing.T) {
	url, conns := fakeHub(t)
	start := func() {
		cfg := serveConfig{hub: url, token: testToken, backoff: 10 * time.Millisecond,
			backoffMax: 50 * time.Millisecond, pingEvery: time.Hour}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { _ = serveLoop(ctx, cfg, testDeps(t), &strings.Builder{}); close(done) }()
		t.Cleanup(func() { cancel(); <-done })
	}
	helloOf := func(c *websocket.Conn) (id string, tools []string) {
		var hello struct {
			Params struct {
				InstanceID string   `json:"instance_id"`
				Tools      []string `json:"tools"`
			} `json:"params"`
		}
		readJSON(t, c, &hello)
		return hello.Params.InstanceID, hello.Params.Tools
	}

	start()
	c := next(t, conns)
	first, tools := helloOf(c)
	if first == "" || !slices.Contains(tools, "status") {
		t.Fatalf("hello: instance %q, tools %v", first, tools)
	}
	c.Close(websocket.StatusGoingAway, "hub restarting")
	if again, _ := helloOf(next(t, conns)); again != first {
		t.Errorf("instance changed across a reconnect: %q → %q", first, again)
	}

	start() // another process
	if other, _ := helloOf(next(t, conns)); other == first {
		t.Error("a second runner process reused the instance id")
	}
}
```

**Step 2: Run them to make sure they fail**

Run:

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo golang:1.24 sh -c 'cd runner && go test ./cmd/cagent-runner/ -run "TestServe(NeverRuns|DropsQueued|AnswersStatus|KeepsItsInstance)"'
```

Expected:

```
FAIL	github.com/yutaofr/container-agent/runner/cmd/cagent-runner [build failed]
FAIL
# github.com/yutaofr/container-agent/runner/cmd/cagent-runner [github.com/yutaofr/container-agent/runner/cmd/cagent-runner.test]
cmd/cagent-runner/executor_test.go:44:47: undefined: runnerStatus
cmd/cagent-runner/executor_test.go:49:9: undefined: runnerStatus
cmd/cagent-runner/executor_test.go:69:18: unknown field Block in struct literal of type dockercli.FakeResponse
cmd/cagent-runner/executor_test.go:100:16: unknown field Block in struct literal of type dockercli.FakeResponse
cmd/cagent-runner/executor_test.go:129:16: unknown field Block in struct literal of type dockercli.FakeResponse
cmd/cagent-runner/executor_test.go:137:109: undefined: rpcBusy
```

The build fails: `Block`, `runnerStatus`, and `rpcBusy` do not exist yet.

**Step 3: Implement**

Create `runner/cmd/cagent-runner/executor.go`:

```go
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

// executor runs tool calls one at a time for the whole process, across reconnects. A call
// belongs to the connection it arrived on: when that connection ends, its queued calls are
// dropped, and a call already running finishes but answers only on its own connection. So
// a reconnect never lets an old call run beside a new one, and the platform can ask
// (status) whether anything is still running.
type executor struct {
	cfg      serveConfig
	d        deps
	instance string // one per process, unchanged across reconnects
	queue    chan job

	mu     sync.Mutex
	gen    uint64 // generation of the live connection; 0 while disconnected
	last   uint64
	active *activeCall
	queued map[string]int // cluster -> calls waiting
}

type job struct {
	gen     uint64
	conn    *websocket.Conn
	ctx     context.Context // the connection's context
	req     rpcRequest
	cluster string
}

type activeCall struct {
	CallID    json.RawMessage `json:"call_id"`
	Method    string          `json:"method"`
	Cluster   string          `json:"cluster"`
	StartedAt time.Time       `json:"started_at"`
}

// runnerStatus is what the status tool reports.
type runnerStatus struct {
	InstanceID string         `json:"instance_id"`
	Active     *activeCall    `json:"active"`
	Queued     map[string]int `json:"queued"`
}

func newExecutor(cfg serveConfig, d deps) *executor {
	size := cfg.queueSize
	if size <= 0 {
		size = 64
	}
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return &executor{cfg: cfg, d: d, instance: hex.EncodeToString(b),
		queue: make(chan job, size), queued: map[string]int{}}
}

// connect makes a new connection generation the live one.
func (e *executor) connect() uint64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.last++
	e.gen = e.last
	return e.gen
}

// disconnect ends a generation: none of its queued calls will start.
func (e *executor) disconnect(gen uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.gen == gen {
		e.gen = 0
	}
}

// enqueue queues a call; false means the queue is full.
func (e *executor) enqueue(j job) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	select {
	case e.queue <- j:
		e.queued[j.cluster]++
		return true
	default:
		return false
	}
}

func (e *executor) status() runnerStatus {
	e.mu.Lock()
	defer e.mu.Unlock()
	queued := make(map[string]int, len(e.queued))
	for c, n := range e.queued {
		queued[c] = n
	}
	var active *activeCall
	if e.active != nil {
		a := *e.active
		active = &a
	}
	return runnerStatus{InstanceID: e.instance, Active: active, Queued: queued}
}

// run works through the queue until ctx ends.
func (e *executor) run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case j := <-e.queue:
			e.do(j)
		}
	}
}

func (e *executor) do(j job) {
	// Checking the generation and marking the call active happen under the lock that
	// disconnect takes, so a call cannot pass the check and then start after its
	// connection ended.
	e.mu.Lock()
	if e.queued[j.cluster]--; e.queued[j.cluster] <= 0 {
		delete(e.queued, j.cluster)
	}
	if j.gen != e.gen {
		e.mu.Unlock()
		return
	}
	e.active = &activeCall{CallID: j.req.ID, Method: j.req.Method, Cluster: j.cluster, StartedAt: time.Now().UTC()}
	e.mu.Unlock()

	// A started call runs to completion even if its connection drops meanwhile: stopping
	// a subcommand halfway would leave the cluster in a state nobody asked for.
	resp := dispatch(context.Background(), e.cfg, e.d, j.req)

	e.mu.Lock()
	e.active = nil
	e.mu.Unlock()
	if err := wsjson.Write(j.ctx, j.conn, resp); err != nil {
		j.conn.CloseNow() // the result is lost; the hub's call times out or fails
	}
}

// clusterOf reads the cluster a call targets, "" when it names none (inspect).
func clusterOf(params json.RawMessage) string {
	var p struct {
		Name string `json:"name"`
	}
	_ = json.Unmarshal(params, &p)
	return p.Name
}
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
	rpcInvalidParams  = -32602
```

with:

```go
	rpcInvalidParams  = -32602
	rpcBusy           = -32000 // the call queue is full
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
	pingEvery    time.Duration
```

with:

```go
	pingEvery    time.Duration
	queueSize    int // calls waiting across all clusters; 0 means 64
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
	wait := cfg.backoff
	for {
		connected, err := serveOnce(ctx, cfg, d)
```

with:

```go
	ex := newExecutor(cfg, d)
	go ex.run(ctx)
	wait := cfg.backoff
	for {
		connected, err := serveOnce(ctx, cfg, ex)
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
// serveOnce runs one connection. The read loop never blocks on a tool: calls go to a
// single worker, so pings keep getting answered during a long build.
func serveOnce(ctx context.Context, cfg serveConfig, d deps) (bool, error) {
```

with:

```go
// serveOnce runs one connection. The read loop never blocks on a tool: calls go to the
// process's single worker, so pings and status keep getting answered during a long build.
func serveOnce(ctx context.Context, cfg serveConfig, ex *executor) (bool, error) {
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
	if err := wsjson.Write(connCtx, c, hello()); err != nil {
		return true, err
	}

	calls := make(chan rpcRequest, 64)
	defer close(calls)
	go func() {
		for req := range calls {
			if err := wsjson.Write(connCtx, c, dispatch(connCtx, cfg, d, req)); err != nil {
				cancel()
			}
		}
	}()
```

with:

```go
	if err := wsjson.Write(connCtx, c, hello(ex.instance)); err != nil {
		return true, err
	}
	gen := ex.connect()
	defer ex.disconnect(gen) // runs first: this connection's queued calls must not start
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
		calls <- req
	}
}

func hello() map[string]any {
	names := make([]string, 0, len(tools))
```

with:

```go
		if req.Method == "status" {
			if err := wsjson.Write(connCtx, c, statusResponse(ex, req)); err != nil {
				return true, err
			}
			continue
		}
		j := job{gen: gen, conn: c, ctx: connCtx, req: req, cluster: clusterOf(req.Params)}
		if !ex.enqueue(j) {
			busy := rpcResponse{JSONRPC: "2.0", ID: req.ID,
				Error: &rpcError{Code: rpcBusy, Message: "runner busy: call queue is full"}}
			if err := wsjson.Write(connCtx, c, busy); err != nil {
				return true, err
			}
		}
	}
}

// statusResponse answers status from the read loop, never the queue, so it works while a
// long call runs or the queue is full.
func statusResponse(ex *executor, req rpcRequest) rpcResponse {
	resp := rpcResponse{JSONRPC: "2.0", ID: req.ID}
	if err := decode(req.Params, &struct{}{}); err != nil {
		resp.Error = &rpcError{Code: rpcInvalidParams, Message: err.Error()}
		return resp
	}
	out, _ := json.Marshal(ex.status())
	resp.Result = &ToolResult{ExitCode: exitOK, Output: out}
	return resp
}

func hello(instance string) map[string]any {
	names := []string{"status"}
```

In `runner/cmd/cagent-runner/serve.go`, replace:

```go
	return map[string]any{"jsonrpc": "2.0", "method": "hello", "params": map[string]any{"tools": names}}
```

with:

```go
	return map[string]any{"jsonrpc": "2.0", "method": "hello",
		"params": map[string]any{"tools": names, "instance_id": instance}}
```

In `runner/internal/dockercli/fake.go`, replace:

```go
	Delay time.Duration // how long the command takes to answer
```

with:

```go
	Delay time.Duration // how long the command takes to answer
	// Block, when set, holds the command until the channel is closed. The call is recorded
	// first, so a test can wait for it to start (Seen) and then decide when it ends.
	Block <-chan struct{}
```

In `runner/internal/dockercli/fake.go`, replace:

```go
	return []byte(r.Out), r.Err
```

with:

```go
	if r.Block != nil {
		<-r.Block
	}
	return []byte(r.Out), r.Err
}

// Seen reports whether the command with these space-joined arguments has been called.
func (f *Fake) Seen(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.Calls {
		if c == key {
			return true
		}
	}
	return false
```

**Step 4: Run the tests**

Run:

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo golang:1.24 make runner-test
```

Expected:

```
ok  	github.com/yutaofr/container-agent/runner/cmd/cagent-runner
ok  	github.com/yutaofr/container-agent/runner/internal/builder
ok  	github.com/yutaofr/container-agent/runner/internal/cluster
ok  	github.com/yutaofr/container-agent/runner/internal/compose
ok  	github.com/yutaofr/container-agent/runner/internal/dockercli
ok  	github.com/yutaofr/container-agent/runner/internal/fingerprint
ok  	github.com/yutaofr/container-agent/runner/internal/guard
ok  	github.com/yutaofr/container-agent/runner/internal/initscripts
ok  	github.com/yutaofr/container-agent/runner/internal/inspect
ok  	github.com/yutaofr/container-agent/runner/internal/maven
ok  	github.com/yutaofr/container-agent/runner/internal/redact
ok  	github.com/yutaofr/container-agent/runner/internal/signals
```

Run:

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo golang:1.24 sh -c 'cd runner && go test -race -count=20 -run TestServe ./cmd/cagent-runner/'
```

Expected:

```
ok  	github.com/yutaofr/container-agent/runner/cmd/cagent-runner
```

**Step 5: Commit**

```bash
git add runner/cmd/cagent-runner/executor.go runner/cmd/cagent-runner/executor_test.go runner/cmd/cagent-runner/serve.go runner/internal/dockercli/fake.go
git commit -F - <<'EOF'
feat(runner): one serial worker per process, and a status tool

A reconnect used to start a new worker beside the old one, whose queue kept
running. Calls now belong to their connection's generation: queued calls of
a closed connection are dropped, a started call finishes and answers only on
its own connection, and status reports the instance, the active call, and
the queue from the read loop.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 2: Hub: instances, pinned calls, listeners, and `status`

The hub keeps each Runner's `instance_id` from `hello`. `call(..., instance=...)` checks the instance against the connection it is about to use and refuses with `InstanceChanged` before sending anything. Listeners hear every connect and disconnect; `status(hub, runner)` returns the Runner's report.

**Files:**

- Modify: `platform/src/cagent/hub.py`
- Test (modify): `platform/tests/test_hub.py`

**Step 1: Write the failing tests**

In `platform/tests/test_hub.py`, replace:

```python
from cagent.hub import RunnerError, RunnerHub, RunnerUnavailable, start
```

with:

```python
from cagent.hub import (
    InstanceChanged,
    RunnerError,
    RunnerHub,
    RunnerUnavailable,
    start,
    status,
)
```

In `platform/tests/test_hub.py`, replace:

```python
async def fake_runner(url: str, answer: Callable[[dict], dict | None], token: str = TOKEN):
    """Connect, say hello, and answer each request with answer(request); None means stay silent."""
    ws = await connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "method": "hello", "params": {"tools": ["validate"]}})
    )
```

with:

```python
async def fake_runner(
    url: str, answer: Callable[[dict], dict | None], token: str = TOKEN, instance: str = "i-1"
):
    """Connect, say hello, and answer each request with answer(request); None means stay silent."""
    ws = await connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    hello = {"tools": ["validate"], "instance_id": instance}
    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "hello", "params": hello}))
```

In `platform/tests/test_hub.py`, replace:

```python
async def connected(hub: RunnerHub, url: str, answer=echo) -> ClientConnection:
    ws, _ = await fake_runner(url, answer)
```

with:

```python
async def connected(hub: RunnerHub, url: str, answer=echo, instance="i-1") -> ClientConnection:
    ws, _ = await fake_runner(url, answer, instance=instance)
```

In `platform/tests/test_hub.py`, replace:

```python
    assert ws.close_reason == "expected hello"
    assert h.connected() == set()
```

with:

```python
    assert ws.close_reason == "expected hello"
    assert h.connected() == set()


async def test_hub_knows_each_runners_instance(hub):
    h, url = hub
    assert h.instance("laptop-1") is None
    ws = await connected(h, url, instance="i-1")
    assert h.instance("laptop-1") == "i-1"
    await ws.close()
    await connected(h, url, instance="i-2")
    assert h.instance("laptop-1") == "i-2"


async def test_a_pinned_call_goes_only_to_its_instance(hub):
    h, url = hub
    sent = []
    await connected(h, url, lambda req: sent.append(req) or echo(req), instance="i-2")
    with pytest.raises(InstanceChanged):
        await h.call("laptop-1", "up", {"name": "demo"}, instance="i-1")
    assert sent == []  # refused before anything was sent
    assert (await h.call("laptop-1", "up", {"name": "demo"}, instance="i-2")).ok


async def test_listeners_hear_connects_and_disconnects(hub):
    h, url = hub
    heard = []
    h.add_listener(lambda runner, up, instance: heard.append((runner, up, instance)))
    ws = await connected(h, url, instance="i-1")
    await ws.close()
    async with asyncio.timeout(5):
        while len(heard) < 2:
            await asyncio.sleep(0.01)
    assert heard == [("laptop-1", True, "i-1"), ("laptop-1", False, None)]


async def test_status_returns_the_runners_report(hub):
    h, url = hub
    report = {"instance_id": "i-1", "active": None, "queued": {}}

    def answer(req):
        assert req["method"] == "status" and req["params"] == {}
        return {"result": {"exit_code": 0, "output": report}}

    await connected(h, url, answer)
    assert await status(h, "laptop-1") == report
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_hub.py -q
```

Expected:

```
1 error
```

Collection fails: `InstanceChanged` and `status` cannot be imported.

**Step 3: Implement**

In `platform/src/cagent/hub.py`, replace:

```python
from collections.abc import Mapping
```

with:

```python
from collections.abc import Callable, Mapping
```

In `platform/src/cagent/hub.py`, replace:

```python
    """The runner is not connected, or disconnected before answering."""
```

with:

```python
    """The runner is not connected, or disconnected before answering."""


class InstanceChanged(RunnerUnavailable):
    """The runner connected now is another process than the one a call was pinned to."""
```

In `platform/src/cagent/hub.py`, replace:

```python
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
```

with:

```python
    instance: str | None = None  # the runner process, unchanged across its reconnects
    pending: dict[int, asyncio.Future] = field(default_factory=dict)


# Told about every connection change: (runner id, connected, instance id).
Listener = Callable[[str, bool, str | None], None]
```

In `platform/src/cagent/hub.py`, replace:

```python
        self._changed = asyncio.Condition()
```

with:

```python
        self._changed = asyncio.Condition()
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> None:
        """Call listener(runner_id, connected, instance) whenever a runner connects or goes."""
        self._listeners.append(listener)

    def _tell(self, runner_id: str, runner: "_Runner | None") -> None:
        for listener in self._listeners:
            listener(runner_id, runner is not None, runner.instance if runner else None)
```

In `platform/src/cagent/hub.py`, replace:

```python
        runner = _Runner(ws, tools=list(first.get("params", {}).get("tools", [])))
```

with:

```python
        params = first.get("params") or {}
        runner = _Runner(
            ws, tools=list(params.get("tools", [])), instance=params.get("instance_id") or None
        )
```

In `platform/src/cagent/hub.py`, replace:

```python
            self._changed.notify_all()
        if old is not None:
```

with:

```python
            self._changed.notify_all()
        self._tell(runner_id, runner)
        if old is not None:
```

In `platform/src/cagent/hub.py`, replace:

```python
                if self._runners.get(runner_id) is runner:
                    del self._runners[runner_id]
                self._changed.notify_all()
```

with:

```python
                gone = self._runners.get(runner_id) is runner
                if gone:
                    del self._runners[runner_id]
                self._changed.notify_all()
            if gone:
                self._tell(runner_id, None)
```

In `platform/src/cagent/hub.py`, replace:

```python

    def tools(self, runner_id: str) -> list[str]:
```

with:

```python

    def instance(self, runner_id: str) -> str | None:
        """The connected runner's process instance, or None when it is not connected."""
        runner = self._runners.get(runner_id)
        return runner.instance if runner else None

    def tools(self, runner_id: str) -> list[str]:
```

In `platform/src/cagent/hub.py`, replace:

```python
        self, runner_id: str, method: str, params: Mapping[str, Any], timeout: float | None = None
    ) -> ToolResult:
        """Call one tool on a runner and return its result."""
        runner = self._runners.get(runner_id)
        if runner is None:
            raise RunnerUnavailable(f"{runner_id} is not connected")
```

with:

```python
        self,
        runner_id: str,
        method: str,
        params: Mapping[str, Any],
        timeout: float | None = None,
        instance: str | None = None,
    ) -> ToolResult:
        """Call one tool on a runner and return its result. With instance, the call is sent
        only if the connected runner is that process, and on the connection just checked."""
        runner = self._runners.get(runner_id)
        if runner is None:
            raise RunnerUnavailable(f"{runner_id} is not connected")
        if instance is not None and runner.instance != instance:
            raise InstanceChanged(f"{runner_id} is now instance {runner.instance}, not {instance}")
```

In `platform/src/cagent/hub.py`, replace:

```python

async def start(hub: RunnerHub, host: str, port: int) -> Server:
```

with:

```python

async def status(hub: RunnerHub, runner_id: str) -> dict[str, Any]:
    """The runner's own report: {instance_id, active, queued}. It answers even while a long
    call runs, because the runner handles status outside its call queue."""
    res = await hub.call(runner_id, "status", {})
    if not res.ok or not isinstance(res.output, dict):
        raise RunnerError(0, f"status failed: {res.stderr}")
    return res.output


async def start(hub: RunnerHub, host: str, port: int) -> Server:
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run pytest tests/test_hub.py -q
```

Expected:

```
13 passed
```

**Step 5: Commit**

```bash
git add platform/src/cagent/hub.py platform/tests/test_hub.py
git commit -F - <<'EOF'
feat(platform): the hub tracks runner instances and pins calls to one

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 3: Dependencies and the SQLite store

Add the web and SQLite dependencies, then `cagent.store`: the tables of design section 7, a write transaction (`BEGIN IMMEDIATE`, one connection, one lock) and a read transaction, an event log numbered by a persisted counter and trimmed to the newest events, page reads that report a cursor outside the kept events, payload caps, and a notifier whose generation makes a wake-up between read and wait impossible to miss.

**Files:**

- Create: `platform/src/cagent/store.py`
- Test (create): `platform/tests/test_store.py`
- Modify (through `uv add`): `platform/pyproject.toml`, `platform/uv.lock`

**Step 1: Write the failing tests**

Create `platform/tests/test_store.py`:

```python
"""The store: transactions, the bounded event log, and wake-ups after commits."""

import asyncio

import pytest

from cagent.store import Store, bounded


async def new_run(store: Store, run_id: str = "r1") -> None:
    async with store.tx() as t:
        await t.insert(
            "runs",
            id=run_id,
            owner="alice",
            runner="laptop-1",
            cluster="demo",
            input={"purpose": "x"},
            created_at=0.0,
            state="running",
        )


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"), keep_events=3)
    yield s
    await s.close()


async def test_a_run_survives_reopening(tmp_path):
    path = str(tmp_path / "cagent.db")
    s = await Store.open(path)
    await new_run(s)
    await s.close()
    s = await Store.open(path)
    async with s.read() as t:
        run = await t.get_run("r1")
    await s.close()
    assert run["input"] == {"purpose": "x"} and run["event_seq"] == 0
    assert run["min_event_seq"] == 1


async def test_a_failed_transaction_leaves_nothing(store):
    await new_run(store)
    with pytest.raises(RuntimeError):
        async with store.tx() as t:
            await t.update_run("r1", state="waiting")
            await t.append_event("r1", "run_state", {"state": "waiting"})
            raise RuntimeError("crash before commit")
    async with store.read() as t:
        run = await t.get_run("r1")
    assert run["state"] == "running" and run["event_seq"] == 0
    assert await store.events_after("r1", 0, 10, 10_000) == []


async def test_events_are_numbered_by_a_counter_and_trimmed(store):
    await new_run(store)
    for i in range(5):
        async with store.tx() as t:
            assert await t.append_event("r1", "phase_started", {"phase": f"p{i}"}) == i + 1
    async with store.read() as t:
        run = await t.get_run("r1")
    assert (run["event_seq"], run["min_event_seq"]) == (5, 3)
    page = await store.events_after("r1", 2, 10, 10_000)
    assert [e["seq"] for e in page] == [3, 4, 5]
    assert page[0]["payload"] == {"phase": "p2"}


async def test_a_cursor_outside_the_kept_events_must_resync(store):
    await new_run(store)
    for _ in range(5):
        async with store.tx() as t:
            await t.append_event("r1", "phase_started", {"phase": "p"})
    assert await store.events_after("r1", 1, 10, 10_000) is None  # 2 was trimmed
    assert await store.events_after("r1", 6, 10, 10_000) is None  # past the last event
    assert await store.events_after("r1", 5, 10, 10_000) == []
    assert await store.events_after("missing", 0, 10, 10_000) is None


async def test_a_page_stops_at_its_byte_limit(store):
    await new_run(store)
    for _ in range(3):
        async with store.tx() as t:
            await t.append_event("r1", "observation", {"line": "x" * 100})
    page = await store.events_after("r1", 0, 10, 150)
    assert [e["seq"] for e in page] == [1]  # one event always fits


def test_payloads_are_bounded_and_marked():
    long = bounded({"tail": ["y" * 5000]}, 64 * 1024)
    assert long["tail"][0].endswith("…[truncated]") and len(long["tail"][0]) < 2100
    assert bounded({"lines": ["z" * 1000] * 100}, 1000) == {"truncated": True, "bytes": 100_411}


async def test_subscribers_wake_only_after_the_commit(store):
    await new_run(store)
    seen = store.notifier.generation("r1")
    waiter = asyncio.create_task(store.notifier.wait("r1", seen, timeout=5))
    async with store.tx() as t:
        await t.append_event("r1", "run_state", {"state": "waiting"})
        await asyncio.sleep(0.05)
        assert not waiter.done()  # not before the commit
    assert await waiter


async def test_a_wake_up_between_read_and_wait_is_not_missed(store):
    await new_run(store)
    seen = store.notifier.generation("r1")  # taken before reading
    async with store.tx() as t:
        await t.append_event("r1", "run_state", {"state": "waiting"})
    assert await store.notifier.wait("r1", seen, timeout=0.01)  # returns at once
    assert not await store.notifier.wait("r1", store.notifier.generation("r1"), timeout=0.01)
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_store.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.store`.

**Step 3: Add the dependencies**

Run:

```bash
cd platform && uv add "aiosqlite>=0.22.1" "fastapi>=0.146.0" "langgraph-checkpoint-sqlite>=3.1.2" "uvicorn>=0.54.0" && uv add --dev "httpx>=0.28.1"
```

This adds the five entries to `platform/pyproject.toml` and updates `platform/uv.lock`. Do not edit either file by hand.

**Step 4: Implement**

Create `platform/src/cagent/store.py`:

```python
"""Durable records for runs: the registry the web reads, commands, approvals, recovery plans,
cluster ownership and marks, tool dispatches, Runner status, and a bounded event log.

One SQLite file holds these tables and LangGraph's checkpoints, but the two are written
through different connections and never in one transaction: nothing here may assume a
checkpoint and a registry change commit together. All of this module's writes go through one
connection, serialized by a lock, in short transactions that never wait on anything outside
the database.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    runner TEXT NOT NULL,
    cluster TEXT NOT NULL,
    input TEXT NOT NULL,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    thread_id TEXT,
    executions INTEGER NOT NULL DEFAULT 0,
    execution_id TEXT,
    execution_instance TEXT,
    phase TEXT,
    pending TEXT,
    reconcile TEXT,
    last_check TEXT,
    recovery TEXT,
    last_observation TEXT,
    last_observation_attempt TEXT,
    verified_at REAL,
    outcome TEXT,
    event_seq INTEGER NOT NULL DEFAULT 0,
    min_event_seq INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS commands (
    user TEXT NOT NULL,
    command_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    class TEXT NOT NULL,
    digest TEXT NOT NULL,
    response TEXT NOT NULL,
    status TEXT NOT NULL,
    effect TEXT,
    reason TEXT,
    execution_id TEXT,
    at REAL NOT NULL,
    PRIMARY KEY (user, command_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    pending_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    content TEXT NOT NULL,
    decision TEXT NOT NULL,
    user TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    digest TEXT NOT NULL,
    based_on_observation_seq INTEGER NOT NULL,
    facts TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    invalidated_at REAL
);
CREATE TABLE IF NOT EXISTS clusters (
    runner TEXT NOT NULL,
    name TEXT NOT NULL,
    owner TEXT NOT NULL,
    lock_run_id TEXT,
    marks TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (runner, name)
);
CREATE TABLE IF NOT EXISTS dispatches (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    method TEXT NOT NULL,
    instance TEXT,
    sent_at REAL NOT NULL,
    result_at REAL,
    exit_code INTEGER
);
CREATE TABLE IF NOT EXISTS runner_status (
    runner TEXT PRIMARY KEY,
    online INTEGER NOT NULL,
    instance_id TEXT,
    changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    execution_id TEXT,
    task_id TEXT,
    type TEXT NOT NULL,
    at REAL NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

# Columns holding JSON, decoded on read.
_JSON = {
    "input",
    "pending",
    "reconcile",
    "last_check",
    "recovery",
    "last_observation",
    "last_observation_attempt",
    "outcome",
    "marks",
    "response",
    "facts",
    "content",
    "decision",
    "payload",
}

# Runs in these states hold their cluster's execution lock; the rest are final.
ACTIVE_STATES = ("running", "waiting", "reconcile")

MAX_STRING = 2000  # characters kept per string in an event payload


def _decode(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: json.loads(row[k]) if k in _JSON and row[k] is not None else row[k] for k in row.keys()
    }


def _encode(column: str, value: Any) -> Any:
    return json.dumps(value) if column in _JSON and value is not None else value


def bounded(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    """Cap an event payload: long strings are cut and marked; a payload still over limit
    bytes is replaced by a marker, so a single log line cannot grow the event log."""

    def cap(v: Any) -> Any:
        if isinstance(v, str) and len(v) > MAX_STRING:
            return v[:MAX_STRING] + "…[truncated]"
        if isinstance(v, list):
            return [cap(x) for x in v]
        if isinstance(v, dict):
            return {k: cap(x) for k, x in v.items()}
        return v

    capped = cap(payload)
    size = len(json.dumps(capped))
    if size > limit:
        return {"truncated": True, "bytes": size}
    return capped


class Notifier:
    """Wakes event subscribers after a commit. Subscribers read events from the database; a
    wake-up is only a hint, so a missed one costs at most a heartbeat, never an event.

    A subscriber takes generation(run) before reading, and wait() returns at once if the
    generation moved since, so a commit between "found nothing" and "start waiting" is not
    missed."""

    def __init__(self) -> None:
        self._gen: dict[str, int] = {}
        self._cond = asyncio.Condition()

    def generation(self, run_id: str) -> int:
        return self._gen.get(run_id, 0)

    async def bump(self, run_ids: Iterable[str]) -> None:
        async with self._cond:
            for r in run_ids:
                self._gen[r] = self._gen.get(r, 0) + 1
            self._cond.notify_all()

    async def wait(self, run_id: str, seen: int, timeout: float) -> bool:
        """Wait until run_id's generation passes seen; False on timeout."""
        try:
            async with asyncio.timeout(timeout), self._cond:
                await self._cond.wait_for(lambda: self.generation(run_id) != seen)
            return True
        except TimeoutError:
            return False


class Tx:
    """One open transaction, with helpers for the tables above."""

    def __init__(self, store: "Store", db: aiosqlite.Connection):
        self.store, self.db = store, db
        self.touched: set[str] = set()  # runs with new events, woken after commit

    async def one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        async with self.db.execute(sql, args) as cur:
            return _decode(await cur.fetchone())

    async def all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async with self.db.execute(sql, args) as cur:
            return [_decode(r) for r in await cur.fetchall()]

    async def run(self, sql: str, *args: Any) -> int:
        """Execute a statement; return the number of rows it changed."""
        async with self.db.execute(sql, args) as cur:
            return cur.rowcount

    async def insert(self, table: str, **values: Any) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        args = [_encode(c, v) for c, v in values.items()]
        await self.run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *args)

    async def update(self, table: str, where: dict[str, Any], **values: Any) -> int:
        sets = ", ".join(f"{c} = ?" for c in values)
        cond = " AND ".join(f"{c} = ?" for c in where)
        args = [_encode(c, v) for c, v in values.items()] + list(where.values())
        return await self.run(f"UPDATE {table} SET {sets} WHERE {cond}", *args)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self.one("SELECT * FROM runs WHERE id = ?", run_id)

    async def update_run(self, run_id: str, **values: Any) -> None:
        await self.update("runs", {"id": run_id}, **values)

    async def bump_revision(self, run_id: str) -> int:
        row = await self.one(
            "UPDATE runs SET revision = revision + 1 WHERE id = ? RETURNING revision", run_id
        )
        return row["revision"]

    async def append_event(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any],
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        """Append one event under the run's persisted counter and trim the run's log to its
        newest keep_events. Numbering never depends on which rows are still kept."""
        row = await self.one(
            "UPDATE runs SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq", run_id
        )
        seq = row["event_seq"]
        await self.insert(
            "events",
            run_id=run_id,
            seq=seq,
            execution_id=execution_id,
            task_id=task_id,
            type=type,
            at=self.store.clock(),
            payload=bounded(payload, self.store.max_payload),
        )
        floor = seq - self.store.keep_events + 1
        if floor > 1:
            await self.run("DELETE FROM events WHERE run_id = ? AND seq < ?", run_id, floor)
            await self.run(
                "UPDATE runs SET min_event_seq = MAX(min_event_seq, ?) WHERE id = ?", floor, run_id
            )
        self.touched.add(run_id)
        return seq

    async def get_cluster(self, runner: str, name: str) -> dict[str, Any] | None:
        return await self.one("SELECT * FROM clusters WHERE runner = ? AND name = ?", runner, name)


class Store:
    def __init__(
        self,
        db: aiosqlite.Connection,
        keep_events: int = 1000,
        max_payload: int = 64 * 1024,
        clock: Callable[[], float] = time.time,
    ):
        self.db, self.keep_events, self.max_payload, self.clock = (
            db,
            keep_events,
            max_payload,
            clock,
        )
        self.notifier = Notifier()
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str, **kwargs: Any) -> "Store":
        db = await aiosqlite.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(SCHEMA)
        return cls(db, **kwargs)

    async def close(self) -> None:
        await self.db.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        """A write transaction (BEGIN IMMEDIATE). It commits on exit and rolls back on an
        exception; subscribers of runs that got events are woken only after the commit."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            t = Tx(self, self.db)
            try:
                yield t
            except BaseException:
                await self.db.execute("ROLLBACK")
                raise
            await self.db.execute("COMMIT")
        if t.touched:
            await self.notifier.bump(t.touched)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[Tx]:
        """A read transaction: everything read inside it is one consistent view."""
        async with self._lock:
            await self.db.execute("BEGIN")
            try:
                yield Tx(self, self.db)
            finally:
                await self.db.execute("ROLLBACK")

    async def events_after(
        self, run_id: str, after: int, limit: int, max_bytes: int
    ) -> list[dict[str, Any]] | None:
        """The next page of events with seq > after, in order, or None when the cursor is
        outside what is kept (trimmed away, or past the last event): the reader must resync.
        Each page is its own short read, so the boundary is checked again for every page."""
        async with self.read() as t:
            run = await t.one("SELECT event_seq, min_event_seq FROM runs WHERE id = ?", run_id)
            if run is None or after < run["min_event_seq"] - 1 or after > run["event_seq"]:
                return None
            rows = await t.all(
                "SELECT seq, execution_id, task_id, type, at, payload FROM events"
                " WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                run_id,
                after,
                limit,
            )
        page, size = [], 0
        for r in rows:
            size += len(json.dumps(r["payload"]))
            if page and size > max_bytes:
                break
            page.append(r)
        return page
```

**Step 5: Run the tests**

Run:

```bash
cd platform && uv run pytest tests/test_store.py -q
```

Expected:

```
8 passed
```

**Step 6: Commit**

```bash
git add platform/pyproject.toml platform/src/cagent/store.py platform/tests/test_store.py platform/uv.lock
git commit -F - <<'EOF'
feat(platform): a SQLite store for runs, commands, and a bounded event log

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 3 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 4: Dispatch log and instance pinning

`RecordingCaller` is the orchestrator's ToolCaller. Inside an execution (a `ContextVar`) it writes each call to `dispatches` before sending it and its result after, and pins the execution to one Runner instance. A refused call (instance changed) was never sent, so its row is removed. `unconfirmed()` lists mutating calls sent without a result: the evidence for cluster marks.

**Files:**

- Create: `platform/src/cagent/dispatch.py`
- Test (create): `platform/tests/test_dispatch.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_dispatch.py`:

```python
"""The dispatch log: written before a call is sent, completed after, pinned to one Runner."""

import asyncio

import pytest

from cagent.dispatch import Execution, RecordingCaller, current, unconfirmed
from cagent.hub import InstanceChanged, RunnerUnavailable, ToolResult
from cagent.store import Store

from .test_store import new_run


class FakeHub:
    """Answers calls; reports which instance is connected; can hang or refuse."""

    def __init__(self, store: Store, instance: str | None = "i-1"):
        self.store, self.connected = store, instance
        self.hang = False
        self.sent: list[tuple[str, str | None]] = []
        self.rows_at_send: list[dict] = []

    def instance(self, runner_id: str) -> str | None:
        return self.connected

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        if instance is not None and instance != self.connected:
            raise InstanceChanged("other process")
        async with self.store.read() as t:
            self.rows_at_send = await t.all("SELECT * FROM dispatches")
        self.sent.append((method, instance))
        if self.hang:
            await asyncio.sleep(3600)
        return ToolResult(0, {"ok": True})


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    await new_run(s)
    async with s.tx() as t:
        await t.update_run("r1", execution_id="e1")
    yield s
    await s.close()


def within(ex: Execution):
    current.set(ex)


async def test_a_call_is_logged_before_it_is_sent_and_completed_after(store):
    hub = FakeHub(store)
    within(Execution("r1", "e1", "i-1"))
    res = await RecordingCaller(hub, store).call("laptop-1", "up", {"name": "demo"})
    assert res.ok and hub.sent == [("up", "i-1")]
    [at_send] = hub.rows_at_send
    assert at_send["method"] == "up" and at_send["result_at"] is None
    assert await unconfirmed(store, "r1") == []


async def test_a_call_without_an_answer_stays_unconfirmed(store):
    hub = FakeHub(store)
    hub.hang = True
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await RecordingCaller(hub, store).call("laptop-1", "build", {})
    assert [d["method"] for d in await unconfirmed(store, "r1")] == ["build"]


async def test_reads_never_count_as_unconfirmed(store):
    hub = FakeHub(store)
    hub.hang = True
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await RecordingCaller(hub, store).call("laptop-1", "snapshot", {})
    assert await unconfirmed(store, "r1") == []


async def test_a_call_pinned_to_another_instance_is_refused_and_not_logged(store):
    hub = FakeHub(store, instance="i-2")
    within(Execution("r1", "e1", "i-1"))
    with pytest.raises(InstanceChanged):
        await RecordingCaller(hub, store).call("laptop-1", "up", {})
    assert hub.sent == []
    async with store.read() as t:
        assert await t.all("SELECT * FROM dispatches") == []


async def test_an_unpinned_execution_pins_the_first_instance(store):
    hub = FakeHub(store, instance="i-7")
    ex = Execution("r1", "e1", None)
    within(ex)
    await RecordingCaller(hub, store).call("laptop-1", "validate", {})
    assert ex.instance == "i-7" and hub.sent == [("validate", "i-7")]
    async with store.read() as t:
        assert (await t.get_run("r1"))["execution_instance"] == "i-7"


async def test_an_unpinned_execution_needs_a_connected_runner(store):
    within(Execution("r1", "e1", None))
    with pytest.raises(RunnerUnavailable):
        await RecordingCaller(FakeHub(store, instance=None), store).call("laptop-1", "up", {})


async def test_calls_outside_an_execution_pass_straight_through(store):
    hub = FakeHub(store)
    current.set(None)
    assert (await RecordingCaller(hub, store).call("laptop-1", "status", {})).ok
    assert hub.sent == [("status", None)]
    async with store.read() as t:
        assert await t.all("SELECT * FROM dispatches") == []
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_dispatch.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.dispatch`.

**Step 3: Implement**

Create `platform/src/cagent/dispatch.py`:

```python
"""Every tool call an execution makes is written down before it is sent.

After a crash the platform cannot ask a half-finished execution what it did. The dispatch
log answers instead: a mutating call that was sent but has no recorded result may have
changed the cluster, so the cluster needs reconciling; a cluster with no such call does not.
The log also pins each execution to one Runner process, so a call is never sent to a Runner
that restarted in between.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from cagent.hub import InstanceChanged, RunnerHub, RunnerUnavailable, ToolResult
from cagent.store import Store

# Calls that may change the user's machine. inspect, snapshot, verify, and status only read.
MUTATING = frozenset({"validate", "build", "up", "down"})


@dataclass
class Execution:
    """The execution the current task belongs to."""

    run_id: str
    execution_id: str
    instance: str | None  # the Runner process its calls go to; None pins the first one seen


current: ContextVar[Execution | None] = ContextVar("execution", default=None)


class RecordingCaller:
    """The orchestrator's ToolCaller: the hub, with a dispatch log and instance pinning."""

    def __init__(self, hub: RunnerHub, store: Store):
        self.hub, self.store = hub, store

    async def call(
        self, runner_id: str, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> ToolResult:
        ex = current.get()
        if ex is None:
            return await self.hub.call(runner_id, method, params, timeout)
        if ex.instance is None:
            ex.instance = self.hub.instance(runner_id)
            if ex.instance is None:
                raise RunnerUnavailable(f"{runner_id} is not connected")
            async with self.store.tx() as t:
                await t.update(
                    "runs",
                    {"id": ex.run_id, "execution_id": ex.execution_id},
                    execution_instance=ex.instance,
                )
        async with self.store.tx() as t:
            row = await t.one(
                "INSERT INTO dispatches (run_id, execution_id, method, instance, sent_at)"
                " VALUES (?, ?, ?, ?, ?) RETURNING call_id",
                ex.run_id,
                ex.execution_id,
                method,
                ex.instance,
                self.store.clock(),
            )
        call_id = row["call_id"]
        try:
            res = await self.hub.call(runner_id, method, params, timeout, instance=ex.instance)
        except InstanceChanged:
            # The hub refuses before sending anything, so nothing was dispatched.
            async with self.store.tx() as t:
                await t.run("DELETE FROM dispatches WHERE call_id = ?", call_id)
            raise
        async with self.store.tx() as t:
            await t.update(
                "dispatches",
                {"call_id": call_id},
                result_at=self.store.clock(),
                exit_code=res.exit_code,
            )
        return res


async def unconfirmed(store: Store, run_id: str) -> list[dict[str, Any]]:
    """Mutating calls of run_id that were sent and never got a recorded result."""
    marks = ", ".join("?" for _ in MUTATING)
    async with store.read() as t:
        return await t.all(
            "SELECT * FROM dispatches WHERE run_id = ? AND result_at IS NULL"
            f" AND method IN ({marks}) ORDER BY call_id",
            run_id,
            *sorted(MUTATING),
        )
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run pytest tests/test_dispatch.py -q
```

Expected:

```
7 passed
```

**Step 5: Commit**

```bash
git add platform/src/cagent/dispatch.py platform/tests/test_dispatch.py
git commit -F - <<'EOF'
feat(platform): log every tool call before sending it, pinned to one runner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 5: Orchestrator: the user names the cluster; `allow` is the full set

`start_from_purpose(..., cluster=...)` sets both `State.cluster` and the spec's name. `validate` always calls the Runner with an empty `allow` and works out which findings the current `allow` leaves uncovered; an approval shows every finding plus the current `allow`, and the decision's `allow` replaces the old set. `SimCluster` records each request's params so tests can check that.

**Files:**

- Modify: `platform/src/cagent/orchestrator.py`
- Test (modify): `platform/tests/simcluster.py`
- Test (modify): `platform/tests/test_orchestrator.py`
- Test (modify): `platform/tests/test_orchestrator_clarify.py`

**Step 1: Write the failing tests**

In `platform/tests/simcluster.py`, replace:

```python
        self.calls: list[str] = []
```

with:

```python
        self.calls: list[str] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []  # (method, params) per call
```

In `platform/tests/simcluster.py`, replace:

```python
        self.calls.append(method)
```

with:

```python
        self.calls.append(method)
        self.requests.append((method, params))
```

In `platform/tests/test_orchestrator.py`, replace:

```python

async def test_watch_waits_for_starting_healthchecks(catalog):
```

with:

```python

async def test_an_approval_lists_every_finding_and_takes_the_full_allow_set(catalog):
    sim = SimCluster(
        findings=[
            {"service": "mysql", "rule": "field:sysctls", "detail": "x"},
            {"service": "redis", "rule": "cap-add", "detail": "y"},
        ]
    )
    o = orchestrator(sim, catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    assert len(status.waiting["findings"]) == 2 and status.waiting["allow"] == []

    status = await approve(o, "t1", allow=["mysql:field:sysctls"])
    # redis is still uncovered: the approval comes back, again with every finding
    assert len(status.waiting["findings"]) == 2
    assert status.waiting["allow"] == ["mysql:field:sysctls"]

    # the new allow replaces the old one instead of adding to it
    status = await approve(o, "t1", allow=["redis:cap-add"])
    assert status.waiting["allow"] == ["redis:cap-add"]

    both = ["mysql:field:sysctls", "redis:cap-add"]
    assert (await approve(o, "t1", allow=both)).outcome == "healthy"
    assert all(p["allow"] == [] for m, p in sim.requests if m == "validate")
    assert [p["allow"] for m, p in sim.requests if m == "up"] == [both]


async def test_watch_waits_for_starting_healthchecks(catalog):
```

In `platform/tests/test_orchestrator_clarify.py`, replace:

```python
    assert sim.builds[0]["maven"] == "com.acme:order-service:1.4.2:jar"
```

with:

```python
    assert sim.builds[0]["maven"] == "com.acme:order-service:1.4.2:jar"


async def test_the_user_names_the_cluster(catalog):
    sim = SimCluster()
    script = {"fake/large": [extraction([], [{"template": "redis"}])]}
    o = orchestrator(sim, catalog, script)
    status = await o.start_from_purpose("t1", "a redis", "laptop-1", cluster="my-cache")
    assert status.waiting["kind"] == "approval"
    assert [p["name"] for m, p in sim.requests] == ["my-cache"]
    snap = await o.graph.aget_state(o._config("t1"))
    assert snap.values["cluster"] == snap.values["spec"]["name"] == "my-cache"
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_orchestrator.py tests/test_orchestrator_clarify.py -q
```

Expected:

```
2 failed, 23 passed
```

The two new tests fail.

**Step 3: Implement**

In `platform/src/cagent/orchestrator.py`, replace:

```python
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
```

with:

```python
    async def start_from_purpose(
        self, thread_id: str, purpose: str, runner: str, cluster: str | None = None
    ) -> Status:
        """Start from a sentence: a model drafts the spec, then questions close its gaps.
        With cluster, the spec takes that name instead of the one the model picks."""
        if self.router is None:
            raise ValueError("starting from a purpose needs a model router")
        initial = self._initial(runner, purpose=purpose, cluster=cluster)
        await self.graph.ainvoke(initial, self._config(thread_id))
        return await self.status(thread_id)

    def _initial(
        self,
        runner: str,
        spec: ClusterSpec | None = None,
        purpose: str | None = None,
        cluster: str | None = None,
    ) -> State:
        return {
            "cluster": cluster or (spec.name if spec else ""),
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
            return self._escalation("model_unavailable", step="extract", detail=str(e))
```

with:

```python
            return self._escalation("model_unavailable", step="extract", detail=str(e))
        if state["cluster"]:  # the user named the cluster; State and spec must agree
            spec = ClusterSpec.model_validate({**spec.model_dump(), "name": state["cluster"]})
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        res = await self._call(
            state, "validate", compose=to_yaml(state["compose"]), allow=state["allow"]
        )
```

with:

```python
        # Validate without approvals, so every finding is known; the user's allow is the full
        # set of exceptions for this candidate, and only uncovered findings need approval.
        res = await self._call(state, "validate", compose=to_yaml(state["compose"]), allow=[])
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        if res.output.get("findings"):
            return Command(
                update={**checked, "findings": res.output["findings"], "approved": False},
                goto="approve",
            )
        if not state["approved"]:
            return Command(update={**checked, "findings": []}, goto="approve")
```

with:

```python
        findings = res.output.get("findings") or []
        allowed = set(state["allow"])
        if any(f"{f['service']}:{f['rule']}" not in allowed for f in findings):
            return Command(
                update={**checked, "findings": findings, "approved": False}, goto="approve"
            )
        if not state["approved"]:
            return Command(update={**checked, "findings": findings}, goto="approve")
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
                "findings": state["findings"],
```

with:

```python
                "findings": state["findings"],
                "allow": state["allow"],
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        return Command(
            update={"approved": True, "allow": decision.get("allow", state["allow"])},
```

with:

```python
        # The decision's allow replaces the old one: it is the complete set for this candidate.
        return Command(
            update={"approved": True, "allow": list(decision.get("allow") or [])},
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
323 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/orchestrator.py platform/tests/simcluster.py platform/tests/test_orchestrator.py platform/tests/test_orchestrator_clarify.py
git commit -F - <<'EOF'
feat(platform): users name the cluster, and an approval's allow is the full set

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 6: Orchestrator: phase events and observations

`start`, `resume`, and `start_from_purpose` drive the graph with `astream(stream_mode=["tasks", "custom"], version="v2")` and map raw task events to whitelisted application events through an optional `on_event` sink. A node that raises ends the stream without a task result, so `_drive` reports the started tasks as failed before re-raising. `_snapshot` emits `observation` only for a successful, well-formed snapshot, stamped when it arrived, and `observation_failed` otherwise. `Status` gains the interrupt id.

**Files:**

- Modify: `platform/src/cagent/orchestrator.py`
- Test (create): `platform/tests/test_orchestrator_events.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_orchestrator_events.py`:

```python
"""Application events: whitelisted phase events and observations from the graph's streams."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cagent.hub import RunnerUnavailable, ToolResult
from cagent.orchestrator import Orchestrator, Settings

from .conftest import spec_with
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
MIDDLEWARE_ONLY = {"services": [], "middleware": [{"template": "redis"}]}
PHASE_KEYS = {"phase", "pending_kind", "reason"}


async def no_sleep(_seconds: float) -> None:
    return None


def orchestrator(sim, catalog) -> Orchestrator:
    return Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep, clock=lambda: 42.0)


class Recorder:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)

    def phases(self) -> list[tuple[str, str]]:
        return [(e["type"], e["payload"]["phase"]) for e in self.events if "phase" in e["payload"]]


async def test_a_run_reports_phases_up_to_its_approval(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    status = await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    assert rec.phases() == [
        ("phase_started", "generate"),
        ("phase_completed", "generate"),
        ("phase_started", "validate"),
        ("phase_completed", "validate"),
        ("phase_started", "approve"),
        ("phase_interrupted", "approve"),
    ]
    assert rec.events[-1]["payload"]["pending_kind"] == "approval"
    snap = await o.graph.aget_state(o._config("t1"))
    assert status.interrupt_id == snap.interrupts[0].id


async def test_events_carry_no_raw_task_data(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    await o.resume("t1", {"approve": True}, on_event=rec)
    for e in rec.events:
        assert set(e) == {"type", "task_id", "payload"}
        if e["type"].startswith("phase_"):
            assert set(e["payload"]) <= PHASE_KEYS
        else:
            assert set(e["payload"]) in ({"received_at", "services"}, {"received_at", "reason"})


async def test_watching_reports_each_snapshot_as_an_observation(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(starting_snapshots=1), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    status = await o.resume("t1", {"approve": True}, on_event=rec)
    assert status.outcome == "healthy"
    observations = [e["payload"] for e in rec.events if e["type"] == "observation"]
    assert len(observations) == 3  # starting, settled, after the stability window
    assert observations[0]["received_at"] == 42.0
    assert observations[0]["services"][0] == {
        "service": "redis",
        "role": None,
        "state": "running",
        "health": "starting",
        "restart_count": 0,
        "exit_code": 0,
        "errors": [],
        "tail": [],
    }
    assert ("phase_completed", "done") in rec.phases()


async def test_a_failed_snapshot_is_reported_not_shown_as_empty(catalog):
    class Flaky(SimCluster):
        failed = False

        def _snapshot(self, p):
            if not self.failed:
                self.failed = True
                return ToolResult(2, None, "docker: cannot connect")
            return super()._snapshot(p)

    rec = Recorder()
    o = orchestrator(Flaky(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    await o.resume("t1", {"approve": True}, on_event=rec)
    first = next(e for e in rec.events if e["type"].startswith("observation"))
    assert first["type"] == "observation_failed"
    assert first["payload"]["reason"] == "snapshot exited 2: docker: cannot connect"


async def test_a_node_error_reports_the_phase_failed_and_propagates(catalog):
    class Gone(SimCluster):
        def _up(self, p):
            raise RunnerUnavailable("laptop-1 disconnected")

    rec = Recorder()
    o = orchestrator(Gone(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1")
    with pytest.raises(RunnerUnavailable):
        await o.resume("t1", {"approve": True}, on_event=rec)
    assert rec.events[-1] == {
        "type": "phase_failed",
        "task_id": rec.events[-1]["task_id"],
        "payload": {"phase": "up", "reason": "RunnerUnavailable"},
    }


async def test_a_resumed_node_starts_again_with_the_same_task_id(catalog):
    rec = Recorder()
    o = orchestrator(SimCluster(), catalog)
    await o.start("t1", spec_with(**MIDDLEWARE_ONLY), "laptop-1", on_event=rec)
    interrupted, n = rec.events[-1], len(rec.events)
    await o.resume("t1", {"approve": True}, on_event=rec)
    restarted = rec.events[n]
    assert restarted["type"] == "phase_started" and restarted["payload"]["phase"] == "approve"
    assert restarted["task_id"] == interrupted["task_id"]
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_orchestrator_events.py -q
```

Expected:

```
6 failed
```

The new tests fail: `start` takes no `on_event`.

**Step 3: Implement**

In `platform/src/cagent/orchestrator.py`, replace:

```python
they do nothing before interrupting, because a node that called interrupt() runs again from
the top when resumed. All cluster work goes through the Runner's tools, so the orchestrator
never touches Docker.
```

with:

```python
they have no side effects before interrupting, because a node that called interrupt() runs
again from the top when resumed. All cluster work goes through the Runner's tools, so the
orchestrator never touches Docker.

Runs report progress as application events (phase_started, phase_completed,
phase_interrupted, phase_failed, observation, observation_failed) through an optional
callback. They are built from LangGraph's task and custom streams with whitelisted fields:
raw task events carry node inputs and results, which may hold the whole state.
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
from jsonpointer import JsonPointerException
```

with:

```python
from jsonpointer import JsonPointerException
from langgraph.config import get_stream_writer
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
    fixes: list[dict[str, Any]]

```

with:

```python
    fixes: list[dict[str, Any]]
    interrupt_id: str | None = None  # LangGraph's id for the interrupt behind waiting


# Receives each application event: {"type", "task_id", "payload"}.
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


def _service_summary(s: dict[str, Any]) -> dict[str, Any]:
    """What an observation shows of one service, from the Runner's redacted snapshot."""
    return {
        "service": s.get("service"),
        "role": s.get("role"),
        "state": s.get("state"),
        "health": s.get("health"),
        "restart_count": s.get("restart_count", 0),
        "exit_code": s.get("exit_code"),
        "errors": list(s.get("error_lines") or []),
        "tail": list(s.get("tail") or []),
    }


def _app_events(part: dict[str, Any], started: dict[str, str]) -> list[dict[str, Any]]:
    """Map one stream part to application events. started tracks running tasks by id."""
    data = part["data"]
    if part["type"] == "custom":
        if isinstance(data, dict) and data.get("type") == "observation":
            payload = {"received_at": data["received_at"], "services": data["services"]}
            return [{"type": "observation", "task_id": None, "payload": payload}]
        if isinstance(data, dict) and data.get("type") == "observation_failed":
            payload = {"received_at": data["received_at"], "reason": data["reason"]}
            return [{"type": "observation_failed", "task_id": None, "payload": payload}]
        return []
    task_id, phase = data["id"], data["name"]
    if phase.startswith("__"):
        return []
    if "triggers" in data:  # a task starting; a resumed node starts again with the same id
        started[task_id] = phase
        return [{"type": "phase_started", "task_id": task_id, "payload": {"phase": phase}}]
    started.pop(task_id, None)
    if data.get("interrupts"):
        kind = (data["interrupts"][0].get("value") or {}).get("kind")
        payload = {"phase": phase, "pending_kind": kind}
        return [{"type": "phase_interrupted", "task_id": task_id, "payload": payload}]
    if data.get("error") is not None:
        payload = {"phase": phase, "reason": type(data["error"]).__name__}
        return [{"type": "phase_failed", "task_id": task_id, "payload": payload}]
    return [{"type": "phase_completed", "task_id": task_id, "payload": {"phase": phase}}]

```

In `platform/src/cagent/orchestrator.py`, replace:

```python
    async def start(self, thread_id: str, spec: ClusterSpec, runner: str) -> Status:
        await self.graph.ainvoke(self._initial(runner, spec=spec), self._config(thread_id))
        return await self.status(thread_id)

    async def start_from_purpose(
        self, thread_id: str, purpose: str, runner: str, cluster: str | None = None
```

with:

```python
    async def start(
        self, thread_id: str, spec: ClusterSpec, runner: str, on_event: EventSink | None = None
    ) -> Status:
        await self._drive(thread_id, self._initial(runner, spec=spec), on_event)
        return await self.status(thread_id)

    async def start_from_purpose(
        self,
        thread_id: str,
        purpose: str,
        runner: str,
        cluster: str | None = None,
        on_event: EventSink | None = None,
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        await self.graph.ainvoke(initial, self._config(thread_id))
        return await self.status(thread_id)
```

with:

```python
        await self._drive(thread_id, initial, on_event)
        return await self.status(thread_id)

    async def _drive(self, thread_id: str, payload: Any, on_event: EventSink | None) -> None:
        """Run the graph until it waits or ends, consuming the whole stream with or without a
        sink. An exception from a node ends the stream without a task result, so the tasks
        still running are reported failed here before the exception propagates."""
        started: dict[str, str] = {}
        try:
            async for part in self.graph.astream(
                payload,
                self._config(thread_id),
                stream_mode=["tasks", "custom"],
                version="v2",
            ):
                for event in _app_events(part, started):
                    if on_event is not None:
                        await on_event(event)
        except Exception as e:
            if on_event is not None:
                for task_id, phase in started.items():
                    payload = {"phase": phase, "reason": type(e).__name__}
                    await on_event({"type": "phase_failed", "task_id": task_id, "payload": payload})
            raise
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
    async def resume(self, thread_id: str, decision: dict[str, Any]) -> Status:
        """Answer what is pending: a question ({"answer": value | None}), a save
        ({"saved": True}), an approval ({"approve": bool, "allow": [...]}), or an escalation
        ({"action": "retry"}). {"action": "abort"} ends a question, save, or escalation."""
        await self.graph.ainvoke(Command(resume=decision), self._config(thread_id))
```

with:

```python
    async def resume(
        self, thread_id: str, decision: dict[str, Any], on_event: EventSink | None = None
    ) -> Status:
        """Answer what is pending: a question ({"answer": value | None}), a save
        ({"saved": True}), an approval ({"approve": bool, "allow": [...]}), or an escalation
        ({"action": "retry"}). {"action": "abort"} ends a question, save, or escalation."""
        await self._drive(thread_id, Command(resume=decision), on_event)
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        waiting = snap.interrupts[0].value if snap.interrupts else None
        return Status(
            values.get("outcome"), waiting, values.get("compose"), values.get("fixes", [])
```

with:

```python
        first = snap.interrupts[0] if snap.interrupts else None
        return Status(
            values.get("outcome"),
            first.value if first else None,
            values.get("compose"),
            values.get("fixes", []),
            first.id if first else None,
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
        return (await self._call(state, "snapshot")).output or {"services": []}
```

with:

```python
        """Take a snapshot and report it as an observation, stamped when it arrived. A failed
        or malformed snapshot is reported as observation_failed, never as an empty cluster."""
        res = await self._call(state, "snapshot")
        received_at = self.clock()
        write = get_stream_writer()
        out = res.output
        if res.ok and isinstance(out, dict) and isinstance(out.get("services"), list):
            services = [_service_summary(s) for s in out["services"]]
            write({"type": "observation", "received_at": received_at, "services": services})
            return out
        reason = f"snapshot exited {res.exit_code}: {(res.stderr or 'no output')[-300:]}"
        write({"type": "observation_failed", "received_at": received_at, "reason": reason})
        return out if isinstance(out, dict) else {"services": []}
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
329 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/orchestrator.py platform/tests/test_orchestrator_events.py
git commit -F - <<'EOF'
feat(platform): runs report whitelisted phase events and observations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 7: Recovery plans and recovery threads

`cagent.recovery.build_plan` is a pure function from records to a plan: `resume_execution` (validate again, build all or nothing) or `continue_cluster` (a new run on a marked cluster), with risks that need acknowledging. `make_recovery_initial` builds a new thread's state from a whitelist. The graph's `START` goes to `validate` for a recovered state; `start_recovery` and `checkpoint` complete the orchestrator side.

**Files:**

- Modify: `platform/src/cagent/orchestrator.py`
- Create: `platform/src/cagent/recovery.py`
- Test (create): `platform/tests/test_recovery.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_recovery.py`:

```python
"""Recovery plans and the state a recovery thread starts from."""

import yaml
from langgraph.checkpoint.memory import InMemorySaver

from cagent.generate import to_yaml
from cagent.orchestrator import Orchestrator, Settings
from cagent.recovery import (
    CONTINUE,
    INIT_PARTIAL,
    INSTANCE_CHANGED,
    PREVIOUS_UNCONFIRMED,
    RESUME,
    build_plan,
    compose_digest,
    make_recovery_initial,
)

from .conftest import spec_with
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
IDLE = {"at": 1.0, "instance_id": "i-1", "idle": True, "same_instance": True}
INIT = {"image": "busybox", "labels": {"cagent.role": "init"}}


async def no_sleep(_seconds: float) -> None:
    return None


def values(**overrides):
    spec = spec_with(services=[], middleware=[{"template": "redis"}]).model_dump()
    base = {
        "cluster": "orders-poc",
        "runner": "laptop-1",
        "purpose": None,
        "spec": spec,
        "descriptors": {},
        "compose": {"services": {"redis": {"image": "redis:7.4"}}},
        "fixes": [{"rule": "port_conflict"}],
        "built": True,
        "approved": True,
        "allow": ["redis:cap-add"],
        "unchecked": {"previous": {}},
        "proposal": {"patch": []},
        "report": {"passed": True},
        "deadline": 0.0,
        "attempts": {"x": 3},
        "rounds": 7,
        "outcome": None,
    }
    return {**base, **overrides}


def plan(v=None, check=IDLE, dispatched=(), last_approved=None, marks=()):
    return build_plan(
        values=v,
        last_approved=last_approved,
        check=check,
        observation={"services": [{"service": "redis", "state": "running", "exit_code": 0}]},
        dispatched=list(dispatched),
        marks=list(marks),
        catalog_digest="c1",
    )


def test_images_are_reused_only_when_built_and_no_build_was_sent():
    assert plan(values()).content["build"]["decision"] == "skip_all"
    assert plan(values(), dispatched=["build"]).content["build"]["decision"] == "rebuild_all"
    assert plan(values(built=False)).content["build"]["decision"] == "rebuild_all"


def test_unverifiable_risks_need_acknowledging():
    v = values(compose={"services": {"redis": {"image": "redis"}, "redis-init": INIT}})
    p = plan(v, check={**IDLE, "same_instance": False})
    assert p.mode == RESUME and p.required == [INSTANCE_CHANGED, INIT_PARTIAL]
    assert "cannot verify" in p.content["risks"][0]["text"]


def test_a_compose_changed_since_approval_is_shown():
    v = values()
    approved = {"id": "a1", "at": 5.0, "candidate_digest": "other", "content": {"compose": "x"}}
    ids = [r["id"] for r in plan(v, last_approved=approved).content["risks"]]
    assert "compose_changed" in ids
    same = {**approved, "candidate_digest": compose_digest(v["compose"])}
    assert "compose_changed" not in [r["id"] for r in plan(v, last_approved=same).content["risks"]]


def test_a_new_run_on_a_marked_cluster_needs_no_compose():
    p = plan(None, marks=[{"id": "m1", "reason": "up sent without a result"}])
    assert p.mode == CONTINUE and p.required == [PREVIOUS_UNCONFIRMED]
    assert p.content["marks"] == ["m1"] and p.content["steps"] == ["start"]


def test_a_plan_changes_when_its_facts_do():
    a = plan(values())
    b = plan(values(), check={**IDLE, "instance_id": "i-2", "same_instance": False})
    assert a.facts["instance_id"] == "i-1" and a.digest != b.digest
    assert plan(values()).digest == a.digest


def test_the_recovery_state_inherits_no_stale_control_state():
    v = values()
    p = plan(v)
    state = make_recovery_initial(p.content, v, deadline=99.0, source={"thread_id": "r1"})
    assert state["compose"] == v["compose"] and state["fixes"] == v["fixes"]
    assert (state["approved"], state["allow"], state["built"]) == (True, [], True)
    assert state["unchecked"] is None and state["proposal"] is None and state["report"] is None
    assert (state["deadline"], state["attempts"], state["rounds"]) == (99.0, {}, 0)
    assert state["recovered"] and state["source"] == {"thread_id": "r1"}


async def test_a_recovery_thread_starts_at_validate_and_follows_the_build_decision(catalog):
    sim = SimCluster()
    o = Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep)
    v = values()
    state = make_recovery_initial(plan(v).content, v, deadline=1e18, source={})
    status = await o.start_recovery("r1:2", state)
    assert status.outcome == "healthy"
    assert sim.calls[:2] == ["validate", "up"]  # approved, images reused: no build
    assert yaml.safe_load(sim.requests[0][1]["compose"]) == v["compose"]


async def test_a_finding_in_recovery_goes_back_to_a_normal_approval(catalog):
    sim = SimCluster(findings=[{"service": "redis", "rule": "cap-add", "detail": "x"}])
    o = Orchestrator(sim, catalog, InMemorySaver(), FAST, sleep=no_sleep)
    v = values()
    state = make_recovery_initial(plan(v).content, v, deadline=1e18, source={})
    status = await o.start_recovery("r1:2", state)
    assert status.waiting["kind"] == "approval" and status.waiting["allow"] == []
    assert to_yaml(status.compose) == plan(v).content["compose"]
    values_, checkpoint_id = await o.checkpoint("r1:2")
    assert values_["recovered"] and checkpoint_id
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_recovery.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.recovery`.

**Step 3: Implement**

In `platform/src/cagent/orchestrator.py`, replace:

```python
    outcome: str | None
```

with:

```python
    outcome: str | None
    recovered: bool  # a recovery thread: START goes straight to validate
    source: dict[str, Any] | None  # the interrupted thread and checkpoint a recovery came from
```

In `platform/src/cagent/orchestrator.py`, replace:

```python

    async def _drive(self, thread_id: str, payload: Any, on_event: EventSink | None) -> None:
```

with:

```python

    async def start_recovery(
        self, thread_id: str, initial: dict[str, Any], on_event: EventSink | None = None
    ) -> Status:
        """Run an approved recovery on a new thread, from validate (cagent.recovery builds
        initial)."""
        await self._drive(thread_id, initial, on_event)
        return await self.status(thread_id)

    async def checkpoint(self, thread_id: str) -> tuple[dict[str, Any], str | None]:
        """The thread's last checkpointed values and that checkpoint's id."""
        snap = await self.graph.aget_state(self._config(thread_id))
        return dict(snap.values), (snap.config or {}).get("configurable", {}).get("checkpoint_id")

    async def _drive(self, thread_id: str, payload: Any, on_event: EventSink | None) -> None:
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
            START, lambda s: "generate" if s["spec"] else "extract", ["generate", "extract"]
```

with:

```python
            START,
            lambda s: "validate" if s.get("recovered") else "generate" if s["spec"] else "extract",
            ["validate", "generate", "extract"],
```

Create `platform/src/cagent/recovery.py`:

```python
"""Recovery plans for runs whose last execution ended with an unknown result.

A plan is what the user approves before anything runs again: the compose that will be
validated, whether images are rebuilt, and the risks nobody can rule out. It is built by a
pure function from records (the interrupted thread's last checkpoint, the last approval, the
last observation and Runner check, the dispatch log, the cluster's marks), so building or
showing it never touches the cluster. Its facts say what it relies on: a later check that
changes them invalidates the plan.

A recovery runs on a new LangGraph thread whose state is built from a whitelist, never copied:
the old thread's pending node, approval, unchecked patch, report, and deadline must not carry
over into a run the user approved separately.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import yaml

from cagent.generate import to_yaml
from cagent.patches import fingerprint, init_services

RESUME = "resume_execution"  # the run has a compose: validate it again, then continue
CONTINUE = "continue_cluster"  # a new run on a marked cluster: start it normally

# Risks the user must acknowledge one by one; the platform cannot verify them.
INIT_PARTIAL = "init_partial"
INSTANCE_CHANGED = "instance_changed"
PREVIOUS_UNCONFIRMED = "previous_unconfirmed"


@dataclass(frozen=True)
class Plan:
    mode: str
    content: dict[str, Any]  # shown to the user and bound to the approval
    facts: dict[str, Any]  # what the plan relies on
    required: list[str]  # risk ids the approval must acknowledge, exactly
    digest: str


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def compose_digest(compose: dict[str, Any]) -> str:
    return hashlib.sha256(to_yaml(compose).encode()).hexdigest()


def facts_of(check: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
    """The facts of a Runner check and observation that a plan relies on."""
    services = sorted(
        (s["service"], s["state"], s.get("exit_code"))
        for s in (observation or {}).get("services", [])
    )
    return {
        "instance_id": check.get("instance_id"),
        "same_instance": check.get("same_instance"),
        "services": [list(s) for s in services],
    }


def _risk(risk_id: str, text: str, requires_ack: bool) -> dict[str, Any]:
    return {"id": risk_id, "text": text, "requires_ack": requires_ack}


def build_plan(
    *,
    values: dict[str, Any] | None,
    last_approved: dict[str, Any] | None,
    check: dict[str, Any],
    observation: dict[str, Any] | None,
    dispatched: list[str],
    marks: list[dict[str, Any]],
    catalog_digest: str,
) -> Plan:
    """values: the interrupted thread's checkpoint values, None for a new run on a marked
    cluster. last_approved: the latest approval record, if any. check: the latest Runner check
    {instance_id, idle, same_instance}. observation: the latest observation's payload.
    dispatched: methods the interrupted execution sent. marks: the cluster's open marks."""
    risks = []
    if check.get("same_instance") is False:
        risks.append(
            _risk(
                INSTANCE_CHANGED,
                "The Runner restarted since the interrupted execution. You accept a risk the "
                "platform cannot verify: processes the old Runner started may still be running.",
                True,
            )
        )
    for m in marks:
        risks.append(_risk(f"mark:{m['id']}", f"Unconfirmed operation: {m['reason']}", False))
    base = {
        "marks": [m["id"] for m in marks],
        "observation": observation,
        "check": {k: check.get(k) for k in ("at", "instance_id", "idle", "same_instance")},
    }
    facts = {**facts_of(check, observation), "catalog": catalog_digest}

    if values is None or not values.get("compose"):
        risks.append(
            _risk(
                PREVIOUS_UNCONFIRMED,
                "An earlier run's last operation on this cluster is unconfirmed: containers, "
                "volumes, and data it may have changed are left as they are.",
                True,
            )
        )
        content = {**base, "mode": CONTINUE, "steps": ["start"], "risks": risks}
        return _plan(CONTINUE, content, facts, risks)

    compose = values["compose"]
    candidate = compose_digest(compose)
    services = [s["name"] for s in (values.get("spec") or {}).get("services", [])]
    workdir = [
        s["name"]
        for s in (values.get("spec") or {}).get("services", [])
        if s["source"]["kind"] == "workdir"
    ]
    skip = bool(values.get("built")) and "build" not in dispatched
    build = {
        "decision": "skip_all" if skip else "rebuild_all",
        "services": [] if skip else services,
        "reason": (
            "images were built and the interrupted execution sent no build"
            if skip
            else "no proof that every image is complete"
        ),
    }
    if not skip and workdir:
        risks.append(
            _risk(
                "rebuild_workdir",
                "These services run their project's build command again: " + ", ".join(workdir),
                False,
            )
        )
    if inits := sorted(init_services(compose)):
        risks.append(
            _risk(
                INIT_PARTIAL,
                "Init scripts may have run partly; check the data before continuing, because a "
                "script without a marker runs again: " + ", ".join(inits),
                True,
            )
        )
    if last_approved is None or last_approved["candidate_digest"] != candidate:
        risks.append(
            _risk(
                "compose_changed",
                "The compose differs from the last one a person approved "
                "(automatic fixes ran in between).",
                False,
            )
        )
    steps = ["validate", "build" if not skip else "build (skipped)", "up", "watch", "stabilize"]
    content = {
        **base,
        "mode": RESUME,
        "compose": to_yaml(compose),
        "candidate_digest": candidate,
        "previous_approved": (
            {
                "id": last_approved["id"],
                "at": last_approved["at"],
                "compose": last_approved["content"].get("compose"),
            }
            if last_approved
            else None
        ),
        "build": build,
        "steps": steps,
        "risks": risks,
    }
    return _plan(RESUME, content, {**facts, "candidate": candidate}, risks)


def _plan(mode: str, content: dict[str, Any], facts: dict[str, Any], risks: list) -> Plan:
    required = [r["id"] for r in risks if r["requires_ack"]]
    return Plan(mode, content, facts, required, _digest({"content": content, "facts": facts}))


def make_recovery_initial(
    content: dict[str, Any], values: dict[str, Any], deadline: float, source: dict[str, Any]
) -> dict[str, Any]:
    """The new thread's state for a resume_execution plan, built from a whitelist."""
    compose = yaml.safe_load(content["compose"])
    return {
        # kept: what the run is and what was approved to run
        "cluster": values["cluster"],
        "runner": values["runner"],
        "purpose": values.get("purpose"),
        "spec": values["spec"],
        "descriptors": dict(values.get("descriptors") or {}),
        "compose": compose,
        "fixes": list(values.get("fixes") or []),  # history, shown in later approvals
        # from this authorization only
        "approved": True,
        "allow": [],  # validate sends any finding back to a normal approval
        "built": content["build"]["decision"] == "skip_all",
        # set fresh
        "question": None,
        "asked": 0,
        "drafting": None,
        "findings": [],
        "problems": [],
        "snapshot": {"services": []},
        "proposal": None,
        "rejected": [],
        "unchecked": None,
        "restarts": {},
        "attempts": {},
        "rounds": 0,
        "seen": [fingerprint(compose)],
        "report": None,
        "escalation": None,
        "outcome": None,
        "deadline": deadline,
        "recovered": True,
        "source": source,
    }
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
337 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/orchestrator.py platform/src/cagent/recovery.py platform/tests/test_recovery.py
git commit -F - <<'EOF'
feat(platform): recovery plans, and recovery threads that start at validate

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 7 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 8: RunService: create runs and execute them

`cagent.runs.RunService` creates a run, claims the cluster (owner and execution lock) in one transaction, and executes the graph in a background task. Events are committed one by one with their projections; `_finished` (Tx2) records the next pending item or the end, only if the run still belongs to that execution. Reads check ownership and the user's Runners and answer 404 otherwise. `runharness.py` wires a RunService over `SimCluster`.

**Files:**

- Create: `platform/src/cagent/runs.py`
- Test (create): `platform/tests/runharness.py`
- Test (create): `platform/tests/test_runs.py`

**Step 1: Write the failing tests**

Create `platform/tests/runharness.py`:

```python
"""A RunService over SimCluster, with a hub-shaped wrapper, for RunService tests."""

import asyncio
import itertools
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from cagent.dispatch import RecordingCaller
from cagent.hub import InstanceChanged, RunnerUnavailable, ToolResult
from cagent.orchestrator import Orchestrator, Settings
from cagent.runs import RunService
from cagent.store import Store

from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)
USERS = {"alice": {"laptop-1"}, "bob": {"laptop-1"}, "carol": {"laptop-2"}}
REDIS = {"purpose": "a cache", "services": [], "middleware": [{"template": "redis"}]}


async def no_sleep(_seconds: float) -> None:
    return None


class SimHub:
    """The hub's interface over a SimCluster: one runner, an instance id, and status."""

    def __init__(self, sim: SimCluster, instance: str | None = "i-1"):
        self.sim, self.connected_instance = sim, instance
        self.active: dict[str, Any] | None = None
        self.queued: dict[str, int] = {}

    def instance(self, runner_id: str) -> str | None:
        return self.connected_instance

    async def call(self, runner_id, method, params, timeout=None, instance=None):
        if self.connected_instance is None:
            raise RunnerUnavailable(f"{runner_id} is not connected")
        if instance is not None and instance != self.connected_instance:
            raise InstanceChanged("another process")
        if method == "status":
            report = {
                "instance_id": self.connected_instance,
                "active": self.active,
                "queued": self.queued,
            }
            return ToolResult(0, report)
        return await self.sim.call(runner_id, method, params, timeout)


class Harness:
    def __init__(self, store: Store, sim: SimCluster, catalog, saver=None, router=None):
        self.store, self.sim, self.catalog = store, sim, catalog
        self.hub = SimHub(sim)
        self.saver = saver or InMemorySaver()
        self.router = router
        self.ids = itertools.count(1)
        self.service = self.new_service()

    def new_service(self) -> RunService:
        """A RunService as a restarted platform builds it, over the same records."""
        orch = Orchestrator(
            RecordingCaller(self.hub, self.store),
            self.catalog,
            self.saver,
            FAST,
            sleep=no_sleep,
            clock=lambda: 1000.0,
            router=self.router,
        )
        return RunService(
            self.store,
            orch,
            self.hub,
            USERS,
            clock=lambda: 1000.0,
            new_id=lambda: f"id{next(self.ids)}",
        )

    async def settle(self) -> None:
        while self.service.tasks:
            await asyncio.gather(*self.service.tasks.values(), return_exceptions=True)

    async def create(self, user="alice", command_id="c1", cluster="demo", spec=REDIS, **kw):
        acc = await self.service.create_run(
            user, command_id, kw.pop("runner", "laptop-1"), cluster, spec=spec, **kw
        )
        await self.settle()
        return acc

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        return await self.store.events_after(run_id, 0, 10_000, 10_000_000)
```

Create `platform/tests/test_runs.py`:

```python
"""RunService: creating runs, executing them in the background, and reading them back."""

import pytest

from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster


@pytest.fixture
async def h(tmp_path, catalog):
    store = await Store.open(str(tmp_path / "cagent.db"))
    yield Harness(store, SimCluster(), catalog)
    await store.close()


async def test_a_new_run_executes_until_its_first_approval(h):
    acc = await h.create()
    run_id = acc.body["run_id"]
    assert acc.body == {
        "command_id": "c1",
        "run_id": run_id,
        "accepted_revision": 1,
        "execution_id": acc.body["execution_id"],
    }
    snap = await h.service.snapshot("alice", run_id)
    assert (snap["state"], snap["revision"], snap["phase"]) == ("waiting", 2, "approve")
    pending = snap["pending"]
    assert pending["kind"] == "approval" and pending["revision"] == 2
    assert set(pending["payload"]) == {"compose", "findings", "allow", "fixes", "previous_approved"}
    assert pending["payload"]["previous_approved"] is None
    assert snap["watching"] is False and snap["runner_status"]["online"] is False
    states = [e["payload"]["state"] for e in await h.events(run_id) if e["type"] == "run_state"]
    assert states == ["running", "waiting"]
    assert (await h.service.command("alice", run_id, "c1"))["status"] == "done"


async def test_creating_twice_with_one_command_id_creates_one_run(h):
    first = await h.create()
    again = await h.create()
    assert again.replay and again.body == first.body
    assert [r["id"] for r in await h.service.list_runs("alice")] == [first.body["run_id"]]
    with pytest.raises(RunError) as err:
        await h.create(cluster="other")
    assert (err.value.status, err.value.code) == (409, "command_id_reused")


async def test_runs_are_visible_only_to_their_owner_with_the_runner(h):
    run_id = (await h.create()).body["run_id"]
    for user in ("bob", "carol", "nobody"):
        with pytest.raises(RunError) as err:
            await h.service.snapshot(user, run_id)
        assert err.value.status == 404
    assert await h.service.list_runs("bob") == []
    with pytest.raises(RunError) as err:
        await h.create(user="carol", runner="laptop-1")
    assert err.value.status == 404
    h.service.users = {"alice": set()}  # the configuration took the runner away
    with pytest.raises(RunError):
        await h.service.snapshot("alice", run_id)


async def test_a_cluster_has_one_owner_and_one_active_run(h):
    await h.create()
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2")
    assert (err.value.status, err.value.code) == (409, "cluster_busy")
    with pytest.raises(RunError) as err:
        await h.create(user="bob", command_id="c3")
    assert (err.value.status, err.value.code) == (409, "cluster_name_taken")
    assert "alice" not in err.value.detail
    await h.create(command_id="c4", cluster="second")  # another name is free


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"cluster": "Bad Name"}, "invalid_cluster"),
        ({"purpose": "a cache"}, "invalid_input"),  # both a purpose and a spec
        ({"spec": None, "purpose": "a cache"}, "invalid_input"),  # no model configured
        ({"spec": None, "purpose": " "}, "invalid_input"),
        ({"spec": {"purpose": "x", "services": "no"}}, "invalid_spec"),
    ],
)
async def test_invalid_input_is_refused(h, kw, code):
    with pytest.raises(RunError) as err:
        await h.create(**kw)
    assert (err.value.status, err.value.code) == (422, code)
    assert await h.service.list_runs("alice") == []
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_runs.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.runs`.

**Step 3: Implement**

Create `platform/src/cagent/runs.py`:

```python
"""RunService: the one place that changes runs. The web layer calls it; it never touches
LangGraph or the Runner directly.

Every command is accepted in one short transaction (Tx1) that records the command, consumes
what it answers, and marks the run running, all or nothing. The graph then runs in a
background task; its events are committed one by one; its result is recorded in a second
transaction (Tx2) only if the run still belongs to that execution. A command and its
execution are therefore never duplicated, but a Runner operation and its recorded result are
not atomic: when an execution ends without a result, the run goes to reconcile.
"""

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from cagent.catalog import NAME_PATTERN
from cagent.dispatch import Execution, current
from cagent.hub import RunnerHub
from cagent.orchestrator import EventSink, Orchestrator, Status
from cagent.spec import ClusterSpec
from cagent.store import ACTIVE_STATES, Store

TERMINAL = {"healthy": "healthy", "rejected": "rejected", "escalated": "escalated"}

# The fields each kind of pending item shows; everything else in the interrupt stays inside.
PAYLOAD_FIELDS = {
    "question": ("gap", "question", "options", "default", "error"),
    "save_descriptors": ("service", "workdir", "files", "note", "error"),
    "approval": ("compose", "findings", "allow", "fixes"),
    "patch_approval": ("signature", "tier", "root_cause", "patch", "reasons"),
    "escalation": (
        "reason",
        "step",
        "stderr",
        "problems",
        "problem",
        "service",
        "script",
        "log",
        "report",
        "snapshot",
        "fix",
        "root_cause",
        "tier",
        "detail",
        "result",
        "source",
        "fixes",
    ),
}


class RunError(Exception):
    """A command or read the service refuses: maps to an HTTP status."""

    def __init__(self, status: int, code: str, detail: str = "", snapshot: Any = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status, self.code, self.detail, self.snapshot = status, code, detail, snapshot


def not_found() -> RunError:
    # The same answer for a missing run and one the user may not see.
    return RunError(404, "not_found")


@dataclass(frozen=True)
class Accepted:
    body: dict[str, Any]
    replay: bool = False


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# Runs the graph for one execution, reporting events to the sink; returns its Status.
Work = Callable[[EventSink], Awaitable[Status]]


class RunService:
    def __init__(
        self,
        store: Store,
        orchestrator: Orchestrator,
        hub: RunnerHub,
        users: Mapping[str, set[str]],
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        """users maps each user to the runners they may use, as configured now."""
        self.store, self.orch, self.hub, self.users = store, orchestrator, hub, users
        self.clock, self.new_id = clock, new_id
        self.accepting = True
        self.tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # --- access ------------------------------------------------------------------------------

    def _may_use(self, user: str, runner: str) -> bool:
        return runner in self.users.get(user, set())

    def _visible(self, user: str, run: dict[str, Any] | None) -> dict[str, Any]:
        if run is None or run["owner"] != user or not self._may_use(user, run["runner"]):
            raise not_found()
        return run

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _check_accepting(self) -> None:
        if not self.accepting:
            raise RunError(503, "shutting_down")

    async def _replay(self, t, user: str, command_id: str, dig: str) -> Accepted | None:
        """The original answer to a repeated command, or None for a new one."""
        old = await t.one(
            "SELECT * FROM commands WHERE user = ? AND command_id = ?", user, command_id
        )
        if old is None:
            return None
        if old["digest"] != dig:
            raise RunError(409, "command_id_reused")
        return Accepted(old["response"], replay=True)

    # --- reads -------------------------------------------------------------------------------

    async def snapshot(self, user: str, run_id: str) -> dict[str, Any]:
        """Everything a page shows about a run, read in one transaction."""
        async with self.store.read() as t:
            run = self._visible(user, await t.get_run(run_id))
            runner = await t.one("SELECT * FROM runner_status WHERE runner = ?", run["runner"])
        return self._view(run, runner)

    @staticmethod
    def _view(run: dict[str, Any], runner: dict[str, Any] | None) -> dict[str, Any]:
        pending = run["pending"]
        return {
            "id": run["id"],
            "runner": run["runner"],
            "cluster": run["cluster"],
            "input": run["input"],
            "state": run["state"],
            "revision": run["revision"],
            "phase": run["phase"],
            "execution_id": run["execution_id"],
            "pending": (
                {k: pending[k] for k in ("pending_id", "kind", "revision", "created_at", "payload")}
                if pending
                else None
            ),
            "reconcile": run["reconcile"],
            "last_check": run["last_check"],
            "runner_status": (
                {
                    "online": bool(runner["online"]),
                    "instance_id": runner["instance_id"],
                    "changed_at": runner["changed_at"],
                }
                if runner
                else {"online": False, "instance_id": None, "changed_at": None}
            ),
            "last_observation": run["last_observation"],
            "last_observation_attempt": run["last_observation_attempt"],
            "verified_at": run["verified_at"],
            "watching": False,  # nothing watches a cluster after it passed acceptance
            "event_seq": run["event_seq"],
            "min_event_seq": run["min_event_seq"],
        }

    async def list_runs(self, user: str) -> list[dict[str, Any]]:
        async with self.store.read() as t:
            rows = await t.all(
                "SELECT id, runner, cluster, state, revision, created_at FROM runs"
                " WHERE owner = ? ORDER BY created_at DESC",
                user,
            )
        return [r for r in rows if self._may_use(user, r["runner"])]

    async def command(self, user: str, run_id: str, command_id: str) -> dict[str, Any]:
        async with self.store.read() as t:
            self._visible(user, await t.get_run(run_id))
            row = await t.one(
                "SELECT * FROM commands WHERE user = ? AND command_id = ? AND run_id = ?",
                user,
                command_id,
                run_id,
            )
        if row is None:
            raise not_found()
        return {k: row[k] for k in ("command_id", "kind", "status", "effect", "reason", "response")}

    # --- creating a run ----------------------------------------------------------------------

    async def create_run(
        self,
        user: str,
        command_id: str,
        runner: str,
        cluster: str,
        purpose: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> Accepted:
        """Create a run on (runner, cluster) and start it. The cluster's owner is whoever
        first used the name; a cluster with unconfirmed operations starts the run in
        reconcile instead (cagent.recovery)."""
        self._check_accepting()
        if not self._may_use(user, runner):
            raise RunError(404, "not_found", "unknown runner")
        dig = digest(
            {
                "kind": "create_run",
                "runner": runner,
                "cluster": cluster,
                "purpose": purpose,
                "spec": spec,
            }
        )
        async with self.store.read() as t:
            if replay := await self._replay(t, user, command_id, dig):
                return replay
        start = self._validate_start(cluster, purpose, spec)
        run_id, execution_id = self.new_id(), self.new_id()
        async with self._lock(f"cluster:{runner}:{cluster}"), self.store.tx() as t:
            if replay := await self._replay(t, user, command_id, dig):
                return replay
            row = await t.get_cluster(runner, cluster)
            if row and row["owner"] != user:
                raise RunError(409, "cluster_name_taken")
            if row and row["lock_run_id"]:
                holder = await t.get_run(row["lock_run_id"])
                if holder and holder["state"] in ACTIVE_STATES:
                    raise RunError(409, "cluster_busy")
            marks = row["marks"] if row else []
            now = self.clock()
            state = "reconcile" if marks else "running"
            await t.insert(
                "runs",
                id=run_id,
                owner=user,
                runner=runner,
                cluster=cluster,
                input={"purpose": purpose} if purpose is not None else {"spec": start},
                created_at=now,
                state=state,
                revision=1,
                thread_id=run_id,
                executions=0 if marks else 1,
                execution_id=None if marks else execution_id,
                execution_instance=None if marks else self.hub.instance(runner),
                reconcile=(
                    {"reason": "cluster_marked", "marks": [m["id"] for m in marks], "since": now}
                    if marks
                    else None
                ),
            )
            if row:
                await t.update("clusters", {"runner": runner, "name": cluster}, lock_run_id=run_id)
            else:
                await t.insert(
                    "clusters", runner=runner, name=cluster, owner=user, lock_run_id=run_id
                )
            body = {
                "command_id": command_id,
                "run_id": run_id,
                "accepted_revision": 1,
                "execution_id": None if marks else execution_id,
            }
            await t.insert(
                "commands",
                user=user,
                command_id=command_id,
                run_id=run_id,
                kind="create_run",
                **{"class": "advancing"},
                digest=dig,
                response=body,
                status="done" if marks else "accepted",
                at=now,
            )
            await t.append_event(
                run_id, "run_state", {"state": state, "revision": 1, "pending_id": None}
            )
        if not marks:
            self._launch(
                run_id,
                execution_id,
                self.hub.instance(runner),
                (user, command_id),
                self._start_work(run_id, runner, cluster, purpose, start),
            )
        return Accepted(body)

    def _validate_start(
        self, cluster: str, purpose: str | None, spec: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not re.fullmatch(NAME_PATTERN, cluster):
            raise RunError(422, "invalid_cluster", "use lowercase letters, digits, - and _")
        if (purpose is None) == (spec is None):
            raise RunError(422, "invalid_input", "give either a purpose or a spec")
        if purpose is not None:
            if not purpose.strip():
                raise RunError(422, "invalid_input", "the purpose is empty")
            if self.orch.router is None:
                raise RunError(422, "invalid_input", "no model is configured to read a purpose")
            return None
        try:
            return ClusterSpec.model_validate({**spec, "name": cluster}).model_dump()
        except ValidationError as e:
            raise RunError(422, "invalid_spec", str(e)) from None

    def _start_work(
        self,
        thread: str,
        runner: str,
        cluster: str,
        purpose: str | None,
        spec: dict[str, Any] | None,
    ) -> Work:
        if purpose is not None:
            return lambda sink: self.orch.start_from_purpose(
                thread, purpose, runner, cluster=cluster, on_event=sink
            )
        return lambda sink: self.orch.start(
            thread, ClusterSpec.model_validate(spec), runner, on_event=sink
        )

    # --- executions --------------------------------------------------------------------------

    def _launch(
        self,
        run_id: str,
        execution_id: str,
        instance: str | None,
        command: tuple[str, str],
        work: Work,
    ) -> None:
        task = asyncio.create_task(self._execute(run_id, execution_id, instance, command, work))
        self.tasks[execution_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(execution_id, None))

    async def _execute(
        self,
        run_id: str,
        execution_id: str,
        instance: str | None,
        command: tuple[str, str],
        work: Work,
    ) -> None:
        current.set(Execution(run_id, execution_id, instance))

        async def sink(event: dict[str, Any]) -> None:
            await self._record_event(run_id, execution_id, event)

        status = await work(sink)
        await self._finished(run_id, execution_id, command, status)

    async def _record_event(self, run_id: str, execution_id: str, event: dict[str, Any]) -> None:
        """Commit one application event with the projections it changes."""
        kind, payload = event["type"], event["payload"]
        async with self.store.tx() as t:
            seq = await t.append_event(run_id, kind, payload, execution_id, event["task_id"])
            if kind == "phase_started":
                await t.update_run(run_id, phase=payload["phase"])
            elif kind == "observation":
                at = payload["received_at"]
                await t.update_run(
                    run_id,
                    last_observation={**payload, "seq": seq},
                    last_observation_attempt={"at": at, "ok": True, "reason": None},
                )
            elif kind == "observation_failed":
                await t.update_run(
                    run_id,
                    last_observation_attempt={
                        "at": payload["received_at"],
                        "ok": False,
                        "reason": payload["reason"],
                    },
                )

    async def _finished(
        self, run_id: str, execution_id: str, command: tuple[str, str], status: Status
    ) -> None:
        """Tx2: record what the execution ended with, if the run is still its own."""
        async with self.store.tx() as t:
            run = await t.get_run(run_id)
            if run is None or run["execution_id"] != execution_id or run["state"] != "running":
                return  # a late result never overwrites what came after it
            revision = await t.bump_revision(run_id)
            now = self.clock()
            pending, values = None, {}
            if status.waiting is not None:
                kind = status.waiting["kind"]
                payload = {k: status.waiting.get(k) for k in PAYLOAD_FIELDS[kind]}
                if kind == "approval":
                    payload["previous_approved"] = await self._previous_approved(t, run_id)
                pending = {
                    "pending_id": self.new_id(),
                    "kind": kind,
                    "revision": revision,
                    "created_at": now,
                    "payload": payload,
                    "interrupt_id": status.interrupt_id,
                }
                state = "waiting"
            elif status.outcome in TERMINAL:
                state = TERMINAL[status.outcome]
                if state == "healthy":
                    values["verified_at"] = now
                await t.update(
                    "clusters",
                    {"runner": run["runner"], "name": run["cluster"], "lock_run_id": run_id},
                    lock_run_id=None,
                )
            else:
                state = "reconcile"
                values["reconcile"] = {
                    "reason": "no_result",
                    "last_phase": run["phase"],
                    "since": now,
                    "execution_id": execution_id,
                }
            await t.update_run(run_id, state=state, pending=pending, **values)
            await t.update(
                "commands", {"user": command[0], "command_id": command[1]}, status="done"
            )
            await t.append_event(
                run_id,
                "run_state",
                {
                    "state": state,
                    "revision": revision,
                    "pending_id": pending and pending["pending_id"],
                },
                execution_id,
            )

    @staticmethod
    async def _previous_approved(t, run_id: str) -> dict[str, Any] | None:
        """The last compose a person approved for this run, from the immutable records."""
        row = await t.one(
            "SELECT * FROM approvals WHERE run_id = ? AND kind = 'approval'"
            " AND json_extract(decision, '$.approve') = 1 ORDER BY at DESC LIMIT 1",
            run_id,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "at": row["at"],
            "compose": row["content"]["compose"],
            "digest": row["candidate_digest"],
        }
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
346 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/runs.py platform/tests/runharness.py platform/tests/test_runs.py
git commit -F - <<'EOF'
feat(platform): RunService creates runs and executes them in the background

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 9: RunService: answer pending items

`submit` takes the command envelope and checks, in order: access, the idempotency record, then revision and pending id, then the decision. `check_decision` validates each kind strictly, including the full-set `allow` and recovery acknowledgements. Tx1 records the command, an immutable approval record, and the move to running in one transaction; the approval payload carries `previous_approved` from the records.

**Files:**

- Modify: `platform/src/cagent/runs.py`
- Test (create): `platform/tests/test_runs_commands.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_runs_commands.py`:

```python
"""Answering pending items: versions, idempotency, strict decisions, approval records."""

import pytest

from cagent.recovery import compose_digest
from cagent.runs import RunError, check_decision
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster

ORDERS_WITHOUT_ENV = {
    "purpose": "orders",
    "services": [{"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}],
    "middleware": [{"template": "mysql"}, {"template": "kafka"}],
}
FINDING = {"service": "redis", "rule": "cap-add", "detail": "x"}


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def answer(h, run_id, decision, command_id="a1", user="alice", **overrides):
    snap = await h.service.snapshot(user, run_id)
    body = {
        "command_id": command_id,
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": decision,
        **overrides,
    }
    acc = await h.service.submit(user, run_id, body)
    await h.settle()
    return acc, body


async def test_an_approved_run_becomes_healthy_and_frees_its_cluster(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    acc, _ = await answer(h, run_id, {"approve": True})
    assert acc.body["accepted_revision"] == 3 and acc.body["execution_id"]
    snap = await h.service.snapshot("alice", run_id)
    assert (snap["state"], snap["verified_at"], snap["pending"]) == ("healthy", 1000.0, None)
    assert snap["last_observation"]["services"][0]["service"] == "redis"
    async with store.read() as t:
        [record] = await t.all("SELECT * FROM approvals")
    assert record["decision"] == {"approve": True, "allow": []}
    assert record["candidate_digest"] == compose_digest(h.sim.compose)
    await h.create(command_id="c2")  # the healthy run released the cluster


async def test_a_repeated_answer_runs_once(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    first, body = await answer(h, run_id, {"approve": True})
    again = await h.service.submit("alice", run_id, body)
    assert again.replay and again.body == first.body
    assert h.sim.calls.count("up") == 1
    with pytest.raises(RunError) as err:  # same pending item, a new command id
        await h.service.submit("alice", run_id, {**body, "command_id": "a2"})
    assert (err.value.status, err.value.code) == (409, "not_waiting")
    assert err.value.snapshot["state"] == "healthy"
    with pytest.raises(RunError) as err:  # same command id, another decision
        await h.service.submit("alice", run_id, {**body, "decision": {"approve": False}})
    assert err.value.code == "command_id_reused"


async def test_a_replay_still_needs_access(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    _, body = await answer(h, run_id, {"approve": True})
    h.service.users = {"alice": set()}
    with pytest.raises(RunError) as err:
        await h.service.submit("alice", run_id, body)
    assert err.value.status == 404


async def test_stale_and_malformed_commands_consume_nothing(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    for overrides, code in [
        ({"expected_revision": 1}, "stale"),
        ({"pending_id": "old"}, "stale"),
    ]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, {"approve": True}, **overrides)
        assert (err.value.status, err.value.code) == (409, code)
        assert err.value.snapshot["revision"] == 2
    for decision in [{"approve": "yes"}, {"approve": True, "extra": 1}, {}]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, decision)
        assert (err.value.status, err.value.code) == (422, "invalid_decision")
    with pytest.raises(RunError) as err:
        await h.service.submit("alice", run_id, {"command_id": "x", "action": "dance"})
    assert err.value.code == "invalid_command"
    assert (await h.service.snapshot("alice", run_id))["state"] == "waiting"
    await answer(h, run_id, {"approve": True})  # a valid answer still works
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_allow_names_only_this_candidates_findings(store, catalog):
    h = Harness(store, SimCluster(findings=[FINDING]), catalog)
    run_id = (await h.create()).body["run_id"]
    for decision in [
        {"approve": True, "allow": ["redis:privileged"]},
        {"approve": True, "allow": ["cap-add"]},
        {"approve": True, "allow": ["redis:cap-add", "redis:cap-add"]},
        {"approve": False, "allow": ["redis:cap-add"]},
    ]:
        with pytest.raises(RunError) as err:
            await answer(h, run_id, decision)
        assert err.value.status == 422
    await answer(h, run_id, {"approve": True}, command_id="a1")  # the finding stays uncovered
    snap = await h.service.snapshot("alice", run_id)
    previous = snap["pending"]["payload"]["previous_approved"]
    assert previous["compose"] == snap["pending"]["payload"]["compose"]
    await answer(h, run_id, {"approve": True, "allow": ["redis:cap-add"]}, command_id="a2")
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_questions_take_defaults_answers_or_an_abort(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create(spec=ORDERS_WITHOUT_ENV)).body["run_id"]
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    assert pending["kind"] == "question"
    assert pending["payload"]["gap"]["key"] == "SPRING_PROFILES_ACTIVE"
    with pytest.raises(RunError):
        await answer(h, run_id, {"answer": "dev", "action": "abort"})
    await answer(h, run_id, {"answer": "dev"})
    assert (await h.service.snapshot("alice", run_id))["pending"]["kind"] == "approval"

    run2 = (await h.create(command_id="c2", cluster="other", spec=ORDERS_WITHOUT_ENV)).body
    await answer(h, run2["run_id"], {"action": "abort"}, command_id="a9")
    assert (await h.service.snapshot("alice", run2["run_id"]))["state"] == "rejected"


async def test_an_escalation_ends_or_retries(store, catalog):
    h = Harness(store, SimCluster(crash={"redis": 3}), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    assert pending["kind"] == "escalation" and pending["payload"]["reason"] == "no_rule"
    await answer(h, run_id, {"action": "retry"}, command_id="a2")
    assert (await h.service.snapshot("alice", run_id))["pending"]["kind"] == "escalation"
    await answer(h, run_id, {"action": "abort"}, command_id="a3")
    assert (await h.service.snapshot("alice", run_id))["state"] == "escalated"
    await h.create(command_id="c2")  # an ended run released the cluster


def test_decisions_are_strict_for_every_kind():
    assert check_decision("question", {}, {"answer": None}) == {"answer": None}
    assert check_decision("save_descriptors", {}, {"saved": True}) == {"saved": True}
    assert check_decision("patch_approval", {}, {"approve": False}) == {"approve": False}
    plan = {"required": ["init_partial"]}
    assert check_decision("recovery", plan, {"approve": True, "acknowledged": ["init_partial"]})
    assert check_decision("recovery", plan, {"approve": False})
    for kind, payload, decision in [
        ("question", {}, {}),
        ("save_descriptors", {}, {"saved": False}),
        ("patch_approval", {}, {"approve": 1}),
        ("escalation", {}, {"action": "skip"}),
        ("recovery", plan, {"approve": True}),
        ("recovery", plan, {"approve": True, "acknowledged": ["init_partial", "x"]}),
        ("recovery", plan, {"approve": False, "acknowledged": ["init_partial"]}),
    ]:
        with pytest.raises(RunError) as err:
            check_decision(kind, payload, decision)
        assert err.value.status == 422, (kind, decision)
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_runs_commands.py -q
```

Expected:

```
1 error
```

Collection fails: `check_decision` cannot be imported.

**Step 3: Implement**

In `platform/src/cagent/runs.py`, replace:

```python
from typing import Any

from pydantic import ValidationError
```

with:

```python
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
```

In `platform/src/cagent/runs.py`, replace:

```python
    ),
}

```

with:

```python
    ),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Strict):
    """Every command on an existing run."""

    command_id: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = None
    action: Literal["answer", "refresh", "review_recovery", "abandon"]
    pending_id: str | None = None
    decision: dict[str, Any] | None = None


class _Answer(_Strict):
    answer: Any  # required; null takes the question's default


class _Abort(_Strict):
    action: Literal["abort"]


class _Saved(_Strict):
    saved: Literal[True]  # asks for a read-back; it does not mean the file is valid


class _Approval(_Strict):
    approve: StrictBool
    allow: list[str] = []


class _PatchApproval(_Strict):
    approve: StrictBool


class _Escalation(_Strict):
    action: Literal["retry", "abort"]


class _Recovery(_Strict):
    approve: StrictBool
    acknowledged: list[str] = []


def check_decision(kind: str, payload: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Validate a decision for a pending item strictly; raise a 422 RunError otherwise."""
    models: dict[str, type[_Strict]] = {
        "question": _Abort if "action" in decision else _Answer,
        "save_descriptors": _Abort if "action" in decision else _Saved,
        "approval": _Approval,
        "patch_approval": _PatchApproval,
        "escalation": _Escalation,
        "recovery": _Recovery,
    }
    try:
        d = models[kind].model_validate(decision).model_dump()
    except ValidationError as e:
        raise RunError(422, "invalid_decision", str(e)) from None
    if kind == "approval":
        findings = {f"{f['service']}:{f['rule']}" for f in payload["findings"]}
        if not d["approve"] and d["allow"]:
            raise RunError(422, "invalid_decision", "a rejection allows nothing")
        if len(set(d["allow"])) != len(d["allow"]) or not set(d["allow"]) <= findings:
            raise RunError(422, "invalid_decision", "allow names only this candidate's findings")
    if kind == "recovery":
        acks = d["acknowledged"]
        if d["approve"] and (len(set(acks)) != len(acks) or set(acks) != set(payload["required"])):
            raise RunError(422, "invalid_decision", "acknowledge exactly the required risks")
        if not d["approve"] and acks:
            raise RunError(422, "invalid_decision", "a rejection acknowledges nothing")
    return d

```

In `platform/src/cagent/runs.py`, replace:

```python
        return {k: row[k] for k in ("command_id", "kind", "status", "effect", "reason", "response")}
```

with:

```python
        return {k: row[k] for k in ("command_id", "kind", "status", "effect", "reason", "response")}

    async def _snapshot_in(self, t, run: dict[str, Any]) -> dict[str, Any]:
        runner = await t.one("SELECT * FROM runner_status WHERE runner = ?", run["runner"])
        return self._view(run, runner)

    # --- commands on a run ------------------------------------------------------------------

    async def submit(self, user: str, run_id: str, body: dict[str, Any]) -> Accepted:
        """Accept one command on a run. Checks run in this order: access, the idempotency
        record, then (new commands only) revision and pending item, then the decision."""
        self._check_accepting()
        try:
            env = Envelope.model_validate(body)
        except ValidationError as e:
            raise RunError(422, "invalid_command", str(e)) from None
        async with self.store.read() as t:
            self._visible(user, await t.get_run(run_id))
        dig = digest(
            {
                "run_id": run_id,
                "action": env.action,
                "pending_id": env.pending_id,
                "decision": env.decision,
            }
        )
        async with self.store.read() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
        handler = getattr(self, f"_{env.action}")
        async with self._lock(run_id):
            return await handler(user, run_id, env, dig)

    async def _current(self, t, run_id: str, env: Envelope, state: str) -> dict[str, Any]:
        """The run, if it is in state at the revision the command expects; else a 409."""
        run = await t.get_run(run_id)
        if run["state"] != state:
            raise RunError(409, f"not_{state}", snapshot=await self._snapshot_in(t, run))
        if env.expected_revision != run["revision"]:
            raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
        return run

    async def _answer(self, user: str, run_id: str, env: Envelope, dig: str) -> Accepted:
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await self._current(t, run_id, env, "waiting")
            pending = run["pending"]
            if env.pending_id != pending["pending_id"]:
                raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
            decision = check_decision(pending["kind"], pending["payload"], env.decision or {})
            now = self.clock()
            if pending["kind"] in ("approval", "patch_approval"):
                await self._record_approval(t, user, run_id, pending, decision, now)
            execution_id = self.new_id()
            instance = self.hub.instance(run["runner"])
            body = await self._accept(
                t, user, run, env.command_id, dig, now, execution_id=execution_id
            )
            await t.update_run(
                run_id,
                pending=None,
                execution_id=execution_id,
                executions=run["executions"] + 1,
                execution_instance=instance,
            )
        thread = run["thread_id"]
        self._launch(
            run_id,
            execution_id,
            instance,
            (user, env.command_id),
            lambda sink: self.orch.resume(thread, decision, on_event=sink),
        )
        return Accepted(body)

    async def _accept(
        self,
        t,
        user: str,
        run: dict[str, Any],
        command_id: str,
        dig: str,
        now: float,
        execution_id: str | None,
        state: str = "running",
        status: str = "accepted",
        kind: str = "answer",
    ) -> dict[str, Any]:
        """Record an accepted command and move the run to state, in the caller's Tx1."""
        revision = await t.bump_revision(run["id"])
        body = {
            "command_id": command_id,
            "run_id": run["id"],
            "accepted_revision": revision,
            "execution_id": execution_id,
        }
        await t.insert(
            "commands",
            user=user,
            command_id=command_id,
            run_id=run["id"],
            kind=kind,
            **{"class": "advancing"},
            digest=dig,
            response=body,
            status=status,
            execution_id=execution_id,
            at=now,
        )
        await t.update_run(run["id"], state=state)
        await t.append_event(
            run["id"], "run_state", {"state": state, "revision": revision, "pending_id": None}
        )
        return body

    async def _record_approval(
        self,
        t,
        user: str,
        run_id: str,
        pending: dict[str, Any],
        decision: dict[str, Any],
        now: float,
    ) -> None:
        """An immutable record of what was shown and what the person decided."""
        payload, kind = pending["payload"], pending["kind"]
        if kind == "approval":  # the same digest recovery plans compare against
            shown = hashlib.sha256(payload["compose"].encode()).hexdigest()
        elif kind == "patch_approval":
            shown = digest(payload["patch"])
        else:
            shown = payload["digest"]
        await t.insert(
            "approvals",
            id=self.new_id(),
            run_id=run_id,
            pending_id=pending["pending_id"],
            kind=kind,
            candidate_digest=shown,
            content=payload,
            decision=decision,
            user=user,
            at=now,
        )
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
354 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/runs.py platform/tests/test_runs_commands.py
git commit -F - <<'EOF'
feat(platform): answer pending items with versioned, idempotent commands

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 10: RunService: interrupted executions, Runner status, shutdown

An execution that raises or is cancelled goes through Tx2': the command is `interrupted` with an `unknown` effect, the run goes to `reconcile`, and the cluster gets a mark when a mutating call has no recorded result. Hub listener events become the stored Runner projection and `runner` events. `close()` stops accepting, then cancels executions.

**Files:**

- Modify: `platform/src/cagent/runs.py`
- Test (create): `platform/tests/test_runs_interrupted.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_runs_interrupted.py`:

```python
"""Executions that end without a result: reconcile, cluster marks, shutdown, runner status."""

import asyncio

import pytest

from cagent.hub import RunnerUnavailable
from cagent.orchestrator import Status
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def cluster(store, name="demo"):
    async with store.read() as t:
        return await t.get_cluster("laptop-1", name)


async def approve_without_settling(h, run_id):
    snap = await h.service.snapshot("alice", run_id)
    body = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    return await h.service.submit("alice", run_id, body)


async def test_a_lost_runner_sends_the_run_to_reconcile_and_marks_the_cluster(store, catalog):
    class Lost(SimCluster):
        def _up(self, p):
            raise RunnerUnavailable("laptop-1 disconnected")

    h = Harness(store, Lost(), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile" and snap["pending"] is None
    assert snap["reconcile"]["reason"] == "RunnerUnavailable: laptop-1 disconnected"
    assert snap["reconcile"]["last_phase"] == "up"
    cmd = await h.service.command("alice", run_id, "a1")
    assert (cmd["status"], cmd["effect"]) == ("interrupted", "unknown")
    [mark] = (await cluster(store))["marks"]
    assert mark["reason"] == "up sent without a recorded result"
    with pytest.raises(RunError) as err:  # reconcile keeps the cluster's execution lock
        await h.create(command_id="c2")
    assert err.value.code == "cluster_busy"


async def test_a_call_refused_before_sending_marks_nothing(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    h.hub.connected_instance = "i-2"  # the runner restarted before the execution's first call
    await h.settle()
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"].startswith("InstanceChanged")
    assert (await cluster(store))["marks"] == []
    assert h.sim.calls == ["validate"]  # only the first execution's call


async def test_stopping_the_platform_interrupts_executions(store, catalog):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(SimCluster):
        async def call(self, runner_id, method, params, timeout=None):
            if method == "up":
                entered.set()
                await release.wait()
            return await super().call(runner_id, method, params, timeout)

    h = Harness(store, Slow(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    async with asyncio.timeout(5):
        await entered.wait()
    await h.service.close()
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"] == "cancelled: the platform stopped"
    assert (await cluster(store))["marks"][0]["reason"] == "up sent without a recorded result"
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2", cluster="other")
    assert (err.value.status, err.value.code) == (503, "shutting_down")


async def test_a_late_result_never_overwrites_later_state(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    before = await h.service.snapshot("alice", run_id)
    await h.service._finished(
        run_id, "an-old-execution", ("alice", "c1"), Status("healthy", None, None, [])
    )
    await h.service._interrupted(run_id, "an-old-execution", ("alice", "c1"), "late")
    assert await h.service.snapshot("alice", run_id) == before


async def test_runner_changes_are_recorded_and_told_to_active_runs(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    h.service.runner_changed("laptop-1", True, "i-1")
    h.service.runner_changed("laptop-1", False, None)
    await asyncio.gather(*h.service._background)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["runner_status"] == {"online": False, "instance_id": None, "changed_at": 1000.0}
    told = [e["payload"] for e in await h.events(run_id) if e["type"] == "runner"]
    assert told == [{"online": True, "instance_id": "i-1"}, {"online": False, "instance_id": None}]
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_runs_interrupted.py -q
```

Expected:

```
5 failed
```

The five new tests fail: nothing handles errors yet, and `close` and `runner_changed` do not exist.

**Step 3: Implement**

In `platform/src/cagent/runs.py`, replace:

```python
import json
```

with:

```python
import json
import logging
```

In `platform/src/cagent/runs.py`, replace:

```python
from cagent.dispatch import Execution, current
```

with:

```python
from cagent.dispatch import MUTATING, Execution, current
```

In `platform/src/cagent/runs.py`, replace:

```python
from cagent.store import ACTIVE_STATES, Store
```

with:

```python
from cagent.store import ACTIVE_STATES, Store

log = logging.getLogger(__name__)
```

In `platform/src/cagent/runs.py`, replace:

```python
        self._locks: dict[str, asyncio.Lock] = {}
```

with:

```python
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task] = set()
        self._runner_lock = asyncio.Lock()

    async def close(self) -> None:
        """Stop accepting commands, then stop the executions. Each records why it stopped;
        one that cannot is found unfinished at the next start."""
        self.accepting = False
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self._background, return_exceptions=True)

    # --- runners ------------------------------------------------------------------------------

    def runner_changed(self, runner: str, online: bool, instance: str | None) -> None:
        """Hub listener: record the change and tell the runner's active runs."""
        task = asyncio.get_running_loop().create_task(self._record_runner(runner, online, instance))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _record_runner(self, runner: str, online: bool, instance: str | None) -> None:
        async with self._runner_lock, self.store.tx() as t:  # in the order the hub saw them
            await t.run(
                "INSERT INTO runner_status (runner, online, instance_id, changed_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (runner) DO UPDATE SET"
                " online = excluded.online, instance_id = excluded.instance_id,"
                " changed_at = excluded.changed_at",
                runner,
                int(online),
                instance,
                self.clock(),
            )
            marks = ", ".join("?" for _ in ACTIVE_STATES)
            runs = await t.all(
                f"SELECT id FROM runs WHERE runner = ? AND state IN ({marks})",
                runner,
                *ACTIVE_STATES,
            )
            for r in runs:
                await t.append_event(r["id"], "runner", {"online": online, "instance_id": instance})
```

In `platform/src/cagent/runs.py`, replace:

```python
        status = await work(sink)
        await self._finished(run_id, execution_id, command, status)
```

with:

```python
        try:
            status = await work(sink)
        except asyncio.CancelledError:
            await self._interrupted(
                run_id, execution_id, command, "cancelled: the platform stopped"
            )
            raise
        except Exception as e:  # the Runner went away, a call timed out, an event failed to commit
            log.warning("execution %s of run %s failed: %r", execution_id, run_id, e)
            await self._interrupted(run_id, execution_id, command, f"{type(e).__name__}: {e}")
            return
        await self._finished(run_id, execution_id, command, status)

    async def _interrupted(
        self, run_id: str, execution_id: str, command: tuple[str, str], reason: str
    ) -> None:
        """Tx2': the execution ended without a result. What it did is unknown: the run goes
        to reconcile, and the cluster is marked if a mutating call has no recorded result."""
        async with self.store.tx() as t:
            run = await t.get_run(run_id)
            if run is None or run["execution_id"] != execution_id or run["state"] != "running":
                return
            now = self.clock()
            await self._mark_unconfirmed(t, run, execution_id, now)
            revision = await t.bump_revision(run_id)
            await t.update(
                "commands",
                {"user": command[0], "command_id": command[1]},
                status="interrupted",
                effect="unknown",
                reason=reason,
            )
            await t.update_run(
                run_id,
                state="reconcile",
                pending=None,
                reconcile={
                    "reason": reason,
                    "last_phase": run["phase"],
                    "since": now,
                    "execution_id": execution_id,
                },
            )
            await t.append_event(
                run_id,
                "run_state",
                {"state": "reconcile", "revision": revision, "pending_id": None},
                execution_id,
            )

    async def _mark_unconfirmed(
        self, t, run: dict[str, Any], execution_id: str | None, now: float
    ) -> None:
        """Mark the run's cluster for each mutating call execution_id sent without a result."""
        if execution_id is None:
            return
        marks = ", ".join("?" for _ in MUTATING)
        sent = await t.all(
            "SELECT method FROM dispatches WHERE execution_id = ? AND result_at IS NULL"
            f" AND method IN ({marks}) ORDER BY call_id",
            execution_id,
            *sorted(MUTATING),
        )
        if not sent:
            return
        cluster = await t.get_cluster(run["runner"], run["cluster"])
        mark = {
            "id": self.new_id(),
            "reason": ", ".join(d["method"] for d in sent) + " sent without a recorded result",
            "run_id": run["id"],
            "execution_id": execution_id,
            "at": now,
        }
        await t.update(
            "clusters",
            {"runner": run["runner"], "name": run["cluster"]},
            marks=[*cluster["marks"], mark],
        )
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
359 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/runs.py platform/tests/test_runs_interrupted.py
git commit -F - <<'EOF'
feat(platform): executions without a result go to reconcile and mark the cluster

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 10 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 11: RunService: startup reconciliation

`recover_on_startup` marks every Runner offline and sends to `reconcile` each run with an open advancing command, a `running` state, or a pending item that no longer matches its checkpoint (or plan). Open read-only commands are closed without touching their run. It advances no graph and calls no Runner.

**Files:**

- Modify: `platform/src/cagent/runs.py`
- Test (create): `platform/tests/test_runs_startup.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_runs_startup.py`:

```python
"""Startup reconciliation: what a restarted platform does with the runs it finds."""

import asyncio

import pytest

from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer
from .test_runs_interrupted import approve_without_settling, cluster


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


async def restart(h: Harness) -> None:
    """A new process over the same database and checkpoints; the old one simply stops."""
    h.service = h.new_service()
    await h.service.recover_on_startup()


async def test_a_run_waiting_for_input_waits_again_after_a_restart(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    before = await h.service.snapshot("alice", run_id)
    await restart(h)
    after = await h.service.snapshot("alice", run_id)
    assert after["state"] == "waiting" and after["pending"] == before["pending"]
    await answer(h, run_id, {"approve": True})
    assert (await h.service.snapshot("alice", run_id))["state"] == "healthy"


async def test_an_execution_cut_off_by_a_crash_goes_to_reconcile(store, catalog):
    entered, never = asyncio.Event(), asyncio.Event()

    class Stuck(SimCluster):
        async def call(self, runner_id, method, params, timeout=None):
            if method == "up":
                entered.set()
                await never.wait()  # the process dies here
            return await super().call(runner_id, method, params, timeout)

    h = Harness(store, Stuck(), catalog)
    run_id = (await h.create()).body["run_id"]
    await approve_without_settling(h, run_id)
    async with asyncio.timeout(5):
        await entered.wait()
    dead = h.service
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"] == {
        "reason": "platform restarted",
        "last_phase": "up",
        "since": 1000.0,
        "execution_id": snap["execution_id"],
    }
    cmd = await h.service.command("alice", run_id, "a1")
    assert (cmd["status"], cmd["effect"]) == ("interrupted", "unknown")
    assert (await cluster(store))["marks"][0]["reason"] == "up sent without a recorded result"
    assert h.sim.calls == ["validate", "validate"]  # the stuck up was never sent again
    # The dead process's execution, if it ever ended, could not touch the reconciled run.
    await dead.close()
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"


async def test_a_result_the_graph_reached_but_never_recorded_goes_to_reconcile(store, catalog):
    h = Harness(store, SimCluster(), catalog)

    async def crash_before_tx2(*_args):
        return None  # the graph finished, the process died before recording it

    run_id = (await h.create()).body["run_id"]
    h.service._finished = crash_before_tx2
    await answer(h, run_id, {"approve": True})
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"  # even though the checkpoint says healthy
    assert (await cluster(store))["marks"] == []  # every call got its result


async def test_a_pending_item_that_no_longer_matches_the_checkpoint_goes_to_reconcile(
    store, catalog
):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    async with store.tx() as t:
        run = await t.get_run(run_id)
        await t.update_run(run_id, pending={**run["pending"], "interrupt_id": "another"})
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile"
    assert snap["reconcile"]["reason"] == "pending item no longer matches the checkpoint"


async def test_an_unfinished_read_changes_no_run_and_runners_start_offline(store, catalog):
    h = Harness(store, SimCluster(), catalog)
    run_id = (await h.create()).body["run_id"]
    h.service.runner_changed("laptop-1", True, "i-1")
    await asyncio.gather(*h.service._background)
    async with store.tx() as t:
        await t.insert(
            "commands",
            user="alice",
            command_id="r1",
            run_id=run_id,
            kind="refresh",
            **{"class": "read_only"},
            digest="d",
            response={},
            status="accepted",
            at=1.0,
        )
    await restart(h)
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "waiting" and snap["runner_status"]["online"] is False
    assert (await h.service.command("alice", run_id, "r1"))["status"] == "interrupted"
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_runs_startup.py -q
```

Expected:

```
5 failed
```

The tests fail: `recover_on_startup` does not exist.

**Step 3: Implement**

In `platform/src/cagent/runs.py`, replace:

```python
        await asyncio.gather(*self._background, return_exceptions=True)
```

with:

```python
        await asyncio.gather(*self._background, return_exceptions=True)

    async def recover_on_startup(self) -> None:
        """Before accepting requests: mark every Runner offline, and send each run that cannot
        show where it stands to reconcile. Nothing here advances a graph or calls a Runner.

        A run keeps waiting only if no advancing command is open and its pending item is
        intact: a graph interrupt still in the checkpoint, or a recovery plan still valid."""
        now = self.clock()
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        async with self.store.read() as t:
            runs = await t.all(f"SELECT * FROM runs WHERE state IN ({marks})", *ACTIVE_STATES)
        interrupts = {}
        for run in runs:
            if run["state"] == "waiting" and run["pending"]["kind"] != "recovery":
                interrupts[run["id"]] = (await self.orch.status(run["thread_id"])).interrupt_id
        async with self.store.tx() as t:
            await t.run(
                "UPDATE runner_status SET online = 0, instance_id = NULL, changed_at = ?", now
            )
            # A read-only check that never finished changed nothing.
            await t.run(
                "UPDATE commands SET status = 'interrupted', reason = 'platform restarted'"
                " WHERE status = 'accepted' AND class = 'read_only'"
            )
            for old in runs:
                run = await t.get_run(old["id"])
                open_ = await t.all(
                    "SELECT * FROM commands WHERE run_id = ? AND status = 'accepted'"
                    " AND class = 'advancing'",
                    run["id"],
                )
                if not open_ and await self._intact(t, run, interrupts.get(run["id"])):
                    continue
                reason = (
                    "platform restarted"
                    if open_ or run["state"] == "running"
                    else ("pending item no longer matches the checkpoint")
                )
                await self._mark_unconfirmed(t, run, run["execution_id"], now)
                for c in open_:
                    await t.update(
                        "commands",
                        {"user": c["user"], "command_id": c["command_id"]},
                        status="interrupted",
                        effect="unknown",
                        reason=reason,
                    )
                revision = await t.bump_revision(run["id"])
                await t.update_run(
                    run["id"],
                    state="reconcile",
                    pending=None,
                    reconcile={
                        "reason": reason,
                        "last_phase": run["phase"],
                        "since": now,
                        "execution_id": run["execution_id"],
                    },
                )
                await t.append_event(
                    run["id"],
                    "run_state",
                    {"state": "reconcile", "revision": revision, "pending_id": None},
                )

    @staticmethod
    async def _intact(t, run: dict[str, Any], interrupt_id: str | None) -> bool:
        if run["state"] == "reconcile":
            return True
        if run["state"] != "waiting":
            return False
        pending = run["pending"]
        if pending["kind"] == "recovery":
            plan = await t.one(
                "SELECT id FROM plans WHERE id = ? AND invalidated_at IS NULL",
                pending["payload"]["plan_id"],
            )
            return plan is not None
        return interrupt_id is not None and interrupt_id == pending["interrupt_id"]
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
364 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/runs.py platform/tests/test_runs_startup.py
git commit -F - <<'EOF'
feat(platform): a restarted platform reconciles runs it cannot vouch for

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 12: RunService: refresh, recovery plans, approval, abandon

`refresh` asks the Runner for `status` and, if the cluster is idle, a snapshot; it invalidates a published plan whose facts changed. `review_recovery` publishes a plan as a `recovery` pending item. Approving one re-checks the Runner outside any transaction, then starts a new thread pinned to the planned instance; declining needs no Runner. `abandon` ends orchestration and keeps marks. A recovery chain that reaches healthy clears exactly the marks its plan named.

**Files:**

- Modify: `platform/src/cagent/orchestrator.py`
- Modify: `platform/src/cagent/runs.py`
- Test (create): `platform/tests/test_runs_recovery.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_runs_recovery.py`:

```python
"""Recovery: refresh, review a plan, approve or decline it, abandon; and clearing marks."""

import pytest

from cagent.hub import RunnerUnavailable
from cagent.recovery import INSTANCE_CHANGED, PREVIOUS_UNCONFIRMED
from cagent.runs import RunError
from cagent.store import Store

from .runharness import Harness
from .simcluster import SimCluster
from .test_runs_commands import answer
from .test_runs_interrupted import cluster


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"))
    yield s
    await s.close()


class LosesUpOnce(SimCluster):
    """The first up starts the cluster, then the answer is lost with the connection."""

    lost = False

    def _up(self, p):
        res = super()._up(p)
        if not self.lost:
            self.lost = True
            raise RunnerUnavailable("laptop-1 disconnected")
        return res


async def interrupted_run(store, catalog, sim=None):
    """A run whose up was sent and never answered: reconcile, cluster marked."""
    h = Harness(store, sim or LosesUpOnce(), catalog)
    run_id = (await h.create()).body["run_id"]
    await answer(h, run_id, {"approve": True})
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    return h, run_id


async def command(h, run_id, action, command_id, **extra):
    snap = await h.service.snapshot("alice", run_id)
    body = {"command_id": command_id, "expected_revision": snap["revision"], "action": action}
    acc = await h.service.submit("alice", run_id, {**body, **extra})
    await h.settle()
    return acc


async def test_refresh_review_and_approve_recover_the_run_and_clear_its_mark(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    before = await h.service.snapshot("alice", run_id)
    await command(h, run_id, "refresh", "f1")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["revision"] == before["revision"]  # a read changes no version
    assert snap["last_check"] == {
        "at": 1000.0,
        "instance_id": "i-1",
        "idle": True,
        "same_instance": True,
    }
    assert snap["last_observation"]["services"][0]["service"] == "redis"

    await command(h, run_id, "review_recovery", "v1")
    pending = (await h.service.snapshot("alice", run_id))["pending"]
    plan = pending["payload"]
    assert pending["kind"] == "recovery" and plan["mode"] == "resume_execution"
    assert plan["build"]["decision"] == "skip_all" and plan["required"] == []
    assert plan["steps"] == ["validate", "build (skipped)", "up", "watch", "stabilize"]
    assert [r["id"] for r in plan["risks"]] == [f"mark:{plan['marks'][0]}"]

    await answer(h, run_id, {"approve": True}, command_id="r1")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "healthy"
    assert (await cluster(store))["marks"] == []
    assert h.sim.calls.count("up") == 2 and "build" not in h.sim.calls
    async with store.read() as t:
        kinds = [a["kind"] for a in await t.all("SELECT kind FROM approvals ORDER BY at")]
    assert kinds == ["approval", "recovery"]


async def test_a_busy_or_absent_runner_blocks_a_plan(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    h.hub.active = {"call_id": 9, "method": "up", "cluster": "demo", "started_at": "t"}
    await command(h, run_id, "refresh", "f1")
    assert (await h.service.snapshot("alice", run_id))["last_check"]["idle"] is False
    with pytest.raises(RunError) as err:
        await command(h, run_id, "review_recovery", "v1")
    assert err.value.code == "check_needed"
    h.hub.connected_instance = None
    with pytest.raises(RunError) as err:
        await command(h, run_id, "refresh", "f2")
    assert err.value.code == "runner_offline"


async def test_a_restarted_runner_needs_acknowledging_and_pins_the_new_instance(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    h.hub.connected_instance = "i-2"
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    plan = (await h.service.snapshot("alice", run_id))["pending"]["payload"]
    assert plan["required"] == [INSTANCE_CHANGED]
    with pytest.raises(RunError) as err:
        await answer(h, run_id, {"approve": True}, command_id="r1")
    assert err.value.status == 422
    await answer(h, run_id, {"approve": True, "acknowledged": [INSTANCE_CHANGED]}, command_id="r2")
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "healthy"
    async with store.read() as t:
        assert (await t.get_run(run_id))["execution_instance"] == "i-2"


async def test_a_plan_is_withdrawn_when_its_facts_change(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    stale = await h.service.snapshot("alice", run_id)
    h.hub.connected_instance = "i-2"
    await command(h, run_id, "refresh", "f2")  # finds another instance
    snap = await h.service.snapshot("alice", run_id)
    assert snap["state"] == "reconcile" and snap["revision"] == stale["revision"] + 1
    with pytest.raises(RunError) as err:
        body = {
            "command_id": "r1",
            "expected_revision": stale["revision"],
            "action": "answer",
            "pending_id": stale["pending"]["pending_id"],
            "decision": {"approve": True},
        }
        await h.service.submit("alice", run_id, body)
    assert err.value.code == "not_waiting"


async def test_an_approval_rechecks_the_runner_first(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    h.hub.connected_instance = "i-2"  # restarted after the plan, before the approval
    with pytest.raises(RunError) as err:
        await answer(h, run_id, {"approve": True}, command_id="r1")
    assert (err.value.status, err.value.code) == (409, "plan_stale")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    assert h.sim.calls.count("up") == 1


async def test_declining_needs_no_runner_and_keeps_the_lock(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    h.hub.connected_instance = None
    await answer(h, run_id, {"approve": False}, command_id="r1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    with pytest.raises(RunError) as err:
        await h.create(command_id="c2")
    assert err.value.code == "cluster_busy"


async def test_abandoning_keeps_the_mark_for_the_next_run(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "abandon", "x1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "abandoned"
    [mark] = (await cluster(store))["marks"]

    # A new run on the marked cluster inherits the mark and starts in reconcile.
    new = (await h.create(command_id="c2")).body["run_id"]
    snap = await h.service.snapshot("alice", new)
    assert snap["state"] == "reconcile" and snap["reconcile"]["reason"] == "cluster_marked"
    await command(h, new, "refresh", "f1")
    await command(h, new, "review_recovery", "v1")
    plan = (await h.service.snapshot("alice", new))["pending"]["payload"]
    assert plan["mode"] == "continue_cluster" and plan["required"] == [PREVIOUS_UNCONFIRMED]
    await answer(h, new, {"approve": True, "acknowledged": [PREVIOUS_UNCONFIRMED]}, command_id="r1")

    # It still needs the normal approval, and waiting there clears nothing.
    assert (await h.service.snapshot("alice", new))["pending"]["kind"] == "approval"
    assert (await cluster(store))["marks"] == [mark]
    await answer(h, new, {"approve": True}, command_id="r2")
    assert (await h.service.snapshot("alice", new))["state"] == "healthy"
    assert (await cluster(store))["marks"] == []


async def test_a_recovery_that_fails_again_keeps_the_marks(store, catalog):
    class AlwaysLost(SimCluster):
        def _up(self, p):
            super()._up(p)
            raise RunnerUnavailable("gone again")

    h, run_id = await interrupted_run(store, catalog, AlwaysLost())
    await command(h, run_id, "refresh", "f1")
    await command(h, run_id, "review_recovery", "v1")
    await answer(h, run_id, {"approve": True}, command_id="r1")
    assert (await h.service.snapshot("alice", run_id))["state"] == "reconcile"
    assert len((await cluster(store))["marks"]) == 2  # the old one and the new one


async def test_a_run_without_a_compose_cannot_be_resumed(store, catalog):
    h, run_id = await interrupted_run(store, catalog)
    await command(h, run_id, "refresh", "f1")

    async def no_compose(_thread):
        return {"compose": None}, "cp"

    h.service.orch.checkpoint = no_compose
    with pytest.raises(RunError) as err:
        await command(h, run_id, "review_recovery", "v1")
    assert err.value.code == "no_compose"
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_runs_recovery.py -q
```

Expected:

```
9 failed
```

The tests fail: the recovery actions do not exist.

**Step 3: Implement**

In `platform/src/cagent/orchestrator.py`, replace:

```python
def _service_summary(s: dict[str, Any]) -> dict[str, Any]:
```

with:

```python
def service_summary(s: dict[str, Any]) -> dict[str, Any]:
```

In `platform/src/cagent/orchestrator.py`, replace:

```python
            services = [_service_summary(s) for s in out["services"]]
```

with:

```python
            services = [service_summary(s) for s in out["services"]]
```

In `platform/src/cagent/runs.py`, replace:

```python
from cagent.hub import RunnerHub
from cagent.orchestrator import EventSink, Orchestrator, Status
```

with:

```python
from cagent.hub import RunnerHub, status
from cagent.orchestrator import EventSink, Orchestrator, Status, service_summary
from cagent.recovery import RESUME, build_plan, facts_of, make_recovery_initial
```

In `platform/src/cagent/runs.py`, replace:

```python
        self._runner_lock = asyncio.Lock()
```

with:

```python
        self._runner_lock = asyncio.Lock()
        catalog = orchestrator.catalog
        self.catalog_digest = digest(
            {
                "services": {k: v.model_dump(mode="json") for k, v in catalog.services.items()},
                "middleware": {k: v.model_dump(mode="json") for k, v in catalog.middleware.items()},
            }
        )
```

In `platform/src/cagent/runs.py`, replace:

```python
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await self._current(t, run_id, env, "waiting")
            pending = run["pending"]
            if env.pending_id != pending["pending_id"]:
                raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
            decision = check_decision(pending["kind"], pending["payload"], env.decision or {})
            now = self.clock()
            if pending["kind"] in ("approval", "patch_approval"):
                await self._record_approval(t, user, run_id, pending, decision, now)
            execution_id = self.new_id()
            instance = self.hub.instance(run["runner"])
            body = await self._accept(
                t, user, run, env.command_id, dig, now, execution_id=execution_id
            )
            await t.update_run(
                run_id,
                pending=None,
                execution_id=execution_id,
                executions=run["executions"] + 1,
                execution_instance=instance,
            )
        thread = run["thread_id"]
        self._launch(
            run_id,
            execution_id,
            instance,
            (user, env.command_id),
            lambda sink: self.orch.resume(thread, decision, on_event=sink),
        )
        return Accepted(body)
```

with:

```python
        async with self.store.read() as t:
            run = await self._waiting(t, run_id, env)
            pending = run["pending"]
            decision = check_decision(pending["kind"], pending["payload"], env.decision or {})
        plan = None
        if pending["kind"] == "recovery":
            plan = await self._plan(pending["payload"]["plan_id"])
            if decision["approve"]:
                await self._confirm_plan(run, plan)  # outside any transaction
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await self._waiting(t, run_id, env)  # again, inside Tx1
            pending, now = run["pending"], self.clock()
            if pending["kind"] in ("approval", "patch_approval", "recovery"):
                await self._record_approval(t, user, run_id, pending, decision, now)
            if plan is not None and not decision["approve"]:
                await t.update("plans", {"id": plan["id"]}, invalidated_at=now)
                body = await self._accept(
                    t, user, run, env.command_id, dig, now, None, "reconcile", "done"
                )
                return Accepted(body)
            execution_id = self.new_id()
            extra: dict[str, Any] = {}
            if plan is not None:
                thread = f"{run_id}.{run['executions'] + 1}"
                instance = plan["facts"]["instance_id"]  # the instance the plan checked
                last = await t.one("SELECT MAX(call_id) AS n FROM dispatches")
                extra = {
                    "thread_id": thread,
                    "recovery": {
                        "plan_id": plan["id"],
                        "marks": plan["content"]["marks"],
                        "after_call_id": last["n"] or 0,
                    },
                }
            else:
                thread, instance = run["thread_id"], self.hub.instance(run["runner"])
            body = await self._accept(t, user, run, env.command_id, dig, now, execution_id)
            await t.update_run(
                run_id,
                execution_id=execution_id,
                executions=run["executions"] + 1,
                execution_instance=instance,
                **extra,
            )
        if plan is not None:
            work = self._recovery_work(run, plan, thread)
        else:
            work = lambda sink: self.orch.resume(thread, decision, on_event=sink)  # noqa: E731
        self._launch(run_id, execution_id, instance, (user, env.command_id), work)
        return Accepted(body)

    async def _waiting(self, t, run_id: str, env: Envelope) -> dict[str, Any]:
        run = await self._current(t, run_id, env, "waiting")
        if env.pending_id != run["pending"]["pending_id"]:
            raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
        return run
```

In `platform/src/cagent/runs.py`, replace:

```python
    ) -> dict[str, Any]:
        """Record an accepted command and move the run to state, in the caller's Tx1."""
        revision = await t.bump_revision(run["id"])
```

with:

```python
        pending: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an accepted command and move the run to state with pending (or none), in
        the caller's Tx1."""
        revision = await t.bump_revision(run["id"])
        if pending is not None:
            pending = {**pending, "revision": revision}
```

In `platform/src/cagent/runs.py`, replace:

```python
        await t.update_run(run["id"], state=state)
        await t.append_event(
            run["id"], "run_state", {"state": state, "revision": revision, "pending_id": None}
        )
        return body
```

with:

```python
        await t.update_run(run["id"], state=state, pending=pending)
        pending_id = pending["pending_id"] if pending else None
        await t.append_event(
            run["id"], "run_state", {"state": state, "revision": revision, "pending_id": pending_id}
        )
        return body

    # --- reconciliation and recovery --------------------------------------------------------

    def _reconcilable(self, run: dict[str, Any]) -> bool:
        pending = run["pending"]
        return run["state"] == "reconcile" or (
            run["state"] == "waiting" and pending is not None and pending["kind"] == "recovery"
        )

    @staticmethod
    def _busy(report: dict[str, Any], cluster: str) -> bool:
        active = report.get("active")
        return bool(active and active.get("cluster") == cluster) or bool(
            (report.get("queued") or {}).get(cluster)
        )

    async def _refresh(self, user: str, run_id: str, env: Envelope, dig: str) -> Accepted:
        """A read-only check: is the Runner idle for this cluster, and what runs there now?
        It changes no state, unless what it finds invalidates the recovery plan."""
        async with self.store.read() as t:
            run = await t.get_run(run_id)
            if not self._reconcilable(run):
                raise RunError(409, "not_reconcile", snapshot=await self._snapshot_in(t, run))
        if self.hub.instance(run["runner"]) is None:
            raise RunError(409, "runner_offline")
        body = {
            "command_id": env.command_id,
            "run_id": run_id,
            "accepted_revision": run["revision"],
            "execution_id": None,
        }
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            await t.insert(
                "commands",
                user=user,
                command_id=env.command_id,
                run_id=run_id,
                kind="refresh",
                **{"class": "read_only"},
                digest=dig,
                response=body,
                status="accepted",
                at=self.clock(),
            )
        try:
            report = await status(self.hub, run["runner"])
        except Exception as e:
            async with self.store.tx() as t:
                await t.update(
                    "commands",
                    {"user": user, "command_id": env.command_id},
                    status="interrupted",
                    reason=f"{type(e).__name__}: {e}",
                )
            raise RunError(409, "runner_unavailable", str(e)) from None
        instance = report["instance_id"]
        idle = not self._busy(report, run["cluster"])
        check = {
            "at": self.clock(),
            "instance_id": instance,
            "idle": idle,
            "same_instance": (
                instance == run["execution_instance"] if run["execution_instance"] else None
            ),
        }
        observation, failure = None, None
        if idle:
            try:
                res = await self.hub.call(
                    run["runner"], "snapshot", {"name": run["cluster"]}, instance=instance
                )
                out = res.output
                if res.ok and isinstance(out, dict) and isinstance(out.get("services"), list):
                    services = [service_summary(s) for s in out["services"]]
                    observation = {"received_at": self.clock(), "services": services}
                else:
                    failure = f"snapshot exited {res.exit_code}: {(res.stderr or '')[-300:]}"
            except Exception as e:
                failure = f"{type(e).__name__}: {e}"
        async with self.store.tx() as t:
            run = await t.get_run(run_id)
            await t.update_run(run_id, last_check=check)
            if observation is not None:
                seq = await t.append_event(run_id, "observation", observation)
                observation = {**observation, "seq": seq}
                await t.update_run(
                    run_id,
                    last_observation=observation,
                    last_observation_attempt={"at": check["at"], "ok": True, "reason": None},
                )
            elif failure is not None:
                payload = {"received_at": check["at"], "reason": failure}
                await t.append_event(run_id, "observation_failed", payload)
                await t.update_run(
                    run_id,
                    last_observation_attempt={"at": check["at"], "ok": False, "reason": failure},
                )
            if run["state"] == "waiting" and run["pending"]["kind"] == "recovery":
                plan = await t.one(
                    "SELECT * FROM plans WHERE id = ?", run["pending"]["payload"]["plan_id"]
                )
                seen = observation or run["last_observation"]
                now_facts = {**plan["facts"], **facts_of(check, seen)}
                if not idle or now_facts != plan["facts"]:
                    await self._invalidate(t, run, plan["id"])
            await t.update("commands", {"user": user, "command_id": env.command_id}, status="done")
        return Accepted(body)

    async def _invalidate(self, t, run: dict[str, Any], plan_id: str) -> None:
        """The plan no longer matches the facts: withdraw it and return to reconcile."""
        await t.update("plans", {"id": plan_id}, invalidated_at=self.clock())
        revision = await t.bump_revision(run["id"])
        await t.update_run(run["id"], state="reconcile", pending=None)
        await t.append_event(
            run["id"], "run_state", {"state": "reconcile", "revision": revision, "pending_id": None}
        )

    async def _review_recovery(self, user: str, run_id: str, env: Envelope, dig: str) -> Accepted:
        """Build a recovery plan from records and publish it for approval."""
        async with self.store.read() as t:
            run = await self._current(t, run_id, env, "reconcile")
        check = run["last_check"]
        if not check or not check["idle"]:
            raise RunError(409, "check_needed", "refresh while the Runner is idle first")
        values, source = None, None
        if run["executions"] > 0:  # resume the run's own execution, which needs a compose
            values, checkpoint_id = await self.orch.checkpoint(run["thread_id"])
            if not values.get("compose"):
                raise RunError(409, "no_compose", "abandon this run and start a new one")
            source = {"thread_id": run["thread_id"], "checkpoint_id": checkpoint_id}
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await self._current(t, run_id, env, "reconcile")
            approved = await t.one(
                "SELECT * FROM approvals WHERE run_id = ? AND kind = 'approval'"
                " AND json_extract(decision, '$.approve') = 1 ORDER BY at DESC LIMIT 1",
                run_id,
            )
            sent = await t.all(
                "SELECT method FROM dispatches WHERE execution_id = ?", run["execution_id"] or ""
            )
            cluster = await t.get_cluster(run["runner"], run["cluster"])
            plan = build_plan(
                values=values,
                last_approved=approved,
                check=run["last_check"],
                observation=run["last_observation"],
                dispatched=[d["method"] for d in sent],
                marks=cluster["marks"],
                catalog_digest=self.catalog_digest,
            )
            now, plan_id = self.clock(), self.new_id()
            content = {**plan.content, "source": source, "required": plan.required}
            await t.insert(
                "plans",
                id=plan_id,
                run_id=run_id,
                mode=plan.mode,
                digest=plan.digest,
                based_on_observation_seq=(run["last_observation"] or {}).get("seq", 0),
                facts=plan.facts,
                content=content,
                created_at=now,
            )
            pending = {
                "pending_id": self.new_id(),
                "kind": "recovery",
                "created_at": now,
                "payload": {**content, "plan_id": plan_id, "digest": plan.digest},
                "interrupt_id": None,
            }
            body = await self._accept(
                t,
                user,
                run,
                env.command_id,
                dig,
                now,
                execution_id=None,
                state="waiting",
                status="done",
                kind="review_recovery",
                pending=pending,
            )
        return Accepted(body)

    async def _plan(self, plan_id: str) -> dict[str, Any]:
        async with self.store.read() as t:
            return await t.one("SELECT * FROM plans WHERE id = ?", plan_id)

    async def _confirm_plan(self, run: dict[str, Any], plan: dict[str, Any]) -> None:
        """Before an approved recovery runs: the Runner must still be the instance the plan
        checked, and idle for this cluster. Otherwise the plan is withdrawn."""
        try:
            report = await status(self.hub, run["runner"])
        except Exception as e:
            raise RunError(409, "runner_unavailable", str(e)) from None
        if report["instance_id"] == plan["facts"]["instance_id"] and not self._busy(
            report, run["cluster"]
        ):
            return
        async with self.store.tx() as t:
            current_run = await t.get_run(run["id"])
            if current_run["revision"] == run["revision"]:
                await self._invalidate(t, current_run, plan["id"])
        raise RunError(409, "plan_stale", "the Runner changed since the plan was made")

    def _recovery_work(self, run: dict[str, Any], plan: dict[str, Any], thread: str) -> Work:
        content = plan["content"]
        if plan["mode"] != RESUME:  # a new run on a marked cluster starts normally
            inp = run["input"]
            return self._start_work(
                thread, run["runner"], run["cluster"], inp.get("purpose"), inp.get("spec")
            )

        async def work(sink: EventSink) -> Status:
            values, _ = await self.orch.checkpoint(content["source"]["thread_id"])
            deadline = self.clock() + self.orch.settings.deadline_s
            initial = make_recovery_initial(content, values, deadline, content["source"])
            return await self.orch.start_recovery(thread, initial, on_event=sink)

        return work

    async def _abandon(self, user: str, run_id: str, env: Envelope, dig: str) -> Accepted:
        """End orchestration. Containers, volumes, and the cluster's marks stay as they are."""
        async with self.store.tx() as t:
            if replay := await self._replay(t, user, env.command_id, dig):
                return replay
            run = await t.get_run(run_id)
            if not self._reconcilable(run):
                raise RunError(409, "not_reconcile", snapshot=await self._snapshot_in(t, run))
            if env.expected_revision != run["revision"]:
                raise RunError(409, "stale", snapshot=await self._snapshot_in(t, run))
            now = self.clock()
            if run["pending"]:
                plan_id = run["pending"]["payload"]["plan_id"]
                await t.update("plans", {"id": plan_id}, invalidated_at=now)
            body = await self._accept(
                t, user, run, env.command_id, dig, now, None, "abandoned", "done", "abandon"
            )
            await t.update(
                "clusters",
                {"runner": run["runner"], "name": run["cluster"], "lock_run_id": run_id},
                lock_run_id=None,
            )
        return Accepted(body)
```

In `platform/src/cagent/runs.py`, replace:

```python
                    values["verified_at"] = now
```

with:

```python
                    values["verified_at"] = now
                    if run["recovery"]:
                        await self._clear_marks(t, run)
```

In `platform/src/cagent/runs.py`, replace:

```python
    @staticmethod
    async def _previous_approved(t, run_id: str) -> dict[str, Any] | None:
```

with:

```python
    @staticmethod
    async def _clear_marks(t, run: dict[str, Any]) -> None:
        """A recovery chain passed acceptance: clear the marks its plan named, unless one of
        its own mutating calls is still unconfirmed. Marks added meanwhile stay."""
        rec = run["recovery"]
        marks = ", ".join("?" for _ in MUTATING)
        open_ = await t.one(
            "SELECT COUNT(*) AS n FROM dispatches WHERE run_id = ? AND call_id > ?"
            f" AND result_at IS NULL AND method IN ({marks})",
            run["id"],
            rec["after_call_id"],
            *sorted(MUTATING),
        )
        if open_["n"]:
            return
        cluster = await t.get_cluster(run["runner"], run["cluster"])
        kept = [m for m in cluster["marks"] if m["id"] not in rec["marks"]]
        await t.update("clusters", {"runner": run["runner"], "name": run["cluster"]}, marks=kept)

    @staticmethod
    async def _previous_approved(t, run_id: str) -> dict[str, Any] | None:
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
373 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/orchestrator.py platform/src/cagent/runs.py platform/tests/test_runs_recovery.py
git commit -F - <<'EOF'
feat(platform): reconcile runs through refresh, recovery plans, and abandon

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 12 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 13: Configuration: `platform.yaml`

`cagent.config.load_config` reads the file strictly and the tokens from the environment, and refuses missing, short, or repeated tokens, unknown Runners, and an origin that does not match how cookies will be sent (design section 10).

**Files:**

- Create: `platform/src/cagent/config.py`
- Test (create): `platform/tests/test_config.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_config.py`:

```python
"""platform.yaml: strict loading, tokens from the environment, origin and cookie rules."""

import pytest
import yaml

from cagent.config import ConfigError, check_origin, load_config

ENV = {"RUNNER_1": "r" * 32, "ALICE": "a" * 32, "BOB": "b" * 32}


def config(tmp_path, env=ENV, **web):
    doc = {
        "db": {"path": "data/cagent.db"},
        "catalog": ["../catalog"],
        "hub": {"runners": {"laptop-1": {"token_env": "RUNNER_1"}}},
        "web": {"origin": "http://127.0.0.1:8000", "dev_http": True, **web},
        "users": {
            "alice": {"token_env": "ALICE", "runners": ["laptop-1"]},
            "bob": {"token_env": "BOB", "runners": []},
        },
    }
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(doc))
    return load_config(path, env)


def test_a_local_development_config_loads(tmp_path):
    c = config(tmp_path)
    assert c.runner_tokens == {"r" * 32: "laptop-1"}
    assert c.user_tokens == {"alice": "a" * 32, "bob": "b" * 32}
    assert c.user_runners == {"alice": {"laptop-1"}, "bob": set()}
    assert c.db_path == tmp_path / "data/cagent.db" and c.catalog == [tmp_path / "../catalog"]
    assert (c.web_host, c.web_port, c.secure_cookies) == ("127.0.0.1", 8000, False)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({**ENV, "ALICE": ""}, "ALICE is missing or empty"),
        ({k: v for k, v in ENV.items() if k != "BOB"}, "BOB is missing or empty"),
        ({**ENV, "ALICE": "short"}, "shorter than 32"),
        ({**ENV, "BOB": "a" * 32}, "same token"),
        ({**ENV, "ALICE": "r" * 32}, "same token"),
    ],
)
def test_tokens_must_exist_be_long_and_be_unique(tmp_path, env, message):
    with pytest.raises(ConfigError, match=message):
        config(tmp_path, env)


def test_a_user_names_only_known_runners(tmp_path):
    path = tmp_path / "platform.yaml"
    config(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["users"]["bob"]["runners"] = ["laptop-9"]
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ConfigError, match="unknown runners"):
        load_config(path, ENV)


@pytest.mark.parametrize(
    ("web", "message"),
    [
        ({"origin": "https://cagent.example.com"}, "needs web.secure_cookies"),
        ({"secure_cookies": True}, "needs an https origin"),
        ({"dev_http": False}, "only for local development"),
        ({"origin": "http://cagent.example.com"}, "only for local development"),
        ({"host": "0.0.0.0"}, "only for local development"),
        ({"origin": "http://127.0.0.1:8000/app"}, "exactly scheme://host"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_the_origin_matches_how_cookies_are_sent(tmp_path, web, message):
    with pytest.raises(ConfigError, match=message):
        config(tmp_path, **web)


def test_https_behind_a_proxy_is_allowed(tmp_path):
    c = config(tmp_path, origin="https://cagent.example.com", secure_cookies=True, dev_http=False)
    assert c.origin == "https://cagent.example.com" and c.secure_cookies


@pytest.mark.parametrize(
    "origin",
    ["ftp://h", "https://", "https://h/path", "https://h?q=1", "https://h#f", "https://u:p@h"],
)
def test_an_origin_is_exactly_scheme_host_port(origin):
    with pytest.raises(ConfigError):
        check_origin(origin)
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_config.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.config`.

**Step 3: Implement**

Create `platform/src/cagent/config.py`:

```python
"""platform.yaml: where the platform listens, who may use it, and which Runners exist.

Secrets are never in the file: each token is read from the environment variable it names.
Loading fails on anything that would make access ambiguous or unsafe: a missing or short
token, two equal tokens anywhere, a user naming an unknown Runner, or a web origin that
does not match how cookies will be sent."""

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MIN_TOKEN = 32  # characters; tokens are random strings, not passwords


class ConfigError(Exception):
    """platform.yaml or the environment is not usable."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Runner(_Strict):
    token_env: str


class _Hub(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765
    runners: dict[str, _Runner]


class _Web(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    origin: str
    secure_cookies: bool = False
    dev_http: bool = False  # plain http, allowed only on this machine
    session_ttl_s: float = Field(default=12 * 3600, gt=0)
    idle_ttl_s: float = Field(default=3600, gt=0)


class _User(_Strict):
    token_env: str
    runners: list[str]


class _Db(_Strict):
    path: str


class _File(_Strict):
    db: _Db
    catalog: list[str]
    models: str | None = None
    hub: _Hub
    web: _Web
    users: dict[str, _User]


@dataclass(frozen=True)
class PlatformConfig:
    db_path: Path
    catalog: list[Path]
    models: Path | None
    hub_host: str
    hub_port: int
    runner_tokens: dict[str, str]  # token -> runner id
    web_host: str
    web_port: int
    origin: str
    secure_cookies: bool
    session_ttl_s: float
    idle_ttl_s: float
    user_tokens: dict[str, str]  # user -> token
    user_runners: dict[str, set[str]]


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_origin(origin: str) -> str:
    """An origin is exactly scheme://host[:port]."""
    parts = urlsplit(origin)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigError(f"web.origin {origin!r} must be exactly scheme://host[:port]")
    return origin


def load_config(path: str | Path, environ: Mapping[str, str] = os.environ) -> PlatformConfig:
    path = Path(path)
    try:
        raw = _File.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"{path}: {e}") from None
    base = path.parent

    seen: dict[str, str] = {}

    def token(owner: str, var: str) -> str:
        value = environ.get(var, "")
        if not value:
            raise ConfigError(f"{owner}: environment variable {var} is missing or empty")
        if len(value) < MIN_TOKEN:
            raise ConfigError(f"{owner}: the token in {var} is shorter than {MIN_TOKEN} characters")
        if value in seen:
            raise ConfigError(f"{owner} and {seen[value]} have the same token")
        seen[value] = owner
        return value

    runner_tokens = {token(f"runner {r}", c.token_env): r for r, c in raw.hub.runners.items()}
    user_tokens = {u: token(f"user {u}", c.token_env) for u, c in raw.users.items()}
    for user, c in raw.users.items():
        if unknown := set(c.runners) - set(raw.hub.runners):
            raise ConfigError(f"user {user}: unknown runners {sorted(unknown)}")

    web = raw.web
    origin = check_origin(web.origin)
    scheme = urlsplit(origin).scheme
    if scheme == "https" and not web.secure_cookies:
        raise ConfigError("an https origin needs web.secure_cookies: true")
    if scheme == "http":
        if web.secure_cookies:
            raise ConfigError("web.secure_cookies needs an https origin")
        if not (web.dev_http and _loopback(urlsplit(origin).hostname) and _loopback(web.host)):
            raise ConfigError(
                "plain http is only for local development: set web.dev_http and keep the "
                "origin and web.host on loopback; otherwise serve https"
            )
    return PlatformConfig(
        db_path=(base / raw.db.path),
        catalog=[base / c for c in raw.catalog],
        models=(base / raw.models) if raw.models else None,
        hub_host=raw.hub.host,
        hub_port=raw.hub.port,
        runner_tokens=runner_tokens,
        web_host=web.host,
        web_port=web.port,
        origin=origin,
        secure_cookies=web.secure_cookies,
        session_ttl_s=web.session_ttl_s,
        idle_ttl_s=web.idle_ttl_s,
        user_tokens=user_tokens,
        user_runners={u: set(c.runners) for u, c in raw.users.items()},
    )
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
394 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/config.py platform/tests/test_config.py
git commit -F - <<'EOF'
feat(platform): load platform.yaml with tokens from the environment

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 14: Browser sessions

`cagent.auth.Sessions`: a pre-session protects the login form, login compares against every user's token in constant time and issues a new random session, sessions expire when idle or old, reading never counts as use, logout revokes at once (an `asyncio.Event` that streams wait on), and failures and pre-sessions are rate-limited per client.

**Files:**

- Create: `platform/src/cagent/auth.py`
- Test (create): `platform/tests/test_auth.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_auth.py`:

```python
"""Sessions: login through a pre-session, expiry, logout, CSRF, and rate limits."""

import pytest

from cagent.auth import LoginError, RateLimited, Sessions

ALICE = "a" * 32


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def sessions(clock, **kw):
    return Sessions({"alice": ALICE, "bob": "b" * 32}, ttl_s=100, idle_ttl_s=10, clock=clock, **kw)


def login(s, client="1.2.3.4", token=ALICE):
    pre = s.new_pre(client)
    return s.login(pre.id, pre.csrf, token, client)


def test_login_exchanges_a_pre_session_for_a_new_session(clock):
    s = sessions(clock)
    pre = s.new_pre("c")
    session = s.login(pre.id, pre.csrf, ALICE, "c")
    assert session.user == "alice" and session.id not in (pre.id, ALICE)
    assert session.csrf != pre.csrf and s.get(session.id) is session
    assert pre.id not in s.pre
    with pytest.raises(LoginError):  # a pre-session is used once
        s.login(pre.id, pre.csrf, ALICE, "c")


@pytest.mark.parametrize("case", ["no pre", "wrong csrf", "wrong token", "expired pre"])
def test_every_login_failure_looks_the_same(clock, case):
    s = sessions(clock)
    pre = s.new_pre("c")
    args = {
        "no pre": (None, pre.csrf, ALICE),
        "wrong csrf": (pre.id, "x", ALICE),
        "wrong token": (pre.id, pre.csrf, "x" * 32),
        "expired pre": (pre.id, pre.csrf, ALICE),
    }[case]
    if case == "expired pre":
        clock.now = 601
    with pytest.raises(LoginError, match="^login failed$"):
        s.login(*args, "c")


def test_sessions_expire_when_idle_or_too_old(clock):
    s = sessions(clock)
    session = login(s)
    clock.now = 9
    s.touch(session)
    clock.now = 18
    assert s.get(session.id) is session  # 9 s since last use
    clock.now = 19.5
    assert s.get(session.id) is None and session.revoked.is_set()

    session = login(s)  # at 19.5, so it ends at 119.5 however busy it is
    for t in range(25, 120, 5):
        clock.now = t
        assert s.get(session.id) is session
        s.touch(session)
    clock.now = 120
    assert s.get(session.id) is None


def test_reading_does_not_keep_a_session_alive(clock):
    s = sessions(clock)
    session = login(s)
    for t in range(1, 10):
        clock.now = t
        assert s.get(session.id) is session  # get never touches
    clock.now = 10
    assert s.get(session.id) is None


def test_logout_revokes_at_once(clock):
    s = sessions(clock)
    session = login(s)
    s.logout(session.id)
    assert session.revoked.is_set() and s.get(session.id) is None


def test_csrf_tokens_are_compared_exactly(clock):
    s = sessions(clock)
    session = login(s)
    assert Sessions.csrf_ok(session, session.csrf)
    assert not Sessions.csrf_ok(session, None) and not Sessions.csrf_ok(session, "x")


def test_failed_logins_are_rate_limited_per_client(clock):
    s = sessions(clock, max_failures=2)
    for _ in range(2):
        with pytest.raises(LoginError):
            login(s, token="x" * 32)
    with pytest.raises(RateLimited):
        login(s)  # even the right token, for a while
    assert login(s, client="5.6.7.8").user == "alice"
    clock.now = 61
    assert login(s).user == "alice"


def test_pre_sessions_are_bounded(clock):
    s = sessions(clock, max_pre_per_client=2, max_pre=3)
    s.new_pre("c1")
    s.new_pre("c1")
    with pytest.raises(RateLimited):
        s.new_pre("c1")
    s.new_pre("c2")
    with pytest.raises(RateLimited):
        s.new_pre("c3")  # the table is full
    clock.now = 601  # expired pre-sessions make room
    s.new_pre("c3")
    assert len(s.pre) == 1
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_auth.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.auth`.

**Step 3: Implement**

Create `platform/src/cagent/auth.py`:

```python
"""Browser sessions for static users. The authentication step (a user token posted to the
login form) is replaceable; user ids, run ownership, and authorization stay the same when
SSO replaces it.

Sessions live in memory: a restart logs everyone out, and runs are unaffected. A session id
is random and never derived from the token; the cookie holds only the id. Login itself is
protected against CSRF with a short-lived pre-session, and both failures and pre-session
creation are rate-limited per client so the tables cannot grow without bound.
"""

import asyncio
import hmac
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field


class LoginError(Exception):
    """Login failed. The message is the same whatever the reason."""


class RateLimited(Exception):
    """Too many attempts from one client."""


@dataclass
class PreSession:
    id: str
    csrf: str
    created: float


@dataclass
class Session:
    id: str
    user: str
    csrf: str
    created: float
    last_seen: float
    # Set on logout or when found expired; open event streams close on it.
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class _Window:
    """At most limit events per client within window seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = {}

    def full(self, client: str, now: float) -> bool:
        hits = self.hits.get(client)
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if hits is not None and not hits:
            del self.hits[client]
            return False
        return hits is not None and len(hits) >= self.limit

    def add(self, client: str, now: float) -> None:
        self.hits.setdefault(client, deque()).append(now)


class Sessions:
    def __init__(
        self,
        tokens: Mapping[str, str],
        ttl_s: float,
        idle_ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
        pre_ttl_s: float = 600,
        max_pre: int = 1000,
        max_failures: int = 5,
        max_pre_per_client: int = 30,
        window_s: float = 60,
    ):
        """tokens maps each user to their login token."""
        self.tokens = dict(tokens)
        self.ttl, self.idle, self.clock = ttl_s, idle_ttl_s, clock
        self.pre_ttl, self.max_pre = pre_ttl_s, max_pre
        self.pre: dict[str, PreSession] = {}
        self.sessions: dict[str, Session] = {}
        self._failures = _Window(max_failures, window_s)
        self._pre_rate = _Window(max_pre_per_client, window_s)

    # --- login -------------------------------------------------------------------------------

    def new_pre(self, client: str) -> PreSession:
        now = self.clock()
        if self._pre_rate.full(client, now):
            raise RateLimited(client)
        self._pre_rate.add(client, now)
        for pid in [p.id for p in self.pre.values() if now - p.created > self.pre_ttl]:
            del self.pre[pid]
        if len(self.pre) >= self.max_pre:
            raise RateLimited("too many pending logins")
        pre = PreSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), now)
        self.pre[pre.id] = pre
        return pre

    def login(self, pre_id: str | None, csrf: str, token: str, client: str) -> Session:
        """Exchange a valid pre-session, its CSRF token, and a user token for a new session.
        The pre-session is used up either way."""
        now = self.clock()
        if self._failures.full(client, now):
            raise RateLimited(client)
        pre = self.pre.pop(pre_id, None) if pre_id else None
        valid_pre = (
            pre is not None
            and now - pre.created <= self.pre_ttl
            and hmac.compare_digest(pre.csrf.encode(), csrf.encode())
        )
        user = None
        for name, expected in self.tokens.items():  # compare against every user, always
            if hmac.compare_digest(expected.encode(), token.encode()):
                user = name
        if not valid_pre or user is None:
            self._failures.add(client, now)
            raise LoginError("login failed")
        session = Session(secrets.token_urlsafe(32), user, secrets.token_urlsafe(32), now, now)
        self.sessions[session.id] = session
        return session

    def logout(self, session_id: str) -> None:
        if session := self.sessions.pop(session_id, None):
            session.revoked.set()

    # --- use ---------------------------------------------------------------------------------

    def deadline(self, session: Session) -> float:
        """When the session expires unless it is used again."""
        return min(session.created + self.ttl, session.last_seen + self.idle)

    def get(self, session_id: str | None) -> Session | None:
        """The live session, without counting this as use. An expired one is revoked."""
        session = self.sessions.get(session_id) if session_id else None
        if session is None:
            return None
        if self.clock() >= self.deadline(session):
            self.logout(session.id)
            return None
        return session

    def touch(self, session: Session) -> None:
        """A request the user made counts as use; event-stream traffic does not."""
        session.last_seen = self.clock()

    @staticmethod
    def csrf_ok(session: Session, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(
            session.csrf.encode(), presented.encode()
        )
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
405 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/auth.py platform/tests/test_auth.py
git commit -F - <<'EOF'
feat(platform): browser sessions with login CSRF, expiry, and rate limits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 15: The JSON API

`cagent.api.create_app` maps HTTP to RunService: login and logout, runs, commands, and command status. Every route checks the session; every change also checks `X-CSRF-Token` and the exact `Origin`. Errors are `{error, detail?, snapshot?}`; a repeated command answers with `Cagent-Replay: true`.

**Files:**

- Create: `platform/src/cagent/api.py`
- Test (create): `platform/tests/test_api.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_api.py`:

```python
"""The HTTP interface: login, CSRF and Origin checks, access, and the run commands."""

import httpx
import pytest

from cagent.api import create_app
from cagent.auth import Sessions
from cagent.store import Store

from .runharness import REDIS, Harness
from .simcluster import SimCluster

ORIGIN = "http://127.0.0.1:8000"
TOKENS = {"alice": "a" * 32, "bob": "b" * 32}


@pytest.fixture
async def h(tmp_path, catalog):
    store = await Store.open(str(tmp_path / "cagent.db"))
    h = Harness(store, SimCluster(), catalog)
    h.sessions = Sessions(TOKENS, ttl_s=3600, idle_ttl_s=600)
    h.app = create_app(h.service, h.sessions, ORIGIN, secure_cookies=False)
    yield h
    await store.close()


def client(h) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=ORIGIN)


async def login(c: httpx.AsyncClient, token=TOKENS["alice"]) -> dict:
    csrf = (await c.get("/login")).json()["csrf"]
    r = await c.post("/login", data={"token": token, "csrf": csrf}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    return {"Origin": ORIGIN, "X-CSRF-Token": r.json()["csrf"]}


async def test_login_sets_only_a_session_cookie(h):
    async with client(h) as c:
        r = await c.get("/login")
        assert "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        headers = await login(c)
        assert set(c.cookies.keys()) == {"cagent_session"}
        assert TOKENS["alice"] not in c.cookies["cagent_session"]
        assert (await c.get("/api/runs")).json() == []
        assert headers["X-CSRF-Token"]


@pytest.mark.parametrize(
    ("origin", "status"), [(None, 403), ("null", 403), ("http://evil.example", 403)]
)
async def test_login_needs_the_exact_origin(h, origin, status):
    async with client(h) as c:
        csrf = (await c.get("/login")).json()["csrf"]
        headers = {"Origin": origin} if origin else {}
        r = await c.post("/login", data={"token": TOKENS["alice"], "csrf": csrf}, headers=headers)
        assert r.status_code == status


async def test_login_needs_the_pre_session_and_the_token(h):
    async with client(h) as c:
        csrf = (await c.get("/login")).json()["csrf"]
        r = await c.post(
            "/login", data={"token": "x" * 32, "csrf": csrf}, headers={"Origin": ORIGIN}
        )
        assert (r.status_code, r.json()) == (401, {"error": "login_failed"})
    async with client(h) as c:  # a forged form without the victim's pre-session
        r = await c.post(
            "/login", data={"token": TOKENS["alice"], "csrf": csrf}, headers={"Origin": ORIGIN}
        )
        assert r.status_code == 401


async def test_everything_needs_a_session(h):
    async with client(h) as c:
        for method, path in [("GET", "/api/runs"), ("GET", "/api/runs/x"), ("POST", "/api/runs")]:
            r = await c.request(method, path, headers={"Origin": ORIGIN})
            assert (r.status_code, r.json()) == (401, {"error": "login_required"})


async def test_changes_need_the_csrf_header_and_the_origin(h):
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    async with client(h) as c:
        headers = await login(c)
        for bad in [
            {"Origin": ORIGIN},
            {**headers, "X-CSRF-Token": "x"},
            {"X-CSRF-Token": headers["X-CSRF-Token"]},
            {**headers, "Origin": "null"},
        ]:
            r = await c.post("/api/runs", json=body, headers=bad)
            assert r.status_code == 403, bad
        assert (await c.post("/api/runs", json=body, headers=headers)).status_code == 202


async def test_a_run_goes_from_creation_to_healthy_over_http(h):
    async with client(h) as c:
        headers = await login(c)
        body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
        r = await c.post("/api/runs", json=body, headers=headers)
        assert r.status_code == 202 and "cagent-replay" not in r.headers
        run_id = r.json()["run_id"]
        again = await c.post("/api/runs", json=body, headers=headers)
        assert again.status_code == 202 and again.headers["cagent-replay"] == "true"
        assert again.json() == r.json()
        await h.settle()

        snap = (await c.get(f"/api/runs/{run_id}")).json()
        command = {
            "command_id": "a1",
            "expected_revision": snap["revision"],
            "action": "answer",
            "pending_id": snap["pending"]["pending_id"],
            "decision": {"approve": True},
        }
        stale = {**command, "command_id": "a0", "expected_revision": 1}
        r = await c.post(f"/api/runs/{run_id}/commands", json=stale, headers=headers)
        assert r.status_code == 409 and r.json()["error"] == "stale"
        assert r.json()["snapshot"]["revision"] == snap["revision"]
        r = await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)
        assert r.status_code == 202
        await h.settle()
        assert (await c.get(f"/api/runs/{run_id}")).json()["state"] == "healthy"
        done = (await c.get(f"/api/runs/{run_id}/commands/a1")).json()
        assert done["status"] == "done"


async def test_malformed_commands_get_422(h):
    async with client(h) as c:
        headers = await login(c)
        r = await c.post("/api/runs", json={"command_id": "c1"}, headers=headers)
        assert (r.status_code, r.json()["error"]) == (422, "invalid_command")
        r = await c.post("/api/runs/x/commands", content=b"not json", headers=headers)
        assert r.status_code == 422


async def test_another_users_run_does_not_exist_for_them(h):
    async with client(h) as alice, client(h) as bob:
        a = await login(alice)
        b = await login(bob, TOKENS["bob"])
        body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
        run_id = (await alice.post("/api/runs", json=body, headers=a)).json()["run_id"]
        await h.settle()
        assert (await bob.get(f"/api/runs/{run_id}")).status_code == 404
        r = await bob.post(
            f"/api/runs/{run_id}/commands",
            json={"command_id": "x", "action": "abandon", "expected_revision": 2},
            headers=b,
        )
        assert r.status_code == 404
        assert (await bob.get("/api/runs")).json() == []


async def test_logout_ends_the_session(h):
    async with client(h) as c:
        headers = await login(c)
        session = h.sessions.get(c.cookies["cagent_session"])
        assert (await c.post("/logout", headers=headers)).status_code == 200
        assert session.revoked.is_set()
        c.cookies.set("cagent_session", session.id)  # replaying the old cookie
        assert (await c.get("/api/runs")).status_code == 401
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_api.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.api`.

**Step 3: Implement**

Create `platform/src/cagent/api.py`:

```python
"""The platform's HTTP interface: login, runs, commands, and (cagent.events) event streams.

Every route checks the session; every state-changing route also checks the X-CSRF-Token
header against the session and the Origin header against the configured origin, exactly.
Login has its own CSRF check through a pre-session. Proxy headers are never consulted.
Routes only translate HTTP to RunService calls and back.
"""

from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cagent.auth import LoginError, RateLimited, Session, Sessions
from cagent.runs import Accepted, RunError, RunService

SESSION_COOKIE = "cagent_session"
PRE_COOKIE = "cagent_pre"


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=128)
    runner: str
    cluster: str
    purpose: str | None = None
    spec: dict[str, Any] | None = None


class Denied(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def _error(status: int, code: str, detail: str = "", snapshot: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": code}
    if detail:
        body["detail"] = detail
    if snapshot is not None:
        body["snapshot"] = snapshot
    return JSONResponse(body, status_code=status)


def _accepted(acc: Accepted) -> JSONResponse:
    headers = {"Cagent-Replay": "true"} if acc.replay else {}
    return JSONResponse(acc.body, status_code=202, headers=headers)


def create_app(
    service: RunService, sessions: Sessions, origin: str, secure_cookies: bool
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.service, app.state.sessions, app.state.origin = service, sessions, origin

    @app.exception_handler(RunError)
    async def run_error(_request: Request, e: RunError) -> JSONResponse:
        return _error(e.status, e.code, e.detail, e.snapshot)

    @app.exception_handler(Denied)
    async def denied(_request: Request, e: Denied) -> JSONResponse:
        return _error(e.status, e.code)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, e: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_command", str(e.errors()))

    def cookie(response: JSONResponse, name: str, value: str, max_age: float) -> None:
        response.set_cookie(
            name,
            value,
            max_age=int(max_age),
            httponly=True,
            samesite="strict",
            secure=secure_cookies,
            path="/",
        )

    def same_origin(request: Request) -> None:
        if request.headers.get("origin") != origin:  # missing and "null" never match
            raise Denied(403, "bad_origin")

    def session(request: Request) -> Session:
        s = sessions.get(request.cookies.get(SESSION_COOKIE))
        if s is None:
            raise Denied(401, "login_required")
        return s

    Live = Annotated[Session, Depends(session)]

    def user(request: Request, s: Live) -> str:
        sessions.touch(s)
        return s.user

    def changing(request: Request, s: Live) -> str:
        same_origin(request)
        if not Sessions.csrf_ok(s, request.headers.get("x-csrf-token")):
            raise Denied(403, "bad_csrf")
        sessions.touch(s)
        return s.user

    User = Annotated[str, Depends(user)]
    Changing = Annotated[str, Depends(changing)]

    def client(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- login -------------------------------------------------------------------------------

    @app.get("/login")
    async def login_form(request: Request) -> JSONResponse:
        try:
            pre = sessions.new_pre(client(request))
        except RateLimited:
            return _error(429, "rate_limited")
        response = JSONResponse({"csrf": pre.csrf})
        cookie(response, PRE_COOKIE, pre.id, sessions.pre_ttl)
        return response

    @app.post("/login")
    async def login(request: Request) -> JSONResponse:
        same_origin(request)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        token = (form.get("token") or [""])[0]
        csrf = (form.get("csrf") or [""])[0]
        try:
            s = sessions.login(request.cookies.get(PRE_COOKIE), csrf, token, client(request))
        except RateLimited:
            return _error(429, "rate_limited")
        except LoginError:
            return _error(401, "login_failed")
        response = JSONResponse({"user": s.user, "csrf": s.csrf})
        cookie(response, SESSION_COOKIE, s.id, sessions.ttl)
        response.delete_cookie(PRE_COOKIE, path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request, _user: Changing) -> JSONResponse:
        sessions.logout(request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # --- runs --------------------------------------------------------------------------------

    @app.get("/api/runs")
    async def list_runs(who: User) -> JSONResponse:
        return JSONResponse(await service.list_runs(who))

    @app.post("/api/runs")
    async def create_run(body: CreateRun, who: Changing) -> JSONResponse:
        acc = await service.create_run(
            who, body.command_id, body.runner, body.cluster, purpose=body.purpose, spec=body.spec
        )
        return _accepted(acc)

    @app.get("/api/runs/{run_id}")
    async def snapshot(run_id: str, who: User) -> JSONResponse:
        return JSONResponse(await service.snapshot(who, run_id))

    @app.post("/api/runs/{run_id}/commands")
    async def submit(run_id: str, request: Request, who: Changing) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return _error(422, "invalid_command", "the body is not JSON")
        if not isinstance(body, dict):
            return _error(422, "invalid_command", "the body is not an object")
        return _accepted(await service.submit(who, run_id, body))

    @app.get("/api/runs/{run_id}/commands/{command_id}")
    async def command(run_id: str, command_id: str, who: User) -> JSONResponse:
        return JSONResponse(await service.command(who, run_id, command_id))

    return app
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
416 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/api.py platform/tests/test_api.py
git commit -F - <<'EOF'
feat(platform): a JSON API for runs behind sessions, CSRF, and Origin checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 15 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 16: Event streams

`cagent.events.event_stream` pages stored events after a cursor, each page checked against what is kept; a gap ends the stream with `resync`. It waits on the store's notification generation, the session's revocation, and a heartbeat, and ends with `session_expired` on logout or expiry. `EventStreamResponse` sends unbuffered with a timeout and stops when the client disconnects. The races are arranged step by step in the tests.

**Files:**

- Modify: `platform/src/cagent/api.py`
- Create: `platform/src/cagent/events.py`
- Test (create): `platform/tests/test_events.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_events.py`:

```python
"""Event streams: paging from the store, resync on gaps, wake-ups, heartbeats, session ends.

Each race is arranged step by step with the generator itself, not with sleeps."""

import asyncio
import json

import pytest

from cagent.auth import Sessions
from cagent.events import BadCursor, StreamLimits, control, event_stream, parse_cursor
from cagent.store import Store

from .test_store import new_run

LIMITS = StreamLimits(page_events=2, heartbeat_s=30)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(str(tmp_path / "cagent.db"), keep_events=4)
    await new_run(s)
    yield s
    await s.close()


@pytest.fixture
def auth():
    clock = Clock()
    sessions = Sessions({"alice": "a" * 32}, ttl_s=1000, idle_ttl_s=100, clock=clock)
    pre = sessions.new_pre("c")
    return sessions, sessions.login(pre.id, pre.csrf, "a" * 32, "c"), clock


async def append(store, n=1, type="phase_started"):
    for _ in range(n):
        async with store.tx() as t:
            await t.append_event("r1", type, {"phase": "p"})


def seq_of(chunk: str) -> int:
    return json.loads(chunk.split("data: ", 1)[1])["seq"]


async def next_chunk(stream, timeout=2.0) -> str:
    async with asyncio.timeout(timeout):
        return await anext(stream)


def stream(store, auth, cursor=0, limits=LIMITS):
    sessions, session, _ = auth
    return event_stream(store, sessions, session, "r1", cursor, limits)


def test_cursors_are_non_negative_integers_and_last_event_id_wins():
    assert parse_cursor(None, None) == 0
    assert parse_cursor("7", "3") == 7 and parse_cursor(None, "3") == 3
    for bad in ["-1", "x", "1.5", ""]:
        with pytest.raises(BadCursor):
            parse_cursor(bad, None)


async def test_events_arrive_in_order_with_their_ids(store, auth):
    await append(store, 3)
    s = stream(store, auth)
    chunks = [await next_chunk(s) for _ in range(3)]
    assert [seq_of(c) for c in chunks] == [1, 2, 3]
    assert chunks[0].startswith("id: 1\nevent: phase_started\ndata: ")


async def test_a_gap_while_paging_ends_with_resync(store, auth):
    await append(store, 3)
    s = stream(store, auth)
    assert seq_of(await next_chunk(s)) == 1
    assert seq_of(await next_chunk(s)) == 2  # first page done
    await append(store, 4)  # keep 4: events 3 and 4 are trimmed before the next page
    assert await next_chunk(s) == control("resync")
    with pytest.raises(StopAsyncIteration):
        await anext(s)


@pytest.mark.parametrize("cursor", [1, 7])  # before the kept events, past the last one
async def test_a_cursor_outside_the_kept_events_resyncs_at_once(store, auth, cursor):
    await append(store, 6)  # kept: 3..6
    assert await next_chunk(stream(store, auth, cursor)) == control("resync")


async def test_a_commit_right_after_an_empty_read_is_not_missed(store, auth):
    class Racing(Store):
        """Commits an event right after a read finds nothing, before the reader waits."""

        raced = False

        async def events_after(self, *args, **kw):
            page = await super().events_after(*args, **kw)
            if page == [] and not self.raced:
                self.raced = True
                await append(self)
            return page

    racing = Racing(store.db, keep_events=4)
    racing.notifier = store.notifier
    s = event_stream(racing, auth[0], auth[1], "r1", 0, LIMITS)  # heartbeat 30 s
    assert seq_of(await next_chunk(s, timeout=2)) == 1


async def test_an_idle_stream_sends_heartbeats_without_keeping_the_session_alive(store, auth):
    sessions, session, _ = auth
    s = stream(store, auth, limits=StreamLimits(heartbeat_s=0.05))
    assert await next_chunk(s) == ": heartbeat\n\n"
    assert session.last_seen == 0.0


async def test_logout_ends_a_stream_in_the_middle_of_a_page(store, auth):
    sessions, session, _ = auth
    await append(store, 2)
    s = stream(store, auth)
    assert seq_of(await next_chunk(s)) == 1
    sessions.logout(session.id)
    assert await next_chunk(s) == control("session_expired")  # event 2 is never sent


async def test_logout_wakes_a_waiting_stream_at_once(store, auth):
    sessions, session, _ = auth
    s = stream(store, auth)  # heartbeat 30 s
    waiting = asyncio.ensure_future(anext(s))
    await asyncio.sleep(0.05)
    sessions.logout(session.id)
    async with asyncio.timeout(1):
        assert await waiting == control("session_expired")


async def test_an_expired_session_ends_the_stream(store, auth):
    sessions, session, clock = auth
    s = stream(store, auth)
    clock.now = 101  # idle for longer than idle_ttl_s
    await append(store)  # any wake-up rechecks the session
    assert await next_chunk(s) == control("session_expired")


async def test_snapshots_and_events_agree_while_runs_change(tmp_path, catalog):
    """A snapshot's state is always the state of the last run_state event it has seen."""
    from .runharness import Harness
    from .simcluster import SimCluster

    store = await Store.open(str(tmp_path / "agree.db"))
    h = Harness(store, SimCluster(starting_snapshots=3), catalog)
    run_id = (await h.create()).body["run_id"]
    snap = await h.service.snapshot("alice", run_id)
    body = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    await h.service.submit("alice", run_id, body)
    seen = []
    while h.service.tasks:
        seen.append(await h.service.snapshot("alice", run_id))
        await asyncio.sleep(0)
    seen.append(await h.service.snapshot("alice", run_id))
    events = await h.events(run_id)
    for snap in seen:
        states = [e for e in events if e["type"] == "run_state" and e["seq"] <= snap["event_seq"]]
        assert states[-1]["payload"]["state"] == snap["state"]
        assert states[-1]["payload"]["revision"] == snap["revision"]
    assert seen[-1]["state"] == "healthy" and len(seen) > 2
    await store.close()
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_events.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.events`.

**Step 3: Implement**

In `platform/src/cagent/api.py`, replace:

```python
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from cagent.auth import LoginError, RateLimited, Session, Sessions
```

with:

```python
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from cagent.auth import LoginError, RateLimited, Session, Sessions
from cagent.events import BadCursor, EventStreamResponse, StreamLimits, event_stream, parse_cursor
```

In `platform/src/cagent/api.py`, replace:

```python
    service: RunService, sessions: Sessions, origin: str, secure_cookies: bool
) -> FastAPI:
```

with:

```python
    service: RunService,
    sessions: Sessions,
    origin: str,
    secure_cookies: bool,
    limits: StreamLimits | None = None,
) -> FastAPI:
    limits = limits or StreamLimits()
    streams: dict[str, int] = {}  # open event streams per session
```

In `platform/src/cagent/api.py`, replace:

```python

    @app.get("/api/runs/{run_id}/commands/{command_id}")
```

with:

```python

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str,
        s: Live,  # streaming is not use: it never refreshes the session
        last_event_id: Annotated[str | None, Header()] = None,
        after: str | None = None,
    ) -> Response:
        await service.snapshot(s.user, run_id)  # the same access check as every read
        try:
            cursor = parse_cursor(last_event_id, after)
        except BadCursor:
            return _error(422, "bad_cursor")
        if streams.get(s.id, 0) >= limits.streams_per_session:
            return _error(429, "too_many_streams")
        streams[s.id] = streams.get(s.id, 0) + 1

        def closed() -> None:
            if streams.get(s.id, 0) <= 1:
                streams.pop(s.id, None)
            else:
                streams[s.id] -= 1

        body = event_stream(service.store, sessions, s, run_id, cursor, limits)
        return EventStreamResponse(body, limits.send_timeout_s, closed)

    @app.get("/api/runs/{run_id}/commands/{command_id}")
```

Create `platform/src/cagent/events.py`:

```python
"""Server-sent events for one run, always read from the store.

A first connection and a reconnection take the same path: read stored events after the
cursor, page by page, each page in its own short read that checks the cursor is still inside
what is kept. A gap (trimmed events, or a cursor past the last event) ends the stream with a
`resync` control event, and the client fetches a snapshot before reconnecting. Waiting uses
the store's notification generation, so a commit between "found nothing" and "start waiting"
is not missed, and a heartbeat re-reads the database anyway.

Control events (`resync`, `session_expired`) have no id and no sequence number. Heartbeats
never count as use of the session; logout and expiry end the stream at once.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.responses import Response

from cagent.auth import Session, Sessions
from cagent.store import Store


@dataclass(frozen=True)
class StreamLimits:
    page_events: int = 100
    page_bytes: int = 256 * 1024
    heartbeat_s: float = 15.0
    send_timeout_s: float = 10.0
    streams_per_session: int = 4


class BadCursor(ValueError):
    """A cursor that is not a non-negative integer."""


def parse_cursor(last_event_id: str | None, after: str | None) -> int:
    """Last-Event-ID wins over after; with neither, start at 0."""
    raw = last_event_id if last_event_id is not None else after
    if raw is None:
        return 0
    if not raw.isdigit():
        raise BadCursor(raw)
    return int(raw)


def _event(e: dict[str, Any]) -> str:
    data = {
        "seq": e["seq"],
        "type": e["type"],
        "at": e["at"],
        "execution_id": e["execution_id"],
        "task_id": e["task_id"],
        "payload": e["payload"],
    }
    return f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(data)}\n\n"


def control(name: str) -> str:
    return f"event: {name}\ndata: {{}}\n\n"


HEARTBEAT = ": heartbeat\n\n"


async def _first(*aws: Any, timeout: float) -> bool:
    """Wait until any awaitable finishes; False if none did within timeout."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return any(t.result() for t in done if not t.cancelled() and t.exception() is None)


async def event_stream(
    store: Store,
    sessions: Sessions,
    session: Session,
    run_id: str,
    cursor: int,
    limits: StreamLimits,
) -> AsyncIterator[str]:
    """Yield SSE text for run_id after cursor until the session ends or a resync is needed."""
    while True:
        if sessions.get(session.id) is not session:
            yield control("session_expired")
            return
        seen = store.notifier.generation(run_id)  # taken before reading
        page = await store.events_after(run_id, cursor, limits.page_events, limits.page_bytes)
        if page is None:
            yield control("resync")
            return
        for e in page:
            if session.revoked.is_set():  # checked again before every business event
                yield control("session_expired")
                return
            yield _event(e)
            cursor = e["seq"]
        if page:
            continue
        until_expiry = max(0.0, sessions.deadline(session) - sessions.clock())
        woke = await _first(
            store.notifier.wait(run_id, seen, limits.heartbeat_s),
            session.revoked.wait(),
            timeout=min(limits.heartbeat_s, until_expiry),
        )
        if not woke and not session.revoked.is_set():
            yield HEARTBEAT


class EventStreamResponse(Response):
    """An event-stream response: unbuffered, with a timeout on every send, and stopped as
    soon as the client disconnects."""

    def __init__(
        self, body: AsyncIterator[str], send_timeout_s: float, on_close: Callable[[], None]
    ):
        self.status_code, self.background = 200, None
        self.body, self.send_timeout, self.on_close = body, send_timeout_s, on_close

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def stream() -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                        (b"x-accel-buffering", b"no"),  # tell proxies not to buffer
                    ],
                }
            )
            async for chunk in self.body:
                message = {"type": "http.response.body", "body": chunk.encode(), "more_body": True}
                await asyncio.wait_for(send(message), self.send_timeout)
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def disconnected() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

        tasks = [asyncio.ensure_future(stream()), asyncio.ensure_future(disconnected())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.body.aclose()
            self.on_close()
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
427 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/api.py platform/src/cagent/events.py platform/tests/test_events.py
git commit -F - <<'EOF'
feat(platform): event streams read from the store with resync and session checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 17: `cagent-platform serve`

`cagent.server.Platform` opens the store, the checkpointer, the hub, and RunService in the documented order, reconciles before the hub starts, and closes them in reverse. `serve` runs uvicorn with one worker and no proxy headers. The tests run a real server and a WebSocket `FakeRunner`.

**Files:**

- Modify: `platform/pyproject.toml`
- Create: `platform/src/cagent/server.py`
- Test (create): `platform/tests/fakerunner.py`
- Test (create): `platform/tests/serverharness.py`
- Test (create): `platform/tests/test_server.py`

**Step 1: Write the failing tests**

Create `platform/tests/fakerunner.py`:

```python
"""A Runner process stand-in: dials the hub over WebSocket like cagent-runner and answers
from a SimCluster. Tests can hold a method (it starts, then waits) or make it silent (it
runs, and its answer is never sent), and it reconnects when the hub restarts."""

import asyncio
import contextlib
import json
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .simcluster import SimCluster


class FakeRunner:
    def __init__(self, url: str, token: str, sim: SimCluster, instance: str = "i-1"):
        self.url, self.token, self.sim, self.instance = url, token, sim, instance
        self.held: dict[str, asyncio.Event] = {}  # method -> release
        self.entered: dict[str, asyncio.Event] = {}  # method -> it started
        self.silent: set[str] = set()  # run, but never answer
        self.connections = 0
        self.connected = asyncio.Event()
        self.active: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def hold(self, method: str) -> asyncio.Event:
        self.entered[method] = asyncio.Event()
        self.held[method] = asyncio.Event()
        return self.entered[method]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        async with asyncio.timeout(10):
            await self.connected.wait()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self._serve()
            except (OSError, ConnectionClosed, InvalidStatus):
                pass
            self.connected.clear()
            await asyncio.sleep(0.05)

    async def _serve(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with connect(self.url, additional_headers=headers) as ws:
            tools = ["build", "inspect", "snapshot", "status", "up", "validate", "verify"]
            hello = {"tools": tools, "instance_id": self.instance}
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "hello", "params": hello}))
            self.connections += 1
            self.connected.set()
            async for raw in ws:
                req = json.loads(raw)
                if req["method"] == "status":
                    report = {"instance_id": self.instance, "active": self.active, "queued": {}}
                    await self._answer(ws, req, {"exit_code": 0, "output": report})
                    continue
                asyncio.create_task(self._call(ws, req))

    async def _call(self, ws, req: dict[str, Any]) -> None:
        method = req["method"]
        name = req["params"].get("name")
        self.active = {"call_id": req["id"], "method": method, "cluster": name, "started_at": "t"}
        try:
            if method in self.held:
                self.entered[method].set()
                await self.held[method].wait()
            res = await self.sim.call("runner", method, req["params"])
        finally:
            self.active = None
        if method in self.silent:
            return
        out = {"exit_code": res.exit_code, "output": res.output, "stderr": res.stderr}
        with contextlib.suppress(ConnectionClosed):
            await self._answer(ws, req, out)

    @staticmethod
    async def _answer(ws, req: dict[str, Any], result: dict[str, Any]) -> None:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}))
```

Create `platform/tests/serverharness.py`:

```python
"""A platform.yaml for tests, and helpers to talk to a running platform over HTTP and SSE."""

import json
import socket
from pathlib import Path

import httpx
import yaml

from .conftest import FIXTURE_CATALOG, REPO_CATALOG

RUNNER_TOKEN = "r" * 40
ENV = {"CAGENT_RUNNER": RUNNER_TOKEN, "CAGENT_ALICE": "a" * 40, "CAGENT_BOB": "b" * 40}
REDIS = {"purpose": "a cache", "services": [], "middleware": [{"template": "redis"}]}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp: Path, web_port: int, hub_port: int = 0) -> Path:
    doc = {
        "db": {"path": "data/cagent.db"},
        "catalog": [str(REPO_CATALOG), str(FIXTURE_CATALOG)],
        "hub": {"port": hub_port, "runners": {"laptop-1": {"token_env": "CAGENT_RUNNER"}}},
        "web": {"origin": f"http://127.0.0.1:{web_port}", "dev_http": True, "port": web_port},
        "users": {
            "alice": {"token_env": "CAGENT_ALICE", "runners": ["laptop-1"]},
            "bob": {"token_env": "CAGENT_BOB", "runners": ["laptop-1"]},
        },
    }
    path = tmp / "platform.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


async def login(c: httpx.AsyncClient, user: str = "alice") -> dict[str, str]:
    csrf = (await c.get("/login")).json()["csrf"]
    origin = str(c.base_url).rstrip("/")
    token = ENV[f"CAGENT_{user.upper()}"]
    r = await c.post("/login", data={"token": token, "csrf": csrf}, headers={"Origin": origin})
    r.raise_for_status()
    return {"Origin": origin, "X-CSRF-Token": r.json()["csrf"]}


async def sse(response: httpx.Response):
    """Yield (id, event, data) from an event-stream response; heartbeats are skipped."""
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "":
            if "event" in fields:
                data = json.loads(fields.get("data", "{}"))
                yield fields.get("id"), fields["event"], data
            fields = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(": ")
            fields[key] = value
```

Create `platform/tests/test_server.py`:

```python
"""The platform as it runs: a real uvicorn server, hub, and a runner over WebSocket."""

import asyncio

import httpx
import pytest

from cagent.config import load_config
from cagent.orchestrator import Settings
from cagent.server import Platform, web_server

from .fakerunner import FakeRunner
from .serverharness import ENV, REDIS, RUNNER_TOKEN, free_port, login, sse, write_config
from .simcluster import SimCluster

FAST = Settings(poll_interval_s=0, stability_window_s=0, startup_timeout_s=5)


@pytest.fixture
async def running(tmp_path):
    port = free_port()
    config = load_config(write_config(tmp_path, port), ENV)
    async with Platform(config, FAST) as platform:
        server = web_server(platform)
        task = asyncio.create_task(server.serve())
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        url = f"ws://127.0.0.1:{platform.hub_port}/runner"
        runner = FakeRunner(url, RUNNER_TOKEN, SimCluster())
        await runner.start()
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            yield platform, client, runner
        await runner.stop()
        server.should_exit = True
        await task


async def wait_for(client, run_id, state):
    async with asyncio.timeout(10):
        while (snap := (await client.get(f"/api/runs/{run_id}")).json())["state"] != state:
            await asyncio.sleep(0.02)
    return snap


async def test_a_stream_resumes_after_a_disconnect(running):
    platform, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    snap = await wait_for(c, run_id, "waiting")
    assert snap["runner_status"]["online"] is True

    async with c.stream("GET", f"/api/runs/{run_id}/events?after=0") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        first = []
        async for event in sse(r):
            first.append(event)
            if len(first) == 3:
                break  # the client goes away
    last = first[-1][0]

    command = {
        "command_id": "a1",
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }
    assert (await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)).is_success
    seqs, states = [], []
    async with c.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": last}) as r:
        async for event_id, event, data in sse(r):
            seqs.append(int(event_id))
            if event == "run_state":
                states.append(data["payload"]["state"])
                if data["payload"]["state"] == "healthy":
                    break
    assert seqs[0] == int(last) + 1 and seqs == sorted(seqs)
    assert states[-1] == "healthy"


async def test_logout_ends_open_streams_at_once(running):
    _, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    snap = await wait_for(c, run_id, "waiting")
    after = snap["event_seq"]

    async def read():
        async with c.stream("GET", f"/api/runs/{run_id}/events?after={after}") as r:
            return [event async for _, event, _ in sse(r)]

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.2)  # the stream is waiting for news
    assert (await c.post("/logout", headers=headers)).is_success
    async with asyncio.timeout(2):
        assert await reader == ["session_expired"]


async def test_streams_check_access_and_cursors(running):
    _, c, _ = running
    headers = await login(c)
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    assert (await c.get(f"/api/runs/{run_id}/events?after=-1")).status_code == 422
    assert (await c.get(f"/api/runs/{run_id}/events?after=x")).status_code == 422
    async with httpx.AsyncClient(base_url=c.base_url) as bob:
        await login(bob, "bob")
        assert (await bob.get(f"/api/runs/{run_id}/events")).status_code == 404
    async with httpx.AsyncClient(base_url=c.base_url) as nobody:
        assert (await nobody.get(f"/api/runs/{run_id}/events")).status_code == 401
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_server.py -q
```

Expected:

```
1 error
```

Collection fails: there is no `cagent.server`.

**Step 3: Implement**

In `platform/pyproject.toml`, replace:

```toml
cagent-gen = "cagent.cli:main"
```

with:

```toml
cagent-gen = "cagent.cli:main"
cagent-platform = "cagent.server:main"
```

Create `platform/src/cagent/server.py`:

```python
"""`cagent-platform serve`: the hub, the HTTP interface, and RunService in one process.

One process and one web worker: Runner connections and running executions live in this
process's memory, so a second worker would not see them.

Startup: open the database, reconcile persisted runs (no graph is advanced and no Runner is
waited for), start the hub, then accept HTTP requests. Shutdown: stop accepting requests,
stop the executions (each records why), close the hub, then the database.
"""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from cagent.api import create_app
from cagent.auth import Sessions
from cagent.catalog import load_catalog
from cagent.config import PlatformConfig, load_config
from cagent.dispatch import RecordingCaller
from cagent.hub import RunnerHub
from cagent.hub import start as start_hub
from cagent.llm import LiteLLMCompleter, Router, load_model_config
from cagent.orchestrator import Orchestrator, Settings
from cagent.runs import RunService
from cagent.store import Store


class Platform:
    """Everything but the HTTP server, opened and closed in the documented order."""

    def __init__(self, config: PlatformConfig, settings: Settings | None = None):
        self.config, self.settings = config, settings
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "Platform":
        c = self.config
        c.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = await Store.open(str(c.db_path))
        self._stack.push_async_callback(self.store.close)
        saver = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(c.db_path))
        )
        router = Router(load_model_config(c.models), LiteLLMCompleter()) if c.models else None
        self.hub = RunnerHub(c.runner_tokens)
        orch = Orchestrator(
            RecordingCaller(self.hub, self.store),
            load_catalog(*c.catalog),
            saver,
            self.settings,
            router=router,
        )
        self.service = RunService(self.store, orch, self.hub, c.user_runners)
        await self.service.recover_on_startup()
        self.hub.add_listener(self.service.runner_changed)
        hub_server = await start_hub(self.hub, c.hub_host, c.hub_port)
        self.hub_port = hub_server.sockets[0].getsockname()[1]

        async def close_hub() -> None:
            hub_server.close()
            await hub_server.wait_closed()

        self._stack.push_async_callback(close_hub)
        self._stack.push_async_callback(self.service.close)  # runs first
        self.sessions = Sessions(c.user_tokens, c.session_ttl_s, c.idle_ttl_s)
        self.app = create_app(self.service, self.sessions, c.origin, c.secure_cookies)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()


def web_server(platform: Platform, port: int | None = None) -> uvicorn.Server:
    c = platform.config
    return uvicorn.Server(
        uvicorn.Config(
            platform.app,
            host=c.web_host,
            port=c.web_port if port is None else port,
            workers=1,
            lifespan="off",
            proxy_headers=False,  # never trust forwarded headers
            log_level="info",
        )
    )


async def serve(config: PlatformConfig) -> None:
    async with Platform(config) as platform:
        await web_server(platform).serve()  # returns after SIGINT or SIGTERM


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cagent-platform")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve", help="run the hub and the web interface")
    run.add_argument("--config", required=True, help="path to platform.yaml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(load_config(args.config)))
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
430 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/pyproject.toml platform/src/cagent/server.py platform/tests/fakerunner.py platform/tests/serverharness.py platform/tests/test_server.py
git commit -F - <<'EOF'
feat(platform): cagent-platform serve runs the hub, the API, and runs in one process

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 18: Crash tests: `kill -9` at three points

`platform.yaml` gains optional `orchestrator` timings so a restarted process can finish a run quickly. The tests run `cagent-platform serve` as a subprocess, kill it after an approval was accepted, after the Runner finished but before the result was stored, and while waiting for input, and restart it on the same database.

**Files:**

- Modify: `platform/src/cagent/config.py`
- Modify: `platform/src/cagent/server.py`
- Test (modify): `platform/tests/serverharness.py`
- Test (modify): `platform/tests/test_config.py`
- Test (create): `platform/tests/test_crash.py`

**Step 1: Write the failing tests**

In `platform/tests/serverharness.py`, replace:

```python
def write_config(tmp: Path, web_port: int, hub_port: int = 0) -> Path:
```

with:

```python
def write_config(tmp: Path, web_port: int, hub_port: int = 0, fast: bool = False) -> Path:
```

In `platform/tests/serverharness.py`, replace:

```python
    }
```

with:

```python
    }
    if fast:
        doc["orchestrator"] = {"poll_interval_s": 0.05, "stability_window_s": 0.1}
```

In `platform/tests/test_config.py`, replace:

```python
    assert (c.web_host, c.web_port, c.secure_cookies) == ("127.0.0.1", 8000, False)
```

with:

```python
    assert (c.web_host, c.web_port, c.secure_cookies) == ("127.0.0.1", 8000, False)
    assert (c.settings.stability_window_s, c.settings.poll_interval_s) == (120, 3)
```

Create `platform/tests/test_crash.py`:

```python
"""Crashes of the platform process (kill -9) and restarts on the same database.

The platform runs as a real subprocess; the runner is a FakeRunner in this process that
reconnects to the restarted hub. In every case a pending item is consumed at most once, a
command starts at most one execution, and startup advances no graph."""

import asyncio
import os
import sys

import httpx
import pytest

from .fakerunner import FakeRunner
from .serverharness import ENV, REDIS, RUNNER_TOKEN, free_port, login, write_config
from .simcluster import SimCluster


class Platform:
    """cagent-platform serve as a subprocess."""

    def __init__(self, tmp_path):
        self.web, self.hub = free_port(), free_port()
        self.config = write_config(tmp_path, self.web, self.hub, fast=True)
        self.log = tmp_path / "platform.log"
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        with self.log.open("a") as log:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from cagent.server import main; main()",
                "serve",
                "--config",
                str(self.config),
                env={**os.environ, **ENV},
                stdout=log,
                stderr=log,
            )
        async with asyncio.timeout(30), httpx.AsyncClient() as c:
            while True:
                try:
                    if (await c.get(f"http://127.0.0.1:{self.web}/login")).is_success:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    async def kill(self) -> None:
        self.proc.kill()  # SIGKILL: no shutdown handlers run
        await self.proc.wait()

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.web}", timeout=10)


@pytest.fixture
async def platform(tmp_path):
    p = Platform(tmp_path)
    await p.start()
    runner = FakeRunner(f"ws://127.0.0.1:{p.hub}/runner", RUNNER_TOKEN, SimCluster())
    await runner.start()
    yield p, runner
    await runner.stop()
    if p.proc.returncode is None:
        p.proc.terminate()
        await p.proc.wait()


async def wait_for(c, run_id, state):
    async with asyncio.timeout(20):
        while (snap := (await c.get(f"/api/runs/{run_id}")).json())["state"] != state:
            await asyncio.sleep(0.05)
    return snap


async def waiting_run(c, headers):
    body = {"command_id": "c1", "runner": "laptop-1", "cluster": "demo", "spec": REDIS}
    run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
    return run_id, await wait_for(c, run_id, "waiting")


def approval(snap, command_id="a1"):
    return {
        "command_id": command_id,
        "expected_revision": snap["revision"],
        "action": "answer",
        "pending_id": snap["pending"]["pending_id"],
        "decision": {"approve": True},
    }


async def restart(p, runner):
    await p.start()
    async with asyncio.timeout(10):
        while runner.connections < 2 or not runner.connected.is_set():
            await asyncio.sleep(0.05)


async def recover(c, headers, run_id):
    """Refresh, review the plan, approve it; return the plan."""
    for action, cid in [("refresh", "f1"), ("review_recovery", "v1")]:
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        body = {"command_id": cid, "expected_revision": snap["revision"], "action": action}
        r = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert r.status_code == 202, r.text
    snap = (await c.get(f"/api/runs/{run_id}")).json()
    plan = snap["pending"]["payload"]
    decision = {"approve": True, "acknowledged": plan["required"]}
    body = {**approval(snap, "r1"), "decision": decision}
    assert (await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)).is_success
    return plan


async def test_a_crash_after_an_approval_was_accepted(platform):
    p, runner = platform
    entered = runner.hold("up")
    async with p.client() as c:
        headers = await login(c)
        run_id, snap = await waiting_run(c, headers)
        body = approval(snap)
        first = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert first.status_code == 202
        async with asyncio.timeout(10):
            await entered.wait()
    await p.kill()  # up is running on the runner; the platform dies
    runner.held["up"].set()  # the runner finishes, but nobody hears it
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        assert snap["state"] == "reconcile" and snap["reconcile"]["last_phase"] == "up"
        done = (await c.get(f"/api/runs/{run_id}/commands/a1")).json()
        assert (done["status"], done["effect"]) == ("interrupted", "unknown")
        again = await c.post(f"/api/runs/{run_id}/commands", json=body, headers=headers)
        assert again.status_code == 202 and again.headers["cagent-replay"] == "true"
        assert again.json() == first.json()  # the accepted fact is unchanged
        other = {**body, "command_id": "a2"}  # the same pending item, consumed already
        r = await c.post(f"/api/runs/{run_id}/commands", json=other, headers=headers)
        assert r.status_code == 409
        await asyncio.sleep(0.3)
        assert runner.sim.calls.count("up") == 1  # nothing ran again by itself

        runner.held.clear()
        plan = await recover(c, headers, run_id)
        assert [r["id"] for r in plan["risks"]][0].startswith("mark:")
        await wait_for(c, run_id, "healthy")
        assert runner.sim.calls.count("up") == 2


async def test_a_crash_after_the_runner_finished_but_before_the_result_was_stored(platform):
    p, runner = platform
    runner.silent.add("up")  # up runs; its answer never reaches the platform
    async with p.client() as c:
        headers = await login(c)
        run_id, snap = await waiting_run(c, headers)
        await c.post(f"/api/runs/{run_id}/commands", json=approval(snap), headers=headers)
        async with asyncio.timeout(10):
            while "up" not in runner.sim.calls:
                await asyncio.sleep(0.02)
    await p.kill()
    runner.silent.clear()
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        snap = (await c.get(f"/api/runs/{run_id}")).json()
        assert snap["state"] == "reconcile" and snap["reconcile"]["reason"] == "platform restarted"
        await asyncio.sleep(0.3)
        assert runner.sim.calls.count("up") == 1
        plan = await recover(c, headers, run_id)
        texts = [r["text"] for r in plan["risks"]]
        assert "Unconfirmed operation: up sent without a recorded result" in texts
        await wait_for(c, run_id, "healthy")


async def test_a_restart_while_waiting_keeps_the_pending_item(platform):
    p, runner = platform
    async with p.client() as c:
        headers = await login(c)
        run_id, before = await waiting_run(c, headers)
    await p.kill()
    await restart(p, runner)

    async with p.client() as c:
        headers = await login(c)
        after = (await c.get(f"/api/runs/{run_id}")).json()
        assert after["state"] == "waiting" and after["pending"] == before["pending"]
        r = await c.post(f"/api/runs/{run_id}/commands", json=approval(after), headers=headers)
        again = await c.post(f"/api/runs/{run_id}/commands", json=approval(after), headers=headers)
        assert r.status_code == again.status_code == 202 and again.json() == r.json()
        await wait_for(c, run_id, "healthy")
        assert runner.sim.calls.count("up") == 1
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_config.py -q
```

Expected:

```
1 failed, 20 passed
```

The config test fails: there is no `settings`. (Run without it, the crash tests would only time out: the platform refuses the `orchestrator` section.)

**Step 3: Implement**

In `platform/src/cagent/config.py`, replace:

```python
from pydantic import BaseModel, ConfigDict, Field, ValidationError
```

with:

```python
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cagent.orchestrator import Settings
```

In `platform/src/cagent/config.py`, replace:

```python

class _File(_Strict):
```

with:

```python

class _Orchestrator(_Strict):
    """Timings; the defaults follow the design's section 5.5."""

    poll_interval_s: float = Field(default=3, ge=0)
    stability_window_s: float = Field(default=120, ge=0)
    startup_timeout_s: float = Field(default=300, gt=0)


class _File(_Strict):
```

In `platform/src/cagent/config.py`, replace:

```python
    users: dict[str, _User]
```

with:

```python
    users: dict[str, _User]
    orchestrator: _Orchestrator = _Orchestrator()
```

In `platform/src/cagent/config.py`, replace:

```python
    user_runners: dict[str, set[str]]
```

with:

```python
    user_runners: dict[str, set[str]]
    settings: Settings
```

In `platform/src/cagent/config.py`, replace:

```python
        user_runners={u: set(c.runners) for u, c in raw.users.items()},
```

with:

```python
        user_runners={u: set(c.runners) for u, c in raw.users.items()},
        settings=Settings(**raw.orchestrator.model_dump()),
```

In `platform/src/cagent/server.py`, replace:

```python
            self.settings,
```

with:

```python
            self.settings or c.settings,
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run pytest tests/test_crash.py tests/test_config.py -q
```

Expected:

```
24 passed
```

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
433 passed, 10 skipped
```

**Step 5: Commit**

```bash
git add platform/src/cagent/config.py platform/src/cagent/server.py platform/tests/serverharness.py platform/tests/test_config.py platform/tests/test_crash.py
git commit -F - <<'EOF'
test(platform): kill -9 the platform at three points and restart on the same database

The orchestrator's timings move into platform.yaml, so the restarted process
can finish a run quickly.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 18 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

### Task 19: End to end on real Docker

A run created over HTTP reaches healthy through the real hub and the real Runner. `make e2e` runs it in CI. This task adds a test for behaviour already built, so it passes at once; on this machine build the Runner for macOS first.

**Files:**

- Modify: `Makefile`
- Test (create): `platform/tests/test_platform_e2e.py`

**Step 1: Write the failing tests**

Create `platform/tests/test_platform_e2e.py`:

```python
"""M8a on real Docker: a run created over HTTP reaches healthy through the real hub and the
real Runner, with observations, Runner status, and the instance the Runner reports.

Skipped unless CAGENT_RUNNER points to the runner binary; `make e2e` sets it.
"""

import asyncio
import os
import subprocess
import time

import httpx
import pytest

from cagent.config import load_config
from cagent.hub import status
from cagent.orchestrator import Settings
from cagent.server import Platform, web_server

from .conftest import FIXTURES
from .serverharness import ENV, RUNNER_TOKEN, free_port, login, write_config

pytestmark = pytest.mark.skipif(
    "CAGENT_RUNNER" not in os.environ, reason="set CAGENT_RUNNER to the runner binary"
)

SETTINGS = Settings(poll_interval_s=1, stability_window_s=3, startup_timeout_s=120)


async def test_a_run_over_http_reaches_healthy_on_real_docker(tmp_path):
    port, hub_port = free_port(), free_port()
    config = load_config(write_config(tmp_path, port, hub_port), ENV)
    config.catalog.append(FIXTURES / "e2e-catalog")
    name = f"m8a-e2e-{int(time.time()) % 100000}"
    async with Platform(config, SETTINGS) as platform:
        server = web_server(platform)
        serving = asyncio.create_task(server.serve())
        env = {**os.environ, "CAGENT_TOKEN": RUNNER_TOKEN, "CAGENT_HOME": str(tmp_path / "home")}
        runner = subprocess.Popen(
            [os.environ["CAGENT_RUNNER"], "serve", "-hub", f"ws://127.0.0.1:{hub_port}/runner"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await platform.hub.wait_for_runner("laptop-1", timeout=10)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as c:
                headers = await login(c)
                spec = {"purpose": "a cache", "middleware": [{"template": "redis"}]}
                body = {"command_id": "c1", "runner": "laptop-1", "cluster": name, "spec": spec}
                run_id = (await c.post("/api/runs", json=body, headers=headers)).json()["run_id"]
                snap = await wait_for(c, run_id, "waiting", 60)
                assert snap["pending"]["kind"] == "approval"
                command = {
                    "command_id": "a1",
                    "expected_revision": snap["revision"],
                    "action": "answer",
                    "pending_id": snap["pending"]["pending_id"],
                    "decision": {"approve": True},
                }
                r = await c.post(f"/api/runs/{run_id}/commands", json=command, headers=headers)
                assert r.status_code == 202, r.text
                snap = await wait_for(c, run_id, "healthy", 180)

            report = await status(platform.hub, "laptop-1")
            assert report["active"] is None and report["queued"] == {}
            assert snap["runner_status"] == {
                "online": True,
                "instance_id": report["instance_id"],
                "changed_at": snap["runner_status"]["changed_at"],
            }
            [redis] = snap["last_observation"]["services"]
            assert (redis["service"], redis["state"]) == ("redis", "running")
            assert snap["verified_at"] >= snap["last_observation"]["received_at"] - 1
            events = await platform.store.events_after(run_id, 0, 10_000, 10_000_000)
            types = {e["type"] for e in events}
            assert {"phase_started", "phase_interrupted", "observation", "run_state"} <= types
        finally:
            await platform.hub.call("laptop-1", "down", {"name": name})
            runner.terminate()
            runner.wait(timeout=10)
            server.should_exit = True
            await serving


async def wait_for(c, run_id, state, timeout):
    async with asyncio.timeout(timeout):
        while (snap := (await c.get(f"/api/runs/{run_id}")).json())["state"] != state:
            assert snap["state"] not in ("reconcile", "rejected", "escalated"), snap
            await asyncio.sleep(0.5)
    return snap
```

**Step 2: No failing run**

This task tests behaviour the earlier tasks built; Step 4 runs it.

**Step 3: Implement**

In `Makefile`, replace:

```makefile
# built, and init scripts, keyspaces, and topics must exist before the application starts.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py tests/test_inspect_e2e.py tests/test_init_e2e.py
```

with:

```makefile
# built, init scripts, keyspaces, and topics must exist before the application starts,
# and a run created over HTTP must reach healthy with the real Runner.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py tests/test_inspect_e2e.py tests/test_init_e2e.py tests/test_platform_e2e.py
```

**Step 4: Run the tests**

Run:

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo/runner -e GOOS=darwin -e GOARCH=arm64 golang:1.24 go build -o ../bin/cagent-runner ./cmd/cagent-runner
```

Expected:

```
(no output)
```

Run:

```bash
cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner uv run pytest tests/test_platform_e2e.py -q
```

Expected:

```
1 passed
```

Run:

```bash
docker ps -a --filter label=cagent.managed=true --format '{{.Names}}' | wc -l
```

Expected:

```
0
```

**Step 5: Commit**

```bash
git add Makefile platform/tests/test_platform_e2e.py
git commit -F - <<'EOF'
test(platform): a run over HTTP reaches healthy with the real runner on Docker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

---

### Task 20: Docs and an example configuration

Record the HTTP interface (the input to M8b), the implementation findings, the Runner's new protocol behaviour in the main design, and an example `platform.yaml` that a test keeps loadable.

**Files:**

- Modify: `docs/plans/2026-10-17-container-agent-design.md`
- Modify: `docs/plans/2026-10-19-m8-web-ui-design.md`
- Create: `platform/platform.example.yaml`
- Test (modify): `platform/tests/test_config.py`

**Step 1: Write the failing tests**

In `platform/tests/test_config.py`, replace:

```python
        check_origin(origin)
```

with:

```python
        check_origin(origin)


def test_the_example_config_loads(tmp_path):
    from .conftest import REPO

    env = {
        "CAGENT_RUNNER_LAPTOP_1": "r" * 43,
        "CAGENT_USER_ALICE": "a" * 43,
    }
    c = load_config(REPO / "platform" / "platform.example.yaml", env)
    assert c.user_runners == {"alice": {"laptop-1"}} and c.models.name == "models.yaml"
    assert c.settings.stability_window_s == 120
```

**Step 2: Run them to make sure they fail**

Run:

```bash
cd platform && uv run pytest tests/test_config.py -q
```

Expected:

```
1 failed, 21 passed
```

The new test fails: the example file does not exist.

**Step 3: Implement**

In `docs/plans/2026-10-17-container-agent-design.md`, replace:

```markdown
- The Runner reads the connection continuously and runs calls one at a time on a worker, so it keeps answering pings while a build runs; otherwise the hub would drop it as dead.
```

with:

```markdown
- The Runner reads the connection continuously and runs calls one at a time on one worker for the whole process, so it keeps answering pings while a build runs; otherwise the hub would drop it as dead. A call belongs to the connection it arrived on: when that connection ends, its queued calls are dropped, and a call already running finishes but answers only on its own connection, so a reconnect never lets an old call run beside a new one. `status`, answered outside the queue, reports the process's `instance_id` (also sent in `hello`), the active call, and the queued calls per cluster ([M8 design](2026-10-19-m8-web-ui-design.md), section 4).
```

In `docs/plans/2026-10-19-m8-web-ui-design.md`, replace:

```markdown
## 14. Out of scope and follow-ups
```

with:

```markdown
## 14. HTTP interface (M8a, input to M8b)

| Method | Path | Body or parameters | Answer |
|---|---|---|---|
| GET | `/login` | — | `{csrf}`; sets the `cagent_pre` cookie |
| POST | `/login` | form `token`, `csrf`; `Origin` | `{user, csrf}`; sets `cagent_session`, clears `cagent_pre` |
| POST | `/logout` | `X-CSRF-Token`, `Origin` | `{ok}`; closes the session's event streams |
| GET | `/api/runs` | — | the user's runs |
| POST | `/api/runs` | `{command_id, runner, cluster, purpose \| spec}` | `202 {command_id, run_id, accepted_revision, execution_id}` |
| GET | `/api/runs/{id}` | — | the snapshot (section 11) |
| POST | `/api/runs/{id}/commands` | `{command_id, expected_revision, action, pending_id?, decision?}`, `action` ∈ `answer`, `refresh`, `review_recovery`, `abandon` | `202 {command_id, run_id, accepted_revision, execution_id}`; `Cagent-Replay: true` on a repeat |
| GET | `/api/runs/{id}/commands/{command_id}` | — | `{command_id, kind, status, effect, reason, response}` |
| GET | `/api/runs/{id}/events` | `Last-Event-ID` or `?after=` | `text/event-stream` (section 11) |

Errors are `{error, detail?, snapshot?}`: `401 login_required`, `401 login_failed`, `403 bad_origin`, `403 bad_csrf`, `404 not_found`, `409 stale | not_waiting | not_reconcile | command_id_reused | cluster_name_taken | cluster_busy | runner_offline | runner_unavailable | check_needed | no_compose | plan_stale` (stale ones carry the snapshot), `422 invalid_command | invalid_decision | invalid_cluster | invalid_input | invalid_spec | bad_cursor`, `429 rate_limited | too_many_streams`, `503 shutting_down`.

`refresh` is read-only and ignores `expected_revision`; every other command checks it. Configuration: [`platform/platform.example.yaml`](../../platform/platform.example.yaml); `orchestrator` holds the state machine's timings.

## 15. Implementation findings (M8a)

- **A node that raises ends `astream` without a task result** (LangGraph 1.2.16). The adapter therefore reports `phase_failed` itself for every task still started, then re-raises.
- **A resumed node reports a task start again, with the same task id.** Events are keyed by `execution_id + task_id`, and the store numbers them.
- **Approval and recovery digests agree.** An approval record's `candidate_digest` is the SHA-256 of the compose YAML shown, the same function recovery plans use, so "the compose changed since it was approved" is never a false alarm.
- **`validate` now always runs with an empty `allow`.** The approval payload carries the current `allow` so a page can pre-select it; `up` still passes the approved set, and the Runner's `-allow-rule` ceiling still applies.
- **`status` is a normal tool result** (`exit_code` 0, the report as output), so the hub's call path needed no special case.
- **Behind a TLS proxy every client has the proxy's address**, because proxy headers are never trusted; login rate limits are then shared by all users. Acceptable for the POC; a deployment that needs per-client limits must terminate TLS in the platform. Recorded, not changed.
- Measurements on the dev machine: the three `kill -9` scenarios take ~7.4 s together; the real-Docker run over HTTP ~11 s; the Runner's serve tests pass 20 times in a row under `-race`.

## 16. Out of scope and follow-ups
```

Create `platform/platform.example.yaml`:

```yaml
# cagent-platform serve --config platform.example.yaml
# Tokens are never written here: each *_env names the environment variable holding one.
# Generate each with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"

db:
  path: data/cagent.db  # SQLite: runs, approvals, events, and LangGraph checkpoints

catalog: [../catalog]  # descriptor and template roots, relative to this file
models: models.yaml  # model tiers; leave out to run without models (spec input only)

hub:  # Runners dial in here: cagent-runner serve -hub ws://HOST:8765/runner
  host: 127.0.0.1
  port: 8765
  runners:
    laptop-1: {token_env: CAGENT_RUNNER_LAPTOP_1}

web:
  host: 127.0.0.1
  port: 8000
  origin: http://127.0.0.1:8000  # exactly what the browser shows; https behind a TLS proxy
  dev_http: true  # plain http is only for this machine; set secure_cookies with https
  session_ttl_s: 43200
  idle_ttl_s: 3600

users:
  alice: {token_env: CAGENT_USER_ALICE, runners: [laptop-1]}

orchestrator:  # the design's defaults (section 5.5)
  poll_interval_s: 3
  stability_window_s: 120
  startup_timeout_s: 300
```

**Step 4: Run the tests**

Run:

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
434 passed, 11 skipped
```

**Step 5: Commit**

```bash
git add docs/plans/2026-10-17-container-agent-design.md docs/plans/2026-10-19-m8-web-ui-design.md platform/platform.example.yaml platform/tests/test_config.py
git commit -F - <<'EOF'
docs: record M8a's interface, findings, and an example platform.yaml

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

**Checkpoint.** Stop after Task 20 and report: the commits so far, the output of the last run, and anything that did not match this plan.

---

## Final verification

Run each command; the expected output is the replay's.

**Runner unit tests:**

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo golang:1.24 make runner-test
```

Expected:

```
ok  	github.com/yutaofr/container-agent/runner/cmd/cagent-runner
ok  	github.com/yutaofr/container-agent/runner/internal/builder
ok  	github.com/yutaofr/container-agent/runner/internal/cluster
ok  	github.com/yutaofr/container-agent/runner/internal/compose
ok  	github.com/yutaofr/container-agent/runner/internal/dockercli
ok  	github.com/yutaofr/container-agent/runner/internal/fingerprint
ok  	github.com/yutaofr/container-agent/runner/internal/guard
ok  	github.com/yutaofr/container-agent/runner/internal/initscripts
ok  	github.com/yutaofr/container-agent/runner/internal/inspect
ok  	github.com/yutaofr/container-agent/runner/internal/maven
ok  	github.com/yutaofr/container-agent/runner/internal/redact
ok  	github.com/yutaofr/container-agent/runner/internal/signals
ok  	github.com/yutaofr/container-agent/runner/internal/verify
ok  	github.com/yutaofr/container-agent/runner/internal/workdir
ok  	github.com/yutaofr/container-agent/runner/internal/workspace
```

**Runner integration tests on real Docker (built for the host):**

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo/runner -e GOOS=darwin -e GOARCH=arm64 golang:1.24 go test -c -tags integration -o /repo/bin/it.test ./test/integration/ && (cd runner/test/integration && ../../../bin/it.test -test.count=1); rc=$?; rm -f bin/it.test; exit $rc
```

Expected:

```
PASS
```

**Platform lint and tests:**

```bash
cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

Expected:

```
434 passed, 11 skipped
```

**Every platform test with the real Runner (the macOS binary from Task 19):**

```bash
docker run --rm -v "$PWD":/repo -v cagent-gomod:/go/pkg/mod -w /repo/runner -e GOOS=darwin -e GOARCH=arm64 golang:1.24 go build -o ../bin/cagent-runner ./cmd/cagent-runner && cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner uv run pytest -q
```

Expected:

```
441 passed, 4 skipped
```

**With a local Ollama serving the tiers in platform/models.yaml: the live-model tests too:**

```bash
cd platform && CAGENT_RUNNER=$PWD/../bin/cagent-runner CAGENT_LLM_E2E=1 uv run pytest -q tests/test_llm_e2e.py tests/test_clarify_llm_e2e.py tests/test_draft_init_llm_e2e.py
```

Expected:

```
4 passed
```


Then check that nothing is left behind:

```bash
docker ps -a --filter label=cagent.managed=true --format '{{.Names}}' | wc -l
pgrep -fl "cagent-runner|cagent.server" || echo none
```

Expected: `0` and `none`.

## After the plan

Report to the planning session: the commit list (`git log --oneline main..HEAD`, 20 commits), the final verification output, and any finding you recorded. M8b (the browser interface) is planned only after this branch is merged, against the interface in design section 14.
