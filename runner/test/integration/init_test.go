//go:build integration

package integration

import (
	"archive/zip"
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/yutaofr/container-agent/runner/internal/builder"
	"github.com/yutaofr/container-agent/runner/internal/cluster"
	"github.com/yutaofr/container-agent/runner/internal/compose"
	"github.com/yutaofr/container-agent/runner/internal/initscripts"
	"github.com/yutaofr/container-agent/runner/internal/maven"
	"github.com/yutaofr/container-agent/runner/internal/maven/maventest"
	"github.com/yutaofr/container-agent/runner/internal/signals"
	"github.com/yutaofr/container-agent/runner/internal/verify"
	"github.com/yutaofr/container-agent/runner/internal/workdir"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// initCompose stands in for the generator's output: a middleware, its one-shot init service
// mounting what build staged, and an application that waits for init to finish.
const initCompose = `services:
  db:
    image: busybox:1.37
    command: ["sh", "-c", "touch /tmp/up && sleep 3600"]
    healthcheck: {test: ["CMD", "test", "-f", "/tmp/up"], interval: 1s, retries: 30}
  db-init:
    image: busybox:1.37
    entrypoint: ["sh", "-c", "for f in /cagent-init/*/*; do echo \"run $$f: $$(cat $$f)\"; done"]
    depends_on: {db: {condition: service_healthy}}
    volumes:
      - ./init/api/mysql:/cagent-init/api:ro
      - ./init/orders/mysql:/cagent-init/orders:ro
    restart: "no"
    labels: {cagent.role: init}
  app:
    image: busybox:1.37
    command: ["sleep", "3600"]
    depends_on:
      db: {condition: service_healthy}
      db-init: {condition: service_completed_successfully}
`

func jarWith(t *testing.T, entries map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range entries {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestInitScriptsFromAProjectAndAJarRunBeforeTheApplication(t *testing.T) {
	ctx := context.Background()
	w, err := workspace.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	name := fmt.Sprintf("it-%d", time.Now().UnixNano()%1_000_000_000)

	// A workdir project names its scripts in its own descriptor.
	proj := t.TempDir()
	for rel, body := range map[string]string{
		"db/schema.sql": "workdir-script",
		".cagent/services/api.yaml": "name: api\nstack: dockerfile\nsources:\n  workdir: {dockerfile: Dockerfile}\n" +
			"requires:\n  - capability: mysql\n    init: {scripts: [db/schema.sql]}\n",
	} {
		path := filepath.Join(proj, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	desc, err := workdir.LoadDescriptor(proj, "api")
	if err != nil {
		t.Fatal(err)
	}
	apiInit, err := w.InitDir(name, "api")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := initscripts.FromDir(proj, desc.Scripts(), apiInit); err != nil {
		t.Fatal(err)
	}

	// A release jar carries its scripts as classpath resources.
	repo := maventest.New(t, "dev", "token")
	repo.PutRelease("com.acme", "orders", "1.0.0", jarWith(t, map[string]string{"BOOT-INF/classes/db/release.sql": "release-script"}))
	mrepo, err := maven.LoadRepo(repo.SettingsXML(t, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	client := maven.Client{Repo: mrepo}
	art, err := client.Resolve(ctx, maven.Coord{Group: "com.acme", Artifact: "orders", Version: "1.0.0", Ext: "jar"})
	if err != nil {
		t.Fatal(err)
	}
	buildDir, err := w.BuildDir(name, "orders")
	if err != nil {
		t.Fatal(err)
	}
	jar, _, err := client.Download(ctx, art, buildDir, builder.ArtifactFile)
	if err != nil {
		t.Fatal(err)
	}
	ordersInit, err := w.InitDir(name, "orders")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := initscripts.FromJar(jar, []initscripts.Script{{Capability: "mysql", Path: "db/release.sql"}}, ordersInit); err != nil {
		t.Fatal(err)
	}

	// The compose file lives in the cluster's workspace, as serve writes it: the init
	// mounts stay inside the workspace and need no approval.
	path, err := w.WriteFile(name, "candidate.yaml", []byte(initCompose))
	if err != nil {
		t.Fatal(err)
	}
	p, err := compose.Load(ctx, path, cluster.Project(name))
	if err != nil {
		t.Fatal(err)
	}
	if findings := compose.Lint(p, []string{filepath.Dir(filepath.Dir(filepath.Dir(path)))}, nil); len(findings) != 0 {
		t.Fatalf("lint: %+v", findings)
	}
	t.Cleanup(func() {
		if err := cluster.Down(context.Background(), d, name); err != nil {
			t.Errorf("cleanup: %v", err)
		}
	})
	// As runUp does: start from a non-interpolated load, so $$ in the init loop survives.
	verbatim, err := compose.LoadVerbatim(ctx, path, cluster.Project(name))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := cluster.Up(ctx, d, w, name, verbatim); err != nil {
		t.Fatal(err)
	}

	snap := waitFor(t, name, 60*time.Second, func(s map[string]signals.Service) bool {
		return s["app"].State == "running" && s["db-init"].Completed()
	})
	logs, err := exec.Command("docker", "logs", cluster.Project(name)+"-db-init-1").CombinedOutput()
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"run /cagent-init/api/01-schema.sql: workdir-script", "run /cagent-init/orders/01-release.sql: release-script"} {
		if !strings.Contains(string(logs), want) {
			t.Errorf("init logs lack %q:\n%s", want, logs)
		}
	}
	rep := verify.Verify(ctx, d, snap, nil)
	if !rep.Passed {
		t.Errorf("verify: %+v", rep)
	}
	finished := inspectTime(t, cluster.Project(name)+"-db-init-1", "{{.State.FinishedAt}}")
	started := inspectTime(t, cluster.Project(name)+"-app-1", "{{.State.StartedAt}}")
	if !finished.Before(started) {
		t.Errorf("app started at %s, before init finished at %s", started, finished)
	}
}

func inspectTime(t *testing.T, container, format string) time.Time {
	t.Helper()
	out, err := exec.Command("docker", "inspect", "--format", format, container).Output()
	if err != nil {
		t.Fatal(err)
	}
	ts, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(string(out)))
	if err != nil {
		t.Fatal(err)
	}
	return ts
}
