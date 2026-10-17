// Package workspace manages the Runner's per-cluster directories under ~/.cagent/clusters.
package workspace

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"syscall"

	"github.com/yutaofr/container-agent/runner/internal/guard"
)

// namePattern matches cluster names. It also keeps names from looking like CLI flags.
var namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)

// ValidName reports whether name can be used as a cluster name.
func ValidName(name string) bool { return namePattern.MatchString(name) }

// Workspace is the Runner's home directory, normally ~/.cagent.
type Workspace struct{ root string }

// Open returns the workspace at root, creating it if needed.
func Open(root string) (*Workspace, error) {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	return &Workspace{root: root}, nil
}

// DefaultRoot is $CAGENT_HOME, or ~/.cagent when it is unset.
func DefaultRoot() (string, error) {
	if r := os.Getenv("CAGENT_HOME"); r != "" {
		return r, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".cagent"), nil
}

// ClusterDir returns the directory for one cluster, creating it if needed.
func (w *Workspace) ClusterDir(name string) (string, error) {
	if !ValidName(name) {
		return "", fmt.Errorf("invalid cluster name %q", name)
	}
	dir := filepath.Join(w.root, "clusters", name)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	return dir, nil
}

// WriteFile writes data to file, a plain file name, inside the cluster's directory. The
// guard rejects a path that resolves outside the directory. The open then uses the declared
// path, not the resolved one, with O_NOFOLLOW, so it refuses any symlink in the final
// element, including one planted after the check: this closes the check-then-write race.
func (w *Workspace) WriteFile(cluster, file string, data []byte) (string, error) {
	if file != filepath.Base(file) || file == "." || file == ".." {
		return "", fmt.Errorf("invalid file name %q", file)
	}
	dir, err := w.ClusterDir(cluster)
	if err != nil {
		return "", err
	}
	g, err := guard.New(dir)
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, file)
	if _, err := g.CheckWrite(path); err != nil {
		return "", err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return "", fmt.Errorf("open %s: %w", path, err)
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return "", err
	}
	return path, f.Close()
}
