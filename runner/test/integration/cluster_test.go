//go:build integration

// Package integration runs the Runner's lifecycle, signal, and verify code against a real
// Docker daemon. Run with: go test -tags integration ./test/integration/
package integration

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/yutaofr/container-agent/runner/internal/cluster"
	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/signals"
	"github.com/yutaofr/container-agent/runner/internal/verify"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

const webService = `
  web:
    image: busybox:1.37
    command: ["sh", "-c", "mkdir -p /www && echo ok > /www/index.html && httpd -f -p 8080 -h /www"]
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]
      interval: 1s
      timeout: 2s
      retries: 30
`

const brokenServices = `
  crash:
    image: busybox:1.37
    command: ["sh", "-c", "echo 'java.net.ConnectException: Connection refused password=hunter2'; exit 3"]
    restart: "no"
  oom:
    image: busybox:1.37
    command: ["sh", "-c", "tail /dev/zero"]
    mem_limit: 32m
    restart: "no"
`

var d = dockercli.Exec{}

// up starts a fresh cluster from the given services and removes it when the test ends.
func up(t *testing.T, services string) string {
	t.Helper()
	ctx := context.Background()
	name := fmt.Sprintf("it-%d", time.Now().UnixNano()%1_000_000_000)
	src := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(src, []byte("services:"+services), 0o644); err != nil {
		t.Fatal(err)
	}
	p, err := compose.Load(ctx, src, cluster.Project(name))
	if err != nil {
		t.Fatal(err)
	}
	w, err := workspace.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := cluster.Down(context.Background(), d, name); err != nil {
			t.Errorf("cleanup: %v", err)
		}
	})
	if _, err := cluster.Up(ctx, d, w, name, p); err != nil {
		t.Fatal(err)
	}
	return name
}

// waitFor polls snapshots until done accepts one, or fails after timeout.
func waitFor(t *testing.T, name string, timeout time.Duration, done func(map[string]signals.Service) bool) signals.Snapshot {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for {
		snap, err := signals.Collect(context.Background(), d, cluster.Project(name), signals.DefaultOptions)
		if err != nil {
			t.Fatal(err)
		}
		byName := map[string]signals.Service{}
		for _, s := range snap.Services {
			byName[s.Service] = s
		}
		if done(byName) {
			return snap
		}
		if time.Now().After(deadline) {
			t.Fatalf("timed out; last snapshot: %+v", snap)
		}
		time.Sleep(time.Second)
	}
}

func TestBrokenServicesAreDiagnosed(t *testing.T) {
	name := up(t, webService+brokenServices)
	snap := waitFor(t, name, 90*time.Second, func(s map[string]signals.Service) bool {
		return s["crash"].State == "exited" && s["oom"].State == "exited" && s["web"].Health == "healthy"
	})
	by := map[string]signals.Service{}
	for _, s := range snap.Services {
		by[s.Service] = s
	}

	crash := by["crash"]
	if crash.ExitCode != 3 || !slices.Contains(crash.Signatures, "conn:refused@crash") ||
		!slices.Contains(crash.Signatures, "exit:3@crash") {
		t.Errorf("crash = %+v", crash)
	}
	evidence := strings.Join(append(crash.ErrorLines, crash.Tail...), "\n")
	if strings.Contains(evidence, "hunter2") || !strings.Contains(evidence, "password=******") {
		t.Errorf("crash evidence not redacted: %q", evidence)
	}

	oom := by["oom"]
	if !oom.OOMKilled || !slices.Contains(oom.Signatures, "oom:killed@oom") {
		t.Errorf("oom = %+v", oom)
	}

	rep := verify.Verify(context.Background(), d, snap, nil)
	if rep.Passed || rep.Reached != "" {
		t.Errorf("verify passed a broken cluster: %+v", rep)
	}
}

func TestHealthyClusterReachesL3(t *testing.T) {
	name := up(t, webService)
	snap := waitFor(t, name, 60*time.Second, func(s map[string]signals.Service) bool {
		return s["web"].Health == "healthy"
	})
	probes := []verify.Probe{
		{Service: "web", HTTP: &verify.HTTPProbe{Port: 8080, Path: "/"}},
		{Service: "web", HTTP: &verify.HTTPProbe{Port: 8080, Path: "/missing", Expect: 404}},
		{Service: "web", TCP: &verify.TCPProbe{Port: 8080}},
	}
	rep := verify.Verify(context.Background(), d, snap, probes)
	if !rep.Passed || rep.Reached != "L3" {
		t.Fatalf("verify: %+v", rep)
	}

	wrong := []verify.Probe{{Service: "web", TCP: &verify.TCPProbe{Port: 9999}}}
	if rep := verify.Verify(context.Background(), d, snap, wrong); rep.Passed {
		t.Errorf("closed port passed: %+v", rep)
	}
}

func TestDownRefusesForeignProject(t *testing.T) {
	name := fmt.Sprintf("it-foreign-%d", time.Now().UnixNano()%1_000_000_000)
	project := cluster.Project(name)
	// A container that claims the project name but lacks the managed label.
	id, err := exec.Command("docker", "run", "-d", "--label", dockercli.ComposeProjectLabel+"="+project,
		"busybox:1.37", "sleep", "60").Output()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = exec.Command("docker", "rm", "-f", strings.TrimSpace(string(id))).Run() })

	if err := cluster.Down(context.Background(), d, name); !errors.Is(err, cluster.ErrNotManaged) {
		t.Fatalf("got %v, want ErrNotManaged", err)
	}
	if out, _ := exec.Command("docker", "inspect", "-f", "{{.State.Running}}", strings.TrimSpace(string(id))).Output(); strings.TrimSpace(string(out)) != "true" {
		t.Error("foreign container was stopped")
	}
}

func TestDownRemovesEverything(t *testing.T) {
	name := up(t, webService)
	waitFor(t, name, 60*time.Second, func(s map[string]signals.Service) bool { return s["web"].State == "running" })
	if err := cluster.Down(context.Background(), d, name); err != nil {
		t.Fatal(err)
	}
	cs, err := dockercli.ProjectContainers(context.Background(), d, cluster.Project(name))
	if err != nil || len(cs) != 0 {
		t.Errorf("containers left after down: %v, %v", cs, err)
	}
}
