package builder

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
)

const (
	tag     = "cagent/order-service:release-1.4.2"
	sha     = "abc123"
	inspect = `image inspect --format {{index .Config.Labels "cagent.artifact-sha256"}} ` + tag
)

func buildCmd(dir string) string {
	return "build -t " + tag + " --label cagent.managed=true --label cagent.artifact-sha256=" + sha +
		" -f " + filepath.Join(dir, "Dockerfile") + " " + dir
}

func TestDockerfile(t *testing.T) {
	df, err := Dockerfile("java-springboot", "17")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"FROM eclipse-temurin:17-jre", "COPY app.jar /app/app.jar", "exec java $JAVA_OPTS -jar /app/app.jar"} {
		if !strings.Contains(df, want) {
			t.Errorf("Dockerfile lacks %q:\n%s", want, df)
		}
	}
	if _, err := Dockerfile("cobol", "17"); err == nil {
		t.Error("unknown stack accepted")
	}
	if _, err := Dockerfile("java-springboot", "17; rm -rf /"); err == nil {
		t.Error("bad jdk accepted")
	}
}

func TestBuildWritesDockerfileAndBuilds(t *testing.T) {
	dir := t.TempDir()
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		inspect:       {Err: errors.New("No such image")},
		buildCmd(dir): {Out: "Successfully built"},
	}}
	cached, err := Build(context.Background(), f, Request{Tag: tag, Stack: "java-springboot", Dir: dir, SHA256: sha})
	if err != nil || cached {
		t.Fatalf("got cached=%v err=%v", cached, err)
	}
	df, _ := os.ReadFile(filepath.Join(dir, "Dockerfile"))
	if !strings.Contains(string(df), "eclipse-temurin:"+DefaultJDK+"-jre") {
		t.Errorf("Dockerfile not written with default JDK:\n%s", df)
	}
}

func TestBuildDockerfileUsesTheProjectsDockerfile(t *testing.T) {
	cmd := "build -t " + tag + " --label cagent.managed=true -f /p/api/Dockerfile /p/api"
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{cmd: {}}}
	if err := BuildDockerfile(context.Background(), f, tag, "/p/api/Dockerfile", "/p/api"); err != nil {
		t.Fatal(err)
	}
	if len(f.Calls) != 1 {
		t.Errorf("calls %v", f.Calls)
	}
}

func TestBuildDockerfileFailureCarriesRedactedLog(t *testing.T) {
	cmd := "build -t " + tag + " --label cagent.managed=true -f /p/Dockerfile /p"
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		cmd: {Out: "step 2\npassword=hunter2\nERROR: failed to solve", Err: errors.New("exit status 1")},
	}}
	err := BuildDockerfile(context.Background(), f, tag, "/p/Dockerfile", "/p")
	var be *BuildError
	if !errors.As(err, &be) || strings.Contains(strings.Join(be.Log, "\n"), "hunter2") {
		t.Fatalf("got %v", err)
	}
	if err := BuildDockerfile(context.Background(), f, "nginx:latest", "/p/Dockerfile", "/p"); err == nil {
		t.Error("tag outside cagent/ accepted")
	}
}

func TestBuildReusesImageWithSameArtifact(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{inspect: {Out: sha + "\n"}}}
	cached, err := Build(context.Background(), f, Request{Tag: tag, Stack: "java-springboot", Dir: t.TempDir(), SHA256: sha})
	if err != nil || !cached {
		t.Fatalf("got cached=%v err=%v", cached, err)
	}
	if len(f.Calls) != 1 {
		t.Errorf("build ran despite cache hit: %v", f.Calls)
	}
}

func TestBuildRebuildsWhenArtifactChanged(t *testing.T) {
	dir := t.TempDir()
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		inspect:       {Out: "old-sha\n"},
		buildCmd(dir): {},
	}}
	if cached, err := Build(context.Background(), f, Request{Tag: tag, Stack: "java-springboot", Dir: dir, SHA256: sha}); err != nil || cached {
		t.Fatalf("got cached=%v err=%v", cached, err)
	}
}

func TestBuildFailureCarriesRedactedLog(t *testing.T) {
	dir := t.TempDir()
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		inspect:       {Err: errors.New("No such image")},
		buildCmd(dir): {Out: "step 1\nERROR: pull access denied token=abc\n", Err: errors.New("exit 1")},
	}}
	_, err := Build(context.Background(), f, Request{Tag: tag, Stack: "java-springboot", Dir: dir, SHA256: sha})
	var be *BuildError
	if !errors.As(err, &be) || strings.Contains(strings.Join(be.Log, "\n"), "abc") {
		t.Fatalf("got %v", err)
	}
}

func TestBuildRejectsForeignTags(t *testing.T) {
	f := &dockercli.Fake{}
	for _, bad := range []string{"nginx:latest", "cagent/x", "cagent/X:1", "cagent/x:-flag", "registry.io/cagent/x:1"} {
		if _, err := Build(context.Background(), f, Request{Tag: bad, Stack: "java-springboot", Dir: t.TempDir()}); err == nil {
			t.Errorf("tag %q accepted", bad)
		}
	}
	if len(f.Calls) != 0 {
		t.Errorf("docker called: %v", f.Calls)
	}
}
