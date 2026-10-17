package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
)

const testToken = "secret"

// fakeHub accepts runner connections that present testToken and hands each one to the test.
func fakeHub(t *testing.T) (string, <-chan *websocket.Conn) {
	t.Helper()
	conns := make(chan *websocket.Conn, 4)
	stop := make(chan struct{})
	t.Cleanup(func() { close(stop) })
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+testToken {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		conns <- c
		<-stop // keep the handler alive while the test uses c
	}))
	t.Cleanup(srv.Close)
	return "ws" + strings.TrimPrefix(srv.URL, "http") + "/runner", conns
}

// startServe runs serveLoop until the test ends and returns the hub side of its first connection.
func startServe(t *testing.T, d deps, mutate func(*serveConfig)) (*websocket.Conn, <-chan *websocket.Conn) {
	t.Helper()
	url, conns := fakeHub(t)
	cfg := serveConfig{hub: url, token: testToken, backoff: 10 * time.Millisecond,
		backoffMax: 50 * time.Millisecond, pingEvery: time.Hour}
	if mutate != nil {
		mutate(&cfg)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = serveLoop(ctx, cfg, d, &strings.Builder{}); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	c := next(t, conns)
	var hello rpcRequest
	readJSON(t, c, &hello)
	if hello.Method != "hello" {
		t.Fatalf("first message %+v, want hello", hello)
	}
	return c, conns
}

func next(t *testing.T, conns <-chan *websocket.Conn) *websocket.Conn {
	t.Helper()
	select {
	case c := <-conns:
		return c
	case <-time.After(5 * time.Second):
		t.Fatal("runner did not connect")
		return nil
	}
}

func readJSON(t *testing.T, c *websocket.Conn, v any) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := wsjson.Read(ctx, c, v); err != nil {
		t.Fatal(err)
	}
}

// call sends one request and returns the decoded response.
func call(t *testing.T, c *websocket.Conn, id int, method string, params any) rpcResponse {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req := map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params}
	if err := wsjson.Write(ctx, c, req); err != nil {
		t.Fatal(err)
	}
	var resp rpcResponse
	readJSON(t, c, &resp)
	return resp
}

func TestServeAnswersToolCalls(t *testing.T) {
	d, _, _ := withDocker(t, map[string]dockercli.FakeResponse{
		"ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-demo": {Out: ""},
	})
	c, _ := startServe(t, d, nil)

	resp := call(t, c, 1, "validate", map[string]any{
		"name": "demo", "compose": "services:\n  web: { image: nginx:1.27, privileged: true }\n",
	})
	if resp.Error != nil || resp.Result == nil || resp.Result.ExitCode != exitFindings {
		t.Fatalf("validate: %+v", resp)
	}
	var rep Report
	if err := json.Unmarshal(resp.Result.Output, &rep); err != nil || len(rep.Findings) != 1 {
		t.Errorf("validate output %s: %v", resp.Result.Output, err)
	}

	resp = call(t, c, 2, "snapshot", map[string]any{"name": "demo"})
	if resp.Error != nil || resp.Result.ExitCode != exitOK {
		t.Fatalf("snapshot: %+v", resp)
	}
	var snap struct {
		Project  string `json:"project"`
		Services []any  `json:"services"`
	}
	if err := json.Unmarshal(resp.Result.Output, &snap); err != nil || snap.Project != "cagent-demo" || snap.Services == nil {
		t.Errorf("snapshot output %s: %v", resp.Result.Output, err)
	}
}

func TestServeRejectsBadCalls(t *testing.T) {
	c, _ := startServe(t, testDeps(t), nil)
	cases := []struct {
		method string
		params any
		code   int
	}{
		{"rm", map[string]any{}, rpcMethodNotFound},
		{"serve", map[string]any{}, rpcMethodNotFound},
		{"snapshot", map[string]any{"name": "demo", "extra": 1}, rpcInvalidParams},
		{"down", map[string]any{"name": "-rf"}, rpcInvalidParams},
		{"build", map[string]any{"name": "demo", "service": "web", "tag": "cagent/web:x", "workdir": "/etc"}, rpcInvalidParams},
	}
	for i, tc := range cases {
		resp := call(t, c, i+1, tc.method, tc.params)
		if resp.Error == nil || resp.Error.Code != tc.code {
			t.Errorf("%s %v: got %+v, want error %d", tc.method, tc.params, resp, tc.code)
		}
	}
}

func TestServeKeepsAnsweringPingsDuringALongCall(t *testing.T) {
	d, _, _ := withDocker(t, map[string]dockercli.FakeResponse{
		"ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-slow": {Delay: 2 * time.Second},
	})
	c, _ := startServe(t, d, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := wsjson.Write(ctx, c, map[string]any{"jsonrpc": "2.0", "id": 1, "method": "snapshot",
		"params": map[string]any{"name": "slow"}}); err != nil {
		t.Fatal(err)
	}
	// Ping needs a concurrent reader on this side too.
	got := make(chan rpcResponse, 1)
	go func() {
		var r rpcResponse
		if wsjson.Read(ctx, c, &r) == nil {
			got <- r
		}
	}()
	pctx, pcancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer pcancel()
	if err := c.Ping(pctx); err != nil {
		t.Fatalf("runner stopped answering pings during a call: %v", err)
	}
	select {
	case r := <-got:
		if r.Result == nil {
			t.Errorf("slow call: %+v", r)
		}
	case <-ctx.Done():
		t.Fatal("slow call never answered")
	}
}

func TestServeReconnectsAfterHubDrops(t *testing.T) {
	c, conns := startServe(t, testDeps(t), nil)
	c.Close(websocket.StatusGoingAway, "hub restarting")
	c2 := next(t, conns)
	var hello rpcRequest
	readJSON(t, c2, &hello)
	if hello.Method != "hello" {
		t.Fatalf("after reconnect got %+v", hello)
	}
	if resp := call(t, c2, 1, "rm", nil); resp.Error == nil {
		t.Error("reconnected runner did not answer")
	}
}

func TestServeStopsOnRejectedToken(t *testing.T) {
	url, _ := fakeHub(t)
	cfg := serveConfig{hub: url, token: "wrong", backoff: 10 * time.Millisecond, backoffMax: 10 * time.Millisecond, pingEvery: time.Hour}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := serveLoop(ctx, cfg, testDeps(t), &strings.Builder{}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("got %v, want ErrUnauthorized", err)
	}
}

func TestServeNeedsHubAndToken(t *testing.T) {
	t.Setenv("CAGENT_TOKEN", "")
	for _, args := range [][]string{
		{"serve", "-hub", "http://x"},
		{"serve", "-hub", "ws://x/runner"},
	} {
		var out, errOut strings.Builder
		if code := run(args, &out, &errOut, testDeps(t)); code != exitError {
			t.Errorf("%v: exit %d, want %d", args, code, exitError)
		}
	}
}
