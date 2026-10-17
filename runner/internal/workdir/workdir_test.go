package workdir

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// project creates a working directory with a descriptor using build and output.
func project(t *testing.T, build, output string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	desc := "name: demo\nkind: app\nstack: java-springboot\nsources:\n  workdir:\n" +
		"    build: '" + build + "'\n    output: '" + output + "'\nruntime: {}\n"
	if err := os.WriteFile(filepath.Join(dir, DescriptorPath), []byte(desc), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func build(t *testing.T, dir string) (string, error) {
	t.Helper()
	d, err := LoadDescriptor(dir)
	if err != nil {
		t.Fatal(err)
	}
	return Build(context.Background(), dir, d)
}

func TestBuildReturnsTheArtifact(t *testing.T) {
	dir := project(t, "mkdir -p target && echo jar > target/demo.jar && echo x > target/demo.jar.original", "target/*.jar")
	got, err := build(t, dir)
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(got) != "demo.jar" {
		t.Errorf("artifact = %s", got)
	}
}

func TestBuildFailureCarriesRedactedLog(t *testing.T) {
	dir := project(t, "echo compiling; echo password=hunter2; echo BUILD FAILURE; exit 1", "target/*.jar")
	_, err := build(t, dir)
	var be *BuildError
	if !errors.As(err, &be) {
		t.Fatalf("got %v, want BuildError", err)
	}
	log := strings.Join(be.Log, "\n")
	if strings.Contains(log, "hunter2") || !strings.Contains(log, "BUILD FAILURE") {
		t.Errorf("log = %q", log)
	}
}

func TestBuildNeedsExactlyOneArtifact(t *testing.T) {
	dir := project(t, "mkdir -p target && touch target/a.jar target/a-sources.jar", "target/*.jar")
	if _, err := build(t, dir); err == nil || !strings.Contains(err.Error(), "matched 2 files") {
		t.Errorf("got %v", err)
	}
}

func TestBuildOutputMustStayInside(t *testing.T) {
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "evil.jar"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	dir := project(t, "mkdir -p target && ln -sf "+outside+"/evil.jar target/app.jar", "target/*.jar")
	if _, err := build(t, dir); err == nil || !strings.Contains(err.Error(), "resolves outside") {
		t.Errorf("symlinked artifact: got %v", err)
	}
}

func TestLoadDescriptorNeedsWorkdirSource(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	body := "name: demo\nsources:\n  release: { nexus: 'a:b:{version}:jar' }\n"
	if err := os.WriteFile(filepath.Join(dir, DescriptorPath), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadDescriptor(dir); err == nil || !strings.Contains(err.Error(), "needs build and output") {
		t.Errorf("got %v", err)
	}
	if _, err := LoadDescriptor(t.TempDir()); err == nil {
		t.Error("missing descriptor accepted")
	}
}
