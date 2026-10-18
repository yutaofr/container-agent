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
