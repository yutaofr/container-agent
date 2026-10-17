// Package workdir builds a project in the developer's working directory with their own
// toolchain. The build command comes only from the project's .cagent/service.yaml in that
// directory, never from the platform: the platform can name a directory to build, but it
// cannot make the Runner run a command the developer did not write.
package workdir

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go.yaml.in/yaml/v3"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/redact"
)

// DescriptorPath is where a project keeps its descriptor, relative to its root.
const DescriptorPath = ".cagent/service.yaml"

// LogTail is how many redacted lines of a failed build's output are reported.
const LogTail = 40

// Descriptor is the part of service.yaml the Runner needs. The platform validates the
// full schema; unknown keys are ignored here.
type Descriptor struct {
	Name    string `yaml:"name"`
	Stack   string `yaml:"stack"`
	Sources struct {
		Workdir *struct {
			Build  string `yaml:"build"`
			Output string `yaml:"output"`
		} `yaml:"workdir"`
	} `yaml:"sources"`
}

// LoadDescriptor reads dir/.cagent/service.yaml and checks it declares a workdir source.
func LoadDescriptor(dir string) (Descriptor, error) {
	path := filepath.Join(dir, DescriptorPath)
	data, err := os.ReadFile(path)
	if err != nil {
		return Descriptor{}, err
	}
	var d Descriptor
	if err := yaml.Unmarshal(data, &d); err != nil {
		return Descriptor{}, fmt.Errorf("parse %s: %w", path, err)
	}
	if w := d.Sources.Workdir; w == nil || w.Build == "" || w.Output == "" {
		return Descriptor{}, fmt.Errorf("%s: sources.workdir needs build and output", path)
	}
	return d, nil
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

// Build runs the descriptor's build command in dir and returns the single artifact its
// output glob matches. The artifact must resolve inside dir.
func Build(ctx context.Context, dir string, d Descriptor) (string, error) {
	cmd := exec.CommandContext(ctx, "sh", "-c", d.Sources.Workdir.Build)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", &BuildError{Err: err, Log: tail(out)}
	}
	matches, err := filepath.Glob(filepath.Join(dir, d.Sources.Workdir.Output))
	if err != nil {
		return "", err
	}
	if len(matches) != 1 {
		return "", fmt.Errorf("output %q matched %d files, want exactly 1: %v", d.Sources.Workdir.Output, len(matches), matches)
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
