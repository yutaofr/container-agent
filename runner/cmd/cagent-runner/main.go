// Command cagent-runner executes whitelisted cluster operations on the user's machine.
// It currently ships only the offline "validate" subcommand.
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

	ctx := context.Background()
	p, err := compose.Load(ctx, abs, *name)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	fields, err := compose.LoadFields(ctx, abs, *name)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
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
