// Package compose loads, checks, and labels compose projects using compose-go,
// the same loader docker compose uses.
package compose

import (
	"context"
	"fmt"
	"sort"

	"github.com/compose-spec/compose-go/v2/cli"
	"github.com/compose-spec/compose-go/v2/types"
)

// Load parses and schema-validates a compose file. Relative paths become absolute.
func Load(ctx context.Context, path, name string) (*types.Project, error) {
	opts, err := cli.NewProjectOptions([]string{path},
		cli.WithName(name),
		cli.WithResolvedPaths(true),
	)
	if err != nil {
		return nil, fmt.Errorf("compose options: %w", err)
	}
	p, err := opts.LoadProject(ctx)
	if err != nil {
		return nil, fmt.Errorf("load %s: %w", path, err)
	}
	return p, nil
}

// LoadVerbatim loads a compose file like Load but without variable interpolation, for
// writing the project back out. MarshalYAML does not re-escape "$", so an interpolated
// project written to disk would turn each "$$" escape into "$", and docker compose would
// then interpolate it a second time from the host environment.
func LoadVerbatim(ctx context.Context, path, name string) (*types.Project, error) {
	opts, err := cli.NewProjectOptions([]string{path},
		cli.WithName(name),
		cli.WithResolvedPaths(true),
		cli.WithInterpolation(false),
	)
	if err != nil {
		return nil, fmt.Errorf("compose options: %w", err)
	}
	p, err := opts.LoadProject(ctx)
	if err != nil {
		return nil, fmt.Errorf("load %s: %w", path, err)
	}
	return p, nil
}

// serviceNames returns service names in a stable order so reports are deterministic.
func serviceNames(p *types.Project) []string {
	names := make([]string, 0, len(p.Services))
	for n := range p.Services {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}
