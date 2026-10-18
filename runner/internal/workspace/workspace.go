// Package workspace manages the Runner's per-cluster directories under ~/.cagent/clusters.
package workspace

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
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

// WriteFile writes data to file, a plain file name, inside the cluster's directory.
func (w *Workspace) WriteFile(cluster, file string, data []byte) (string, error) {
	dir, err := w.ClusterDir(cluster)
	if err != nil {
		return "", err
	}
	path, _, err := WriteNoFollow(dir, file, bytes.NewReader(data))
	return path, err
}

// BuildDir returns the docker build context for one service of a cluster, creating it
// if needed.
func (w *Workspace) BuildDir(cluster, service string) (string, error) {
	if !ValidName(service) {
		return "", fmt.Errorf("invalid service name %q", service)
	}
	dir, err := w.ClusterDir(cluster)
	if err != nil {
		return "", err
	}
	build := filepath.Join(dir, "build", service)
	if err := os.MkdirAll(build, 0o700); err != nil {
		return "", err
	}
	return build, nil
}

// InitDir returns where one service's init scripts are staged. It is not created: staging
// replaces it whole.
func (w *Workspace) InitDir(cluster, service string) (string, error) {
	if !ValidName(service) {
		return "", fmt.Errorf("invalid service name %q", service)
	}
	dir, err := w.ClusterDir(cluster)
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "init", service), nil
}

// WriteNoFollow streams r into file, a plain file name inside dir, and returns the path
// and the hex SHA-256 of the bytes written. The guard rejects a path that resolves outside
// dir. The open then uses the declared path, not the resolved one, with O_NOFOLLOW, so it
// refuses any symlink in the final element, including one planted after the check: this
// closes the check-then-write race. A failed copy removes the partial file.
func WriteNoFollow(dir, file string, r io.Reader) (string, string, error) {
	if file != filepath.Base(file) || file == "." || file == ".." {
		return "", "", fmt.Errorf("invalid file name %q", file)
	}
	g, err := guard.New(dir)
	if err != nil {
		return "", "", err
	}
	path := filepath.Join(dir, file)
	if _, err := g.CheckWrite(path); err != nil {
		return "", "", err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return "", "", fmt.Errorf("open %s: %w", path, err)
	}
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(f, h), r)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(path)
		return "", "", fmt.Errorf("write %s: %w", path, err)
	}
	return path, hex.EncodeToString(h.Sum(nil)), nil
}
