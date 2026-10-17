// Package dockercli runs the docker CLI and parses its JSON output. Callers build every
// argument themselves; nothing here passes through a shell.
package dockercli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

// Docker runs docker subcommands. Exec is the real implementation; tests use a fake.
type Docker interface {
	// Output returns stdout. A non-zero exit becomes an error carrying stderr.
	Output(ctx context.Context, args ...string) ([]byte, error)
	// Combined returns stdout and stderr interleaved, plus the exit error if any.
	Combined(ctx context.Context, args ...string) ([]byte, error)
}

// Exec runs the docker binary found on PATH.
type Exec struct{}

func (Exec) Output(ctx context.Context, args ...string) ([]byte, error) {
	var stderr bytes.Buffer
	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return out, fmt.Errorf("docker %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}

func (Exec) Combined(ctx context.Context, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, "docker", args...).CombinedOutput()
}

// ComposeProjectLabel is the label docker compose puts on every container it creates.
const ComposeProjectLabel = "com.docker.compose.project"

// ComposeServiceLabel names the compose service a container belongs to.
const ComposeServiceLabel = "com.docker.compose.service"

// Container is the part of `docker inspect` output the Runner uses.
type Container struct {
	ID           string `json:"Id"`
	Name         string `json:"Name"`
	RestartCount int    `json:"RestartCount"`
	State        struct {
		Status     string `json:"Status"` // created, running, restarting, exited, dead, ...
		Restarting bool   `json:"Restarting"`
		OOMKilled  bool   `json:"OOMKilled"`
		ExitCode   int    `json:"ExitCode"`
		Health     *struct {
			Status string `json:"Status"` // starting, healthy, unhealthy
			Log    []struct {
				ExitCode int    `json:"ExitCode"`
				Output   string `json:"Output"`
			} `json:"Log"`
		} `json:"Health"`
	} `json:"State"`
	Config struct {
		Labels map[string]string `json:"Labels"`
	} `json:"Config"`
}

// Service returns the compose service name of the container.
func (c Container) Service() string { return c.Config.Labels[ComposeServiceLabel] }

// ProjectContainers inspects every container, running or not, of a compose project.
func ProjectContainers(ctx context.Context, d Docker, project string) ([]Container, error) {
	ids, err := d.Output(ctx, "ps", "-a", "-q", "--no-trunc", "--filter", "label="+ComposeProjectLabel+"="+project)
	if err != nil {
		return nil, err
	}
	fields := strings.Fields(string(ids))
	if len(fields) == 0 {
		return nil, nil
	}
	out, err := d.Output(ctx, append([]string{"inspect"}, fields...)...)
	if err != nil {
		return nil, err
	}
	var cs []Container
	if err := json.Unmarshal(out, &cs); err != nil {
		return nil, fmt.Errorf("parse docker inspect: %w", err)
	}
	return cs, nil
}
