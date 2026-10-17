// Package guard confines Runner file access to approved directories.
package guard

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrOutside is returned when a path resolves outside every allowed root.
var ErrOutside = errors.New("path outside allowed roots")

// Guard allows reads and writes inside a workspace, and reads inside extra read-only roots.
type Guard struct {
	workspace string
	readOnly  []string
}

// New builds a Guard. Roots are resolved through symlinks so later checks compare real paths.
func New(workspace string, readOnly ...string) (*Guard, error) {
	ws, err := resolve(workspace)
	if err != nil {
		return nil, fmt.Errorf("workspace: %w", err)
	}
	g := &Guard{workspace: ws}
	for _, r := range readOnly {
		rr, err := resolve(r)
		if err != nil {
			return nil, fmt.Errorf("read-only root %s: %w", r, err)
		}
		g.readOnly = append(g.readOnly, rr)
	}
	return g, nil
}

// CheckRead returns the resolved path if it lies in the workspace or a read-only root.
func (g *Guard) CheckRead(path string) (string, error) {
	p, err := resolve(path)
	if err != nil {
		return "", err
	}
	if Within(g.workspace, p) {
		return p, nil
	}
	for _, r := range g.readOnly {
		if Within(r, p) {
			return p, nil
		}
	}
	return "", fmt.Errorf("%w: %s", ErrOutside, path)
}

// CheckWrite returns the resolved path if it lies in the workspace.
func (g *Guard) CheckWrite(path string) (string, error) {
	p, err := resolve(path)
	if err != nil {
		return "", err
	}
	if !Within(g.workspace, p) {
		return "", fmt.Errorf("%w: %s", ErrOutside, path)
	}
	return p, nil
}

// Within reports whether path equals root or sits below it. It compares paths lexically.
func Within(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// resolve makes path absolute and follows symlinks. A missing final element is allowed
// so callers can check a file before creating it.
func resolve(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	real, err := filepath.EvalSymlinks(abs)
	if err == nil {
		return real, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	// EvalSymlinks also fails for a dangling symlink. Its target is unknown, so a write
	// through it could land anywhere; only a truly absent final element may fall through.
	if fi, lerr := os.Lstat(abs); lerr == nil && fi.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("%w: dangling symlink %s", ErrOutside, path)
	}
	parent, err := filepath.EvalSymlinks(filepath.Dir(abs))
	if err != nil {
		return "", err
	}
	return filepath.Join(parent, filepath.Base(abs)), nil
}
