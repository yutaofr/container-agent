package workspace

import (
	"os"
	"path/filepath"
	"testing"
)

func TestValidName(t *testing.T) {
	for _, n := range []string{"orders-poc", "a", "x_1"} {
		if !ValidName(n) {
			t.Errorf("ValidName(%q) = false", n)
		}
	}
	for _, n := range []string{"", "-rf", "Orders", "a/b", "../x", "a b"} {
		if ValidName(n) {
			t.Errorf("ValidName(%q) = true", n)
		}
	}
}

func TestWriteFile(t *testing.T) {
	w, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path, err := w.WriteFile("demo", "compose.yaml", []byte("services: {}\n"))
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(filepath.Dir(path)) != "demo" {
		t.Errorf("written to %s, want inside clusters/demo", path)
	}
	got, _ := os.ReadFile(path)
	if string(got) != "services: {}\n" {
		t.Errorf("content = %q", got)
	}
	fi, _ := os.Stat(path)
	if fi.Mode().Perm() != 0o600 {
		t.Errorf("mode = %v, want 0600", fi.Mode().Perm())
	}
}

func TestWriteFileRefusesSymlink(t *testing.T) {
	root := t.TempDir()
	w, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := w.ClusterDir("demo")
	if err != nil {
		t.Fatal(err)
	}
	// A link that stays inside the workspace passes the guard, but O_NOFOLLOW still
	// refuses to write through it.
	target := filepath.Join(dir, "real.yaml")
	if err := os.WriteFile(target, []byte("original"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(dir, "compose.yaml")); err != nil {
		t.Fatal(err)
	}
	if _, err := w.WriteFile("demo", "compose.yaml", []byte("evil")); err == nil {
		t.Fatal("write through a symlink succeeded")
	}
	if got, _ := os.ReadFile(target); string(got) != "original" {
		t.Errorf("link target modified: %q", got)
	}
}

func TestRejectsBadNames(t *testing.T) {
	w, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.ClusterDir("../escape"); err == nil {
		t.Error("ClusterDir accepted ../escape")
	}
	for _, file := range []string{"../x", "sub/x", "..", "."} {
		if _, err := w.WriteFile("demo", file, nil); err == nil {
			t.Errorf("WriteFile accepted file name %q", file)
		}
	}
}
