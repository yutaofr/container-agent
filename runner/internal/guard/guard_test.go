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

// A dangling symlink has no real target to resolve, so a naive "missing final element"
// fallback would report it as inside the workspace while a write would follow it out.
func TestGuardRejectsDanglingSymlink(t *testing.T) {
	ws := t.TempDir()
	outside := t.TempDir()
	link := filepath.Join(ws, "dangling")
	if err := os.Symlink(filepath.Join(outside, "not-yet"), link); err != nil {
		t.Fatal(err)
	}
	g, err := New(ws)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := g.CheckWrite(link); !errors.Is(err, ErrOutside) {
		t.Errorf("write through dangling symlink: got %v, want ErrOutside", err)
	}
	if _, err := g.CheckRead(link); !errors.Is(err, ErrOutside) {
		t.Errorf("read through dangling symlink: got %v, want ErrOutside", err)
	}
}

func TestResolve(t *testing.T) {
	base := t.TempDir()
	real, err := filepath.EvalSymlinks(base)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(real, "target")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(base, "link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(real, "gone"), filepath.Join(base, "dangling")); err != nil {
		t.Fatal(err)
	}

	ok := []struct{ name, in, want string }{
		{"existing path", base, real},
		{"through symlink", filepath.Join(base, "link"), target},
		{"missing leaf", filepath.Join(base, "new"), filepath.Join(real, "new")},
		{"several missing levels", filepath.Join(base, "a", "b", "c"), filepath.Join(real, "a", "b", "c")},
		{"missing below symlinked dir", filepath.Join(base, "link", "x", "y"), filepath.Join(target, "x", "y")},
	}
	for _, c := range ok {
		t.Run(c.name, func(t *testing.T) {
			got, err := Resolve(c.in)
			if err != nil {
				t.Fatal(err)
			}
			if got != c.want {
				t.Errorf("Resolve(%q) = %q, want %q", c.in, got, c.want)
			}
		})
	}

	for _, in := range []string{
		filepath.Join(base, "dangling"),
		filepath.Join(base, "dangling", "sub"),
	} {
		if _, err := Resolve(in); !errors.Is(err, ErrOutside) {
			t.Errorf("Resolve(%q): got %v, want ErrOutside", in, err)
		}
	}
}
