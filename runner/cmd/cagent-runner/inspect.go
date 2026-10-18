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
