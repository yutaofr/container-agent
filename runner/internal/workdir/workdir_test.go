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
	d, err := LoadDescriptor(dir, "demo")
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
	if _, err := LoadDescriptor(dir, "demo"); err == nil || !strings.Contains(err.Error(), "sources.workdir is missing") {
		t.Errorf("got %v", err)
	}
	if _, err := LoadDescriptor(t.TempDir(), "demo"); err == nil {
		t.Error("missing descriptor accepted")
	}
}

// write creates dir/rel with body, making parent directories.
func write(t *testing.T, dir, rel, body string) {
	t.Helper()
	path := filepath.Join(dir, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLoadDescriptorPrefersThePerServiceFile(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, DescriptorPath, "name: other\nstack: dockerfile\nsources:\n  workdir: { dockerfile: Dockerfile }\n")
	write(t, dir, ServicesDir+"/api.yaml", "name: api\nstack: dockerfile\nsources:\n  workdir: { dockerfile: api/Dockerfile }\n")
	d, err := LoadDescriptor(dir, "api")
	if err != nil || d.Sources.Workdir.Dockerfile != "api/Dockerfile" {
		t.Fatalf("got %+v, %v", d, err)
	}
	// Without a per-service file the project's single descriptor is used, and must name the service.
	if _, err := LoadDescriptor(dir, "web"); err == nil || !strings.Contains(err.Error(), `names "other", not "web"`) {
		t.Errorf("got %v", err)
	}
}

func TestLoadDescriptorRejectsBadServiceNames(t *testing.T) {
	for _, svc := range []string{"", "../x", "a/b", "Api"} {
		if _, err := LoadDescriptor(t.TempDir(), svc); err == nil || !strings.Contains(err.Error(), "invalid service") {
			t.Errorf("%q: got %v", svc, err)
		}
	}
}

// The same fixture descriptors are checked by the platform's ServiceDescriptor tests, so the
// Runner and the platform agree on which workdir sources are valid.
func TestSharedDescriptorFixtures(t *testing.T) {
	root := filepath.Join("..", "..", "..", "platform", "tests", "fixtures", "descriptors")
	for _, kind := range []string{"valid", "invalid"} {
		paths, err := filepath.Glob(filepath.Join(root, kind, "*.yaml"))
		if err != nil || len(paths) == 0 {
			t.Fatalf("no %s fixtures: %v", kind, err)
		}
		for _, path := range paths {
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			_, err = ParseDescriptor(data, "demo")
			if (err == nil) != (kind == "valid") {
				t.Errorf("%s: err = %v", path, err)
			}
		}
	}
}

func TestDockerfileStackRunsTheOptionalBuildFirst(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, "api/Dockerfile", "FROM busybox\n")
	write(t, dir, DescriptorPath, "name: demo\nstack: dockerfile\nsources:\n  workdir:\n"+
		"    build: 'mkdir -p api/out && touch api/out/built'\n    dockerfile: api/Dockerfile\n    context: api\n")
	d, err := LoadDescriptor(dir, "demo")
	if err != nil {
		t.Fatal(err)
	}
	if artifact, err := Build(context.Background(), dir, d); err != nil || artifact != "" {
		t.Fatalf("artifact %q, err %v", artifact, err)
	}
	if _, err := os.Stat(filepath.Join(dir, "api", "out", "built")); err != nil {
		t.Error("build command did not run")
	}
	df, ctxDir, err := DockerInputs(dir, d)
	if err != nil {
		t.Fatal(err)
	}
	real, _ := filepath.EvalSymlinks(dir)
	if df != filepath.Join(real, "api", "Dockerfile") || ctxDir != filepath.Join(real, "api") {
		t.Errorf("dockerfile %s, context %s", df, ctxDir)
	}
}

func TestDockerInputsStayInsideTheProject(t *testing.T) {
	outside := t.TempDir()
	write(t, outside, "Dockerfile", "FROM busybox\n")
	dir := t.TempDir()
	if err := os.Symlink(filepath.Join(outside, "Dockerfile"), filepath.Join(dir, "Dockerfile")); err != nil {
		t.Fatal(err)
	}
	d := Descriptor{Name: "demo", Stack: StackDockerfile}
	d.Sources.Workdir = &Workdir{Dockerfile: "Dockerfile", Context: "."}
	if _, _, err := DockerInputs(dir, d); err == nil || !strings.Contains(err.Error(), "outside") {
		t.Errorf("symlinked Dockerfile: got %v", err)
	}
	d.Sources.Workdir.Dockerfile = "missing/Dockerfile"
	if _, _, err := DockerInputs(dir, d); err == nil {
		t.Error("missing Dockerfile accepted")
	}
}
