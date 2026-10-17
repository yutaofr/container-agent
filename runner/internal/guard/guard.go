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
	ws, err := Resolve(workspace)
	if err != nil {
		return nil, fmt.Errorf("workspace: %w", err)
	}
	g := &Guard{workspace: ws}
	for _, r := range readOnly {
		rr, err := Resolve(r)
		if err != nil {
			return nil, fmt.Errorf("read-only root %s: %w", r, err)
		}
		g.readOnly = append(g.readOnly, rr)
	}
	return g, nil
}

// CheckRead returns the resolved path if it lies in the workspace or a read-only root.
func (g *Guard) CheckRead(path string) (string, error) {
	p, err := Resolve(path)
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
	p, err := Resolve(path)
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

// Resolve makes path absolute and follows symlinks. Missing trailing elements are allowed
// so callers can check a file before creating it; they cannot be symlinks, so they are
// appended to the resolved nearest existing ancestor. A dangling symlink anywhere on the
// path is refused with ErrOutside: its target is unknown, so a write through it could
// land anywhere.
func Resolve(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	var missing []string
	cur := abs
	for {
		real, err := filepath.EvalSymlinks(cur)
		if err == nil {
			return filepath.Join(append([]string{real}, missing...)...), nil
		}
		if !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		if fi, lerr := os.Lstat(cur); lerr == nil && fi.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("%w: dangling symlink %s", ErrOutside, cur)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return "", err
		}
		missing = append([]string{filepath.Base(cur)}, missing...)
		cur = parent
	}
}
