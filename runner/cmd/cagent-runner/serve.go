package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/initscripts"
	"github.com/yutaofr/container-agent/runner/internal/redact"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// The Runner dials the hub and answers JSON-RPC 2.0 calls; the hub never connects to the
// user's machine. Each call runs one CLI subcommand in-process, so RPC and CLI share one
// code path, one validation, and one output format.

// ErrUnauthorized means the hub rejected the token; retrying cannot help.
var ErrUnauthorized = errors.New("hub rejected the runner token")

// JSON-RPC 2.0 error codes.
const (
	rpcMethodNotFound = -32601
	rpcInvalidParams  = -32602
)

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  *ToolResult     `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

// ToolResult carries a subcommand's outcome unchanged: findings and failed verifications
// are results with a non-zero exit code, not RPC errors.
type ToolResult struct {
	ExitCode int             `json:"exit_code"`
	Output   json.RawMessage `json:"output"`
	Stderr   string          `json:"stderr,omitempty"`
}

type serveConfig struct {
	hub          string
	token        string
	workdirRoots []string        // resolved; build may only use working directories inside these
	settings     string          // Maven settings.xml; the platform cannot choose another
	allowRules   map[string]bool // rule IDs the platform may approve over RPC; empty means none
	backoff      time.Duration
	backoffMax   time.Duration
	pingEvery    time.Duration
}

// stringList is a repeatable string flag.
type stringList []string

func (l *stringList) String() string     { return strings.Join(*l, ",") }
func (l *stringList) Set(v string) error { *l = append(*l, v); return nil }

func runServe(args []string, stdout, stderr io.Writer, d deps) int {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(stderr)
	hub := fs.String("hub", "", "hub URL, ws:// or wss://")
	tokenEnv := fs.String("token-env", "CAGENT_TOKEN", "environment variable holding the runner token")
	settings := fs.String("maven-settings", "", "Maven settings.xml for build (default ~/.m2/settings.xml)")
	var roots, rules stringList
	fs.Var(&roots, "workdir-root", "directory under which build may use working directories; repeatable")
	fs.Var(&rules, "allow-rule", "lint rule ID the platform may approve over RPC, e.g. privileged or field:sysctls; repeatable, default none")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if !strings.HasPrefix(*hub, "ws://") && !strings.HasPrefix(*hub, "wss://") {
		fmt.Fprintln(stderr, "serve needs -hub ws://… or wss://…")
		return exitError
	}
	// The token comes from the environment, not a flag, so it never shows up in ps.
	token := os.Getenv(*tokenEnv)
	if token == "" {
		fmt.Fprintf(stderr, "environment variable %s is empty\n", *tokenEnv)
		return exitError
	}
	cfg := serveConfig{hub: *hub, token: token, settings: *settings,
		backoff: time.Second, backoffMax: 30 * time.Second, pingEvery: 30 * time.Second}
	cfg.allowRules = map[string]bool{}
	for _, r := range rules {
		if r = strings.TrimSpace(r); r != "" {
			cfg.allowRules[r] = true
		}
	}
	for _, r := range roots {
		rr, err := guard.Resolve(r)
		if err != nil {
			fmt.Fprintf(stderr, "workdir-root %s: %v\n", r, err)
			return exitError
		}
		cfg.workdirRoots = append(cfg.workdirRoots, rr)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := serveLoop(ctx, cfg, d, stderr); err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return exitOK
}

// serveLoop keeps a hub connection up until ctx ends, reconnecting with exponential
// backoff. Only a rejected token stops it early.
func serveLoop(ctx context.Context, cfg serveConfig, d deps, logw io.Writer) error {
	wait := cfg.backoff
	for {
		connected, err := serveOnce(ctx, cfg, d)
		if ctx.Err() != nil {
			return nil
		}
		if errors.Is(err, ErrUnauthorized) {
			return err
		}
		if connected {
			wait = cfg.backoff
		}
		fmt.Fprintf(logw, "hub connection lost: %v; reconnecting in %s\n", err, wait)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(wait):
		}
		wait = min(wait*2, cfg.backoffMax)
	}
}

// serveOnce runs one connection. The read loop never blocks on a tool: calls go to a
// single worker, so pings keep getting answered during a long build.
func serveOnce(ctx context.Context, cfg serveConfig, d deps) (bool, error) {
	c, resp, err := websocket.Dial(ctx, cfg.hub, &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": {"Bearer " + cfg.token}},
	})
	if err != nil {
		if resp != nil && (resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden) {
			return false, ErrUnauthorized
		}
		return false, err
	}
	defer c.CloseNow()
	c.SetReadLimit(16 << 20) // compose files and probe lists

	connCtx, cancel := context.WithCancel(ctx)
	defer cancel()
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
	go func() {
		t := time.NewTicker(cfg.pingEvery)
		defer t.Stop()
		for {
			select {
			case <-connCtx.Done():
				return
			case <-t.C:
				pctx, done := context.WithTimeout(connCtx, 10*time.Second)
				if err := c.Ping(pctx); err != nil {
					cancel()
				}
				done()
			}
		}
	}()

	for {
		var req rpcRequest
		if err := wsjson.Read(connCtx, c, &req); err != nil {
			return true, err
		}
		if len(req.ID) == 0 {
			continue // notifications need no answer
		}
		calls <- req
	}
}

func hello() map[string]any {
	names := make([]string, 0, len(tools))
	for n := range tools {
		names = append(names, n)
	}
	sort.Strings(names)
	return map[string]any{"jsonrpc": "2.0", "method": "hello", "params": map[string]any{"tools": names}}
}

// dispatch runs one call. Params errors become JSON-RPC errors; everything the subcommand
// itself reports, including a non-zero exit, is a result.
func dispatch(ctx context.Context, cfg serveConfig, d deps, req rpcRequest) rpcResponse {
	resp := rpcResponse{JSONRPC: "2.0", ID: req.ID}
	tool, ok := tools[req.Method]
	if !ok {
		resp.Error = &rpcError{Code: rpcMethodNotFound, Message: "unknown method " + req.Method}
		return resp
	}
	argv, err := tool(cfg, d, req.Params)
	if err != nil {
		resp.Error = &rpcError{Code: rpcInvalidParams, Message: err.Error()}
		return resp
	}
	var out, errOut bytes.Buffer
	code := run(argv, &out, &errOut, d)
	res := &ToolResult{ExitCode: code, Output: json.RawMessage("null"), Stderr: redact.String(strings.TrimSpace(errOut.String()))}
	if json.Valid(out.Bytes()) && out.Len() > 0 {
		res.Output = json.RawMessage(bytes.TrimSpace(out.Bytes()))
	}
	resp.Result = res
	return resp
}

// A tool turns validated params into argv for one subcommand. Inputs the platform sends as
// content (compose YAML, probes) are written into the cluster's workspace first, so the
// platform never names a file on the user's machine.
type tool func(cfg serveConfig, d deps, params json.RawMessage) ([]string, error)

var tools = map[string]tool{
	"validate": composeTool("validate"),
	"up":       composeTool("up"),
	"snapshot": nameTool("snapshot"),
	"down":     nameTool("down"),
	"verify":   verifyTool,
	"build":    buildTool,
	"inspect":  inspectTool,
}

// decode parses params strictly: an unknown field is an error, not silently ignored.
func decode(params json.RawMessage, v any) error {
	if len(params) == 0 {
		params = json.RawMessage("{}")
	}
	dec := json.NewDecoder(bytes.NewReader(params))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

func requireName(name string) error {
	if !workspace.ValidName(name) {
		return fmt.Errorf("invalid name %q", name)
	}
	return nil
}

func writeInput(d deps, cluster, file string, data []byte) (string, error) {
	w, err := openWorkspace(d)
	if err != nil {
		return "", err
	}
	return w.WriteFile(cluster, file, data)
}

func composeTool(cmd string) tool {
	return func(cfg serveConfig, d deps, params json.RawMessage) ([]string, error) {
		var p struct {
			Name    string   `json:"name"`
			Compose string   `json:"compose"`
			Allow   []string `json:"allow"`
		}
		if err := decode(params, &p); err != nil {
			return nil, err
		}
		if err := requireName(p.Name); err != nil {
			return nil, err
		}
		if p.Compose == "" {
			return nil, errors.New("compose is required")
		}
		allow, err := checkApprovals(cfg, p.Allow)
		if err != nil {
			return nil, err
		}
		path, err := writeInput(d, p.Name, "candidate.yaml", []byte(p.Compose))
		if err != nil {
			return nil, err
		}
		return []string{cmd, "-name", p.Name, "-f", path, "-allow", strings.Join(allow, ",")}, nil
	}
}

// checkApprovals returns the approvals the platform may pass on, each as "service:rule".
// The platform relays what the user approved in the web UI, but a compromised platform or a
// leaked token could claim any approval, so over RPC only rules the user listed on this
// machine with -allow-rule take effect. By default none do. An entry splits at its first
// colon: rule IDs such as "field:sysctls" contain colons, services do not.
func checkApprovals(cfg serveConfig, entries []string) ([]string, error) {
	approved := make([]string, 0, len(entries))
	for _, raw := range entries {
		a := strings.TrimSpace(raw) // the subcommand trims too; check what it will see
		if strings.Contains(a, ",") {
			return nil, fmt.Errorf("allow entry %q contains a comma", raw)
		}
		service, rule, ok := strings.Cut(a, ":")
		if !ok || service == "" || rule == "" {
			return nil, fmt.Errorf("allow entry %q is not service:rule", raw)
		}
		if !cfg.allowRules[rule] {
			return nil, fmt.Errorf("allow %q: rule %q is not in this Runner's -allow-rule list", a, rule)
		}
		approved = append(approved, a)
	}
	return approved, nil
}

func nameTool(cmd string) tool {
	return func(_ serveConfig, _ deps, params json.RawMessage) ([]string, error) {
		var p struct {
			Name string `json:"name"`
		}
		if err := decode(params, &p); err != nil {
			return nil, err
		}
		if err := requireName(p.Name); err != nil {
			return nil, err
		}
		return []string{cmd, "-name", p.Name}, nil
	}
}

func verifyTool(_ serveConfig, d deps, params json.RawMessage) ([]string, error) {
	var p struct {
		Name   string          `json:"name"`
		Probes json.RawMessage `json:"probes"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := requireName(p.Name); err != nil {
		return nil, err
	}
	argv := []string{"verify", "-name", p.Name}
	if len(p.Probes) > 0 && string(p.Probes) != "null" {
		path, err := writeInput(d, p.Name, "probes.json", p.Probes)
		if err != nil {
			return nil, err
		}
		argv = append(argv, "-probes", path)
	}
	return argv, nil
}

