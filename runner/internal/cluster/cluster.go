// Package cluster starts and stops Runner-managed compose projects.
package cluster

import (
	"context"
	"errors"
	"fmt"

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// ProjectPrefix keeps Runner projects apart from the user's own compose projects.
const ProjectPrefix = "cagent-"

// ErrNotManaged means a compose project holds a container without the managed label,
// so it is not the Runner's to touch.
var ErrNotManaged = errors.New("project has a container the Runner does not manage")

// Project returns the compose project name for a cluster.
func Project(name string) string { return ProjectPrefix + name }

// Up labels every service in p, writes p to the cluster's workspace, and starts it
// detached. It returns the path of the written compose file. Callers must validate p first.
func Up(ctx context.Context, d dockercli.Docker, w *workspace.Workspace, name string, p *types.Project) (string, error) {
	if !workspace.ValidName(name) {
		return "", fmt.Errorf("invalid cluster name %q", name)
	}
	compose.ApplyManagedLabels(p, name)
	data, err := p.MarshalYAML()
	if err != nil {
		return "", fmt.Errorf("marshal compose: %w", err)
	}
	path, err := w.WriteFile(name, "compose.yaml", data)
	if err != nil {
		return "", err
	}
	_, err = d.Output(ctx, "compose", "-p", Project(name), "-f", path, "up", "-d", "--remove-orphans")
	return path, err
}

// Down removes the cluster's containers, networks, and volumes. It refuses when any
// container in the project lacks the managed label.
func Down(ctx context.Context, d dockercli.Docker, name string) error {
	if !workspace.ValidName(name) {
		return fmt.Errorf("invalid cluster name %q", name)
	}
	cs, err := dockercli.ProjectContainers(ctx, d, Project(name))
	if err != nil {
		return err
	}
	for _, c := range cs {
		if !compose.IsManaged(c.Config.Labels) {
			return fmt.Errorf("%w: %s", ErrNotManaged, c.Name)
		}
	}
	_, err = d.Output(ctx, "compose", "-p", Project(name), "down", "-v", "--remove-orphans")
	return err
}
