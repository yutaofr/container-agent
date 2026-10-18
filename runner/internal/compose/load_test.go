package compose

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeCompose writes content to compose.yaml in a fresh temp dir and returns its path.
func writeCompose(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadValid(t *testing.T) {
	path := writeCompose(t, `
services:
  web:
    image: nginx:1.27
    volumes: ["./data:/data:ro"]
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	web, ok := p.Services["web"]
	if !ok {
		t.Fatal("service web missing")
	}
	want := filepath.Join(filepath.Dir(path), "data")
	if got := web.Volumes[0].Source; got != want {
		t.Errorf("bind source = %q, want resolved %q", got, want)
	}
}

func TestLoadRejectsUnknownKey(t *testing.T) {
	path := writeCompose(t, `
services:
  web:
    image: nginx:1.27
    bogus_key: 1
`)
	if _, err := Load(context.Background(), path, "demo"); err == nil {
		t.Fatal("want schema error for unknown key, got nil")
	}
}

func TestLoadVerbatimKeepsEscapes(t *testing.T) {
	path := writeCompose(t, "services:\n  c:\n    image: busybox\n    command: [\"echo\", \"$$HOME\"]\n")
	p, err := LoadVerbatim(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	out, err := p.MarshalYAML()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), "$$HOME") {
		t.Errorf("escape lost:\n%s", out)
	}
}