func buildTool(cfg serveConfig, _ deps, params json.RawMessage) ([]string, error) {
	var p struct {
		Name    string               `json:"name"`
		Service string               `json:"service"`
		Tag     string               `json:"tag"`
		Maven   string               `json:"maven"`
		Workdir string               `json:"workdir"`
		Stack   string               `json:"stack"`
		JDK     string               `json:"jdk"`
		Init    []initscripts.Script `json:"init"`
	}
	if err := decode(params, &p); err != nil {
		return nil, err
	}
	if err := requireName(p.Name); err != nil {
		return nil, err
	}
	argv := []string{"build", "-name", p.Name, "-service", p.Service, "-tag", p.Tag}
	switch {
	case p.Maven != "" && p.Workdir == "":
		argv = append(argv, "-maven", p.Maven)
		if cfg.settings != "" {
			argv = append(argv, "-settings", cfg.settings)
		}
		for _, s := range p.Init {
			argv = append(argv, "-init", s.Capability+"="+s.Path)
		}
	case p.Workdir != "" && p.Maven == "":
		if len(p.Init) > 0 {
			return nil, errors.New("init applies to maven builds; a workdir project declares its own scripts")
		}
		dir, err := allowedWorkdir(cfg, p.Workdir)
		if err != nil {
			return nil, err
		}
		argv = append(argv, "-workdir", dir)
	default:
		return nil, errors.New("build needs exactly one of maven or workdir")
	}
	if p.Stack != "" {
		argv = append(argv, "-stack", p.Stack)
	}
	if p.JDK != "" {
		argv = append(argv, "-jdk", p.JDK)
	}
	return argv, nil
}

func withinRoots(dir string, roots []string) bool {
	for _, r := range roots {
		if guard.Within(r, dir) {
			return true
		}
	}
	return false
}
