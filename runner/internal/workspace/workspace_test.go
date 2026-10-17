package workspace

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
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

func TestBuildDir(t *testing.T) {
	root := t.TempDir()
	w, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := w.BuildDir("demo", "order-service")
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(root, "clusters", "demo", "build", "order-service"); dir != want {
		t.Errorf("BuildDir = %s, want %s", dir, want)
	}
	if _, err := w.BuildDir("demo", "../x"); err == nil {
		t.Error("BuildDir accepted ../x")
	}
}

func TestWriteNoFollowHashes(t *testing.T) {
	dir := t.TempDir()
	path, sum, err := WriteNoFollow(dir, "app.jar", strings.NewReader("hello"))
	if err != nil {
		t.Fatal(err)
	}
	// sha256("hello")
	if sum != "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" {
		t.Errorf("sha256 = %s", sum)
	}
	if got, _ := os.ReadFile(path); string(got) != "hello" {
		t.Errorf("content = %q", got)
	}
}

type failingReader struct{}

func (failingReader) Read([]byte) (int, error) { return 0, errors.New("connection reset") }

func TestWriteNoFollowRemovesPartialFile(t *testing.T) {
	dir := t.TempDir()
	if _, _, err := WriteNoFollow(dir, "app.jar", failingReader{}); err == nil {
		t.Fatal("want error from failing reader")
	}
	if _, err := os.Stat(filepath.Join(dir, "app.jar")); !os.IsNotExist(err) {
		t.Errorf("partial file left behind: %v", err)
	}
}
