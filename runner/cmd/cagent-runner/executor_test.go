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
