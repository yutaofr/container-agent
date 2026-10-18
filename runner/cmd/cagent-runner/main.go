// Command cagent-runner executes whitelisted cluster operations on the user's machine.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/builder"
	"github.com/yutaofr/container-agent/runner/internal/cluster"
	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/maven"
	"github.com/yutaofr/container-agent/runner/internal/signals"
	"github.com/yutaofr/container-agent/runner/internal/verify"
	"github.com/yutaofr/container-agent/runner/internal/workdir"
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
  cagent-runner down     -name N
  cagent-runner serve    -hub ws://HOST/runner [-token-env CAGENT_TOKEN] [-workdir-root DIR]... [-allow-rule RULE]... [-maven-settings F]
  cagent-runner build    -name N -service S -tag cagent/S:REF (-maven G:A:V[:EXT] [-settings F] | -workdir DIR) [-stack S] [-jdk V]
  cagent-runner inspect  -workdir DIR`

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
		"build": runBuild, "serve": runServe, "inspect": runInspect,
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
func check(cf composeFlags, project string, d deps) (*types.Project, Report, error) {
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
	own := ownPorts(ctx, d.docker, *cf.name)
	free := func(port int) bool { return own[port] || d.free(port) }
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

// ownPorts returns the host ports this cluster's own managed containers already publish.
// When a fix is re-applied, the running cluster must not count as a conflict with itself.
// Containers without the managed label do not count, and an unavailable docker means none.
func ownPorts(ctx context.Context, d dockercli.Docker, name string) map[int]bool {
	own := map[int]bool{}
	if !workspace.ValidName(name) {
		return own
	}
	cs, err := dockercli.ProjectContainers(ctx, d, cluster.Project(name))
	if err != nil {
		return own
	}
	for _, c := range cs {
		if compose.IsManaged(c.Config.Labels) {
			for _, p := range c.PublishedPorts() {
				own[p] = true
			}
		}
	}
	return own
}

func (r Report) clean() bool { return len(r.Findings) == 0 && len(r.PortConflicts) == 0 }

func runValidate(args []string, stdout, stderr io.Writer, d deps) int {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	cf := addComposeFlags(fs, "validate")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	_, rep, err := check(cf, *cf.name, d)
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
	_, rep, err := check(cf, cluster.Project(*cf.name), d)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	if !rep.clean() {
		fmt.Fprintln(stderr, "refusing to start: validation findings")
		_ = writeJSON(stdout, rep)
		return exitFindings
	}
	// Checks need interpolated values; the file docker compose reads must keep its "$$".
	abs, err := filepath.Abs(*cf.file)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
	}
	p, err := compose.LoadVerbatim(context.Background(), abs, cluster.Project(*cf.name))
	if err != nil {
		fmt.Fprintln(stderr, err)
		return exitError
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

// BuildResult is the JSON that build prints. On failure Error and Log are set instead of
// Cached, and the exit code is exitFindings so the platform can diagnose the log.
type BuildResult struct {
	Image   string   `json:"image"`
	Source  string   `json:"source"`
	Version string   `json:"version,omitempty"` // resolved repository version
	SHA256  string   `json:"sha256,omitempty"`
	Cached  bool     `json:"cached"`
	Error   string   `json:"error,omitempty"`
	Log     []string `json:"log,omitempty"`
}

func runBuild(args []string, stdout, stderr io.Writer, d deps) int {
	fs := flag.NewFlagSet("build", flag.ContinueOnError)
	fs.SetOutput(stderr)
	name := fs.String("name", "", "cluster name")
	service := fs.String("service", "", "compose service the image is for")
	tag := fs.String("tag", "", "image tag, cagent/<service>:<ref>")
	coord := fs.String("maven", "", "repository artifact, group:artifact:version[:ext]")
	dir := fs.String("workdir", "", "project root holding .cagent/services/<service>.yaml or .cagent/service.yaml")
	settings := fs.String("settings", "", "Maven settings.xml (default ~/.m2/settings.xml)")
	stack := fs.String("stack", "", "builder stack; for -workdir it must match the project's descriptor (default java-springboot)")
	jdk := fs.String("jdk", "", "Java major version (default "+builder.DefaultJDK+")")
	if err := fs.Parse(args); err != nil {
		return exitError
	}
	usageErr := func(msg string) int { fmt.Fprintln(stderr, msg); return exitError }
	switch {
	case !workspace.ValidName(*name) || !workspace.ValidName(*service):
		return usageErr("build needs valid -name and -service")
	case !builder.ValidTag(*tag):
		return usageErr(fmt.Sprintf("invalid -tag %q: must be cagent/<service>:<ref>", *tag))
	case (*coord == "") == (*dir == ""):
		return usageErr("build needs exactly one of -maven or -workdir")
	}
	w, err := openWorkspace(d)
	if err != nil {
		return usageErr(err.Error())
	}
	buildDir, err := w.BuildDir(*name, *service)
	if err != nil {
		return usageErr(err.Error())
	}

	ctx := context.Background()
	res := BuildResult{Image: *tag}
	// failed reports a build that ran and failed: the platform gets the error and log.
	failed := func(err error) int {
		res.Error = err.Error()
		var wb *workdir.BuildError
		var db *builder.BuildError
		switch {
		case errors.As(err, &wb):
			res.Error, res.Log = wb.Err.Error(), wb.Log
		case errors.As(err, &db):
			res.Error, res.Log = db.Err.Error(), db.Log
		}
		_ = writeJSON(stdout, res)
		return exitFindings
	}

	if *coord != "" {
		c, err := maven.ParseCoord(*coord)
		if err != nil {
			return usageErr(err.Error())
		}
		path := *settings
		if path == "" {
			if path, err = maven.DefaultSettingsPath(); err != nil {
				return usageErr(err.Error())
			}
		}
		repo, err := maven.LoadRepo(path)
		if err != nil {
			return usageErr(err.Error())
		}
		client := maven.Client{Repo: repo}
		art, err := client.Resolve(ctx, c)
		if err != nil {
			return failed(err)
		}
		res.Source, res.Version = "maven:"+c.String(), art.Resolved
		if _, res.SHA256, err = client.Download(ctx, art, buildDir, builder.ArtifactFile); err != nil {
			return failed(err)
		}
	} else {
		abs, err := filepath.Abs(*dir)
		if err != nil {
			return usageErr(err.Error())
		}
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
		src, err := os.Open(artifact)
		if err != nil {
			return failed(err)
		}
		_, res.SHA256, err = workspace.WriteNoFollow(buildDir, builder.ArtifactFile, src)
		src.Close()
		if err != nil {
			return failed(err)
		}
	}
	if *stack == "" {
		*stack = "java-springboot"
	}
	res.Cached, err = builder.Build(ctx, d.docker, builder.Request{
		Tag: *tag, Stack: *stack, JDK: *jdk, Dir: buildDir, SHA256: res.SHA256,
	})
	if err != nil {
		return failed(err)
	}
	return writeOrFail(stdout, stderr, res)
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
