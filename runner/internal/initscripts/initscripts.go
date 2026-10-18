// Package initscripts stages the SQL and CQL scripts a service declares for its middleware
// (M7c). Scripts come from the project directory or from the downloaded jar and are copied
// into the cluster's workspace, where a one-shot init service mounts them read-only.
//
// Staged files are named <capability>/NN-<basename>, NN being the declared order within
// the capability. The source's directory part is never used, so a script name cannot place
// a file anywhere else.
package initscripts

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/guard"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

const (
	MaxScripts = 20      // per capability, as in the platform's descriptor schema
	MaxBytes   = 1 << 20 // per script
)

// Script is one declared script: the capability whose middleware runs it and its path,
// relative to the project root or, in a jar, a classpath path.
type Script struct {
	Capability string `json:"capability"`
	Path       string `json:"path"`
}

// Staged is one staged script.
type Staged struct {
	Capability string `json:"capability"`
	File       string `json:"file"`
	SHA256     string `json:"sha256"`
}

// Check validates names, paths, and counts before anything is read.
func Check(scripts []Script) error {
	count := map[string]int{}
	for _, s := range scripts {
		if !workspace.ValidName(s.Capability) {
			return fmt.Errorf("invalid capability %q", s.Capability)
		}
		if !Relative(s.Path) {
			return fmt.Errorf("script %q must be a relative path inside the project", s.Path)
		}
		if count[s.Capability]++; count[s.Capability] > MaxScripts {
			return fmt.Errorf("%s: more than %d scripts", s.Capability, MaxScripts)
		}
	}
	return nil
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

// FromDir stages scripts from the project at root into dest, replacing what dest held.
// Each script must resolve inside root and be a regular file of at most MaxBytes.
func FromDir(root string, scripts []Script, dest string) ([]Staged, error) {
	if err := Check(scripts); err != nil {
		return nil, err
	}
	base, err := guard.Resolve(root)
	if err != nil {
		return nil, err
	}
	return stage(scripts, dest, func(s Script) ([]byte, error) {
		p, err := guard.Resolve(filepath.Join(base, s.Path))
		if err != nil {
			return nil, err
		}
		if !guard.Within(base, p) {
			return nil, errors.New("resolves outside the project")
		}
		fi, err := os.Stat(p)
		if err != nil {
			return nil, errors.New("not found")
		}
		if !fi.Mode().IsRegular() {
			return nil, errors.New("not a regular file")
		}
		f, err := os.Open(p)
		if err != nil {
			return nil, err
		}
		defer f.Close()
		return readLimited(f)
	})
}

// stage clears dest, then reads and writes each script in order.
func stage(scripts []Script, dest string, read func(Script) ([]byte, error)) ([]Staged, error) {
	if err := os.RemoveAll(dest); err != nil {
		return nil, err
	}
	out := make([]Staged, 0, len(scripts))
	n := map[string]int{}
	for _, s := range scripts {
		data, err := read(s)
		if err != nil {
			return nil, fmt.Errorf("init script %s: %w", s.Path, err)
		}
		dir := filepath.Join(dest, s.Capability)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
		n[s.Capability]++
		file := fmt.Sprintf("%02d-%s", n[s.Capability], filepath.Base(filepath.Clean(s.Path)))
		_, sum, err := workspace.WriteNoFollow(dir, file, bytes.NewReader(data))
		if err != nil {
			return nil, err
		}
		out = append(out, Staged{Capability: s.Capability, File: file, SHA256: sum})
	}
	return out, nil
}

// readLimited reads at most MaxBytes; a longer input is an error, whatever its header said.
func readLimited(r io.Reader) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(r, MaxBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > MaxBytes {
		return nil, fmt.Errorf("larger than %d bytes", MaxBytes)
	}
	return data, nil
}
