// Command cagent-runner executes whitelisted cluster operations on the user's machine.
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

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/cluster"
	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/signals"
	"github.com/yutaofr/container-agent/runner/internal/verify"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// Exit codes shared by every subcommand.
const (
	exitOK       = 0
	exitFindings = 1 // validation findings, or a cluster that failed verification
	exitError    = 2
)

const usage = `usage:
  cagent-runner validate -f compose.yaml [-name N] [-workspace DIR] [-allow svc:rule,...]
  cagent-runner up       -name N -f compose.yaml [-workspace DIR] [-allow svc:rule,...]
  cagent-runner snapshot -name N
  cagent-runner verify   -name N [-probes probes.json]
  cagent-runner down     -name N`

// Report is the JSON that validate prints, and that up prints when it refuses to start.
type Report struct {
	Project       string                 `json:"project"`
	Findings      []compose.Finding      `json:"findings"`
	PortConflicts []compose.PortConflict `json:"port_conflicts"`
}

// deps are the process-level dependencies, injected so tests can replace them.
type deps struct {
	free   func(int) bool
	docker dockercli.Docker
	home   func() (string, error)
}

func main() {
	d := deps{free: compose.TCPPortFree, docker: dockercli.Exec{}, home: workspace.DefaultRoot}
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, d))
}

// run is main without process globals, so tests can drive it.
func run(args []string, stdout, stderr io.Writer, d deps) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, usage)
		return exitError
	}
	cmds := map[string]func([]string, io.Writer, io.Writer, deps) int{
		"validate": runValidate, "up": runUp, "snapshot": runSnapshot, "verify": runVerify, "down": runDown,
	}
	cmd, ok := cmds[args[0]]
	if !ok {
		fmt.Fprintln(stderr, usage)
		return exitError
	}
	return cmd(args[1:], stdout, stderr, d)
}

// composeFlags are shared by validate and up.
type composeFlags struct {
	file, name, workspace, allow *string
}

func addComposeFlags(fs *flag.FlagSet, defaultName string) composeFlags {
	return composeFlags{
		file:      fs.String("f", "compose.yaml", "compose file"),
		name:      fs.String("name", defaultName, "cluster name"),
		workspace: fs.String("workspace", "", "directory bind mounts may use (default: compose file dir)"),
		allow:     fs.String("allow", "", "approved exceptions, comma-separated service:rule"),
	}
}

// check loads a compose file under project and runs every validation. The returned
// project is the loaded one, ready to start when the report is clean.
func check(cf composeFlags, project string, free func(int) bool) (*types.Project, Report, error) {
	abs, err := filepath.Abs(*cf.file)
	if err != nil {
		return nil, Report{}, err
	}
	ws := *cf.workspace
	if ws == "" {
		ws = filepath.Dir(abs)
	}
	allow := map[string]bool{}
	for _, a := range strings.Split(*cf.allow, ",") {
		if a = strings.TrimSpace(a); a != "" {
			allow[a] = true
		}
	}
	ctx := context.Background()
	p, err := compose.Load(ctx, abs, project)
	if err != nil {
		return nil, Report{}, err
	}
	fields, err := compose.LoadFields(ctx, abs, project)
	if err != nil {
		return nil, Report{}, err
	}
	// Specific rules run unfiltered so the field check knows which keys they already cover;
	// approvals apply once, to the combined list.
	specific := compose.Lint(p, []string{ws}, nil)
	all := append(specific, compose.UnreviewedFields(fields, specific)...)
	rep := Report{
		Project:       p.Name,
		Findings:      compose.WithoutApproved(all, allow),
		PortConflicts: compose.HostPortConflicts(p, free),
	}
	return p, rep, nil
}

func (r Report) clean() bool { return len(r.Findings) == 0 && len(r.PortConflicts) == 0 }

func runValidate(args []string, stdout, stderr io.Writer, d deps) int {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cf := addComposeFlags(fs, "validate")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	_, rep, err := check(cf, *cf.name, d.free)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	if err := writeJSON(stdout, rep); err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	if !rep.clean() {
		return exitFindings
	}
	return exitOK
}

func runUp(args []string, stdout, stderr io.Writer, d deps) int {
	fs := flag.NewFlagSet("up", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cf := addComposeFlags(fs, "")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	if !workspace.ValidName(*cf.name) {
		fmt.Fprintf(stderr, "invalid or missing -name %q\n", *cf.name)
		return exitError
	}
	p, rep, err := check(cf, cluster.Project(*cf.name), d.free)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	if !rep.clean() {
		fmt.Fprintln(stderr, "refusing to start: validation findings")
		_ = writeJSON(stdout, rep)
		return exitFindings
	}
	w, err := openWorkspace(d)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	path, err := cluster.Up(context.Background(), d.docker, w, *cf.name, p)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return writeOrFail(stdout, stderr, map[string]string{"project": cluster.Project(*cf.name), "compose": path})
}

func runSnapshot(args []string, stdout, stderr io.Writer, d deps) int {
	name, ok := parseName("snapshot", args, stderr, nil)
	if !ok {
		return exitError
	}
	snap, err := signals.Collect(context.Background(), d.docker, cluster.Project(name), signals.DefaultOptions)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return writeOrFail(stdout, stderr, snap)
}

func runVerify(args []string, stdout, stderr io.Writer, d deps) int {
	var probesFile *string
	name, ok := parseName("verify", args, stderr, func(fs *flag.FlagSet) {
		probesFile = fs.String("probes", "", "JSON array of probes for L3")
	})
	if !ok {
		return exitError
	}
	var probes []verify.Probe
	if *probesFile != "" {
		data, err := os.ReadFile(*probesFile)
		if err == nil {
			err = json.Unmarshal(data, &probes)
		}
		if err != nil {
			fmt.Fprintf(stderr, "probes: %v\n", err)
			return exitError
		}
	}
	ctx := context.Background()
	snap, err := signals.Collect(ctx, d.docker, cluster.Project(name), signals.DefaultOptions)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	rep := verify.Verify(ctx, d.docker, snap, probes)
	if code := writeOrFail(stdout, stderr, rep); code != exitOK {
		return code
	}
	if !rep.Passed {
		return exitFindings
	}
	return exitOK
}

func runDown(args []string, stdout, stderr io.Writer, d deps) int {
	name, ok := parseName("down", args, stderr, nil)
	if !ok {
		return exitError
	}
	if err := cluster.Down(context.Background(), d.docker, name); err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return writeOrFail(stdout, stderr, map[string]string{"project": cluster.Project(name), "status": "removed"})
}

// parseName parses a subcommand that takes -name plus optional extra flags.
func parseName(cmd string, args []string, stderr io.Writer, extra func(*flag.FlagSet)) (string, bool) {
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	fs.SetOutput(stderr)
	name := fs.String("name", "", "cluster name")
	if extra != nil {
		extra(fs)
	}
	if err := fs.Parse(args); err != nil {
		return "", false
	}
	if !workspace.ValidName(*name) {
		fmt.Fprintf(stderr, "invalid or missing -name %q\n", *name)
		return "", false
	}
	return *name, true
}

func openWorkspace(d deps) (*workspace.Workspace, error) {
	root, err := d.home()
	if err != nil {
		return nil, err
	}
	return workspace.Open(root)
}

func writeJSON(w io.Writer, v any) error {
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func writeOrFail(stdout, stderr io.Writer, v any) int {
	if err := writeJSON(stdout, v); err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	return exitOK
}
