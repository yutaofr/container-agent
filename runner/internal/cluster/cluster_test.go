package cluster

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

const psDemo = "ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-demo"

func TestUpWritesLabelledComposeAndStarts(t *testing.T) {
	src := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(src, []byte("services:\n  web: { image: nginx:1.27 }\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	p, err := compose.Load(ctx, src, Project("demo"))
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	w, err := workspace.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(root, "clusters", "demo", "compose.yaml")
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		"compose -p cagent-demo -f " + want + " up -d --remove-orphans": {},
	}}

	path, err := Up(ctx, f, w, "demo", p)
	if err != nil {
		t.Fatal(err)
	}
	if path != want {
		t.Errorf("compose written to %s, want %s", path, want)
	}
	data, _ := os.ReadFile(path)
	for _, label := range []string{`cagent.managed: "true"`, "cagent.cluster: demo"} {
		if !strings.Contains(string(data), label) {
			t.Errorf("written compose lacks %s:\n%s", label, data)
		}
	}
}

func TestDownRefusesUnmanagedContainers(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		psDemo:        {Out: "aaa\n"},
		"inspect aaa": {Out: `[{"Id":"aaa","Name":"/theirs","Config":{"Labels":{}}}]`},
	}}
	err := Down(context.Background(), f, "demo")
	if !errors.Is(err, ErrNotManaged) {
		t.Fatalf("got %v, want ErrNotManaged", err)
	}
	for _, c := range f.Calls {
		if strings.Contains(c, " down ") {
			t.Errorf("down ran on an unmanaged project: %v", f.Calls)
		}
	}
}

func TestDownRemovesManagedProject(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		psDemo:        {Out: "aaa\n"},
		"inspect aaa": {Out: `[{"Id":"aaa","Name":"/x","Config":{"Labels":{"cagent.managed":"true"}}}]`},
		"compose -p cagent-demo down -v --remove-orphans": {},
	}}
	if err := Down(context.Background(), f, "demo"); err != nil {
		t.Fatal(err)
	}
}

func TestRejectsFlagLikeNames(t *testing.T) {
	f := &dockercli.Fake{}
	if err := Down(context.Background(), f, "-rf"); err == nil {
		t.Error("Down accepted -rf")
	}
	if len(f.Calls) != 0 {
		t.Errorf("docker was called: %v", f.Calls)
	}
}
