package guard

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestWithin(t *testing.T) {
	cases := []struct {
		root, path string
		want       bool
	}{
		{"/ws", "/ws", true},
		{"/ws", "/ws/a/b", true},
		{"/ws", "/ws2/a", false},
		{"/ws", "/other", false},
		{"/ws", "/ws/../etc", false},
	}
	for _, c := range cases {
		if got := Within(c.root, filepath.Clean(c.path)); got != c.want {
			t.Errorf("Within(%q, %q) = %v, want %v", c.root, c.path, got, c.want)
		}
	}
}

func TestGuard(t *testing.T) {
	ws := t.TempDir()
	src := t.TempDir()
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A symlink inside the workspace that points outside it.
	if err := os.Symlink(outside, filepath.Join(ws, "escape")); err != nil {
		t.Fatal(err)
	}
	g, err := New(ws, src)
	if err != nil {
		t.Fatal(err)
	}

	if _, err := g.CheckWrite(filepath.Join(ws, "compose.yaml")); err != nil {
		t.Errorf("write new file in workspace: %v", err)
	}
	if _, err := g.CheckRead(filepath.Join(src, "pom.xml")); err != nil {
		t.Errorf("read in read-only root: %v", err)
	}
	if _, err := g.CheckWrite(filepath.Join(src, "pom.xml")); !errors.Is(err, ErrOutside) {
		t.Errorf("write in read-only root: got %v, want ErrOutside", err)
	}
	if _, err := g.CheckRead(filepath.Join(ws, "..", "x")); !errors.Is(err, ErrOutside) {
		t.Errorf("dot-dot traversal: got %v, want ErrOutside", err)
	}
	if _, err := g.CheckRead(filepath.Join(ws, "escape", "secret")); !errors.Is(err, ErrOutside) {
		t.Errorf("symlink escape: got %v, want ErrOutside", err)
	}
}
