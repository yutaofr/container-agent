//go:build integration

package integration

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/yutaofr/container-agent/runner/internal/builder"
	"github.com/yutaofr/container-agent/runner/internal/maven"
	"github.com/yutaofr/container-agent/runner/internal/maven/maventest"
	"github.com/yutaofr/container-agent/runner/internal/signals"
	"github.com/yutaofr/container-agent/runner/internal/verify"
	"github.com/yutaofr/container-agent/runner/internal/workdir"
	"github.com/yutaofr/container-agent/runner/internal/workspace"
)

// mainJava is a minimal stand-in for a Spring Boot service: it answers /actuator/health.
const mainJava = `import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;

public class Main {
    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress(8080), 0);
        server.createContext("/actuator/health", exchange -> {
            byte[] body = "{\"status\":\"UP\"}".getBytes();
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        System.out.println("demo started on 8080");
    }
}
`

// compileInJDK is the "host toolchain" for these tests: the host has no Java, so javac runs
// in a JDK container. From the Runner's point of view it is just the developer's command.
// It runs as the invoking user: on Linux a root container would leave root-owned files in
// the bind-mounted temp dir that the test cannot delete (Docker Desktop hides this on macOS).
const compileInJDK = `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/src -w /src eclipse-temurin:17-jdk sh -c ` +
	`'mkdir -p target/classes && javac -d target/classes Main.java && jar --create --file target/demo.jar --main-class Main -C target/classes .'`

// demoJar compiles mainJava into a runnable jar and returns its bytes.
func demoJar(t *testing.T) []byte {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "Main.java"), []byte(mainJava), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("sh", "-c", compileInJDK)
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("compile demo: %v\n%s", err, out)
	}
	jar, err := os.ReadFile(filepath.Join(dir, "target", "demo.jar"))
	if err != nil {
		t.Fatal(err)
	}
	return jar
}

// uniqueTag returns a fresh cagent/ tag and removes the image when the test ends.
func uniqueTag(t *testing.T, ref string) string {
	t.Helper()
	tag := fmt.Sprintf("cagent/it-demo-%d:%s", time.Now().UnixNano()%1_000_000_000, ref)
	t.Cleanup(func() { _ = exec.Command("docker", "rmi", "-f", tag).Run() })
	return tag
}

func imageLabel(t *testing.T, tag string) string {
	t.Helper()
	out, err := exec.Command("docker", "image", "inspect", "--format",
		`{{index .Config.Labels "`+builder.LabelArtifactSHA+`"}}`, tag).Output()
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(out))
}

func TestReleaseBuildsCachesAndRuns(t *testing.T) {
	ctx := context.Background()
	repo := maventest.New(t, "dev", "token")
	repo.PutRelease("com.acme", "demo", "1.0.0", demoJar(t))
	mrepo, err := maven.LoadRepo(repo.SettingsXML(t, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	client := maven.Client{Repo: mrepo}
	w, err := workspace.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	dir, err := w.BuildDir("it", "demo")
	if err != nil {
		t.Fatal(err)
	}
	tag := uniqueTag(t, "release-1.0.0")

	art, err := client.Resolve(ctx, maven.Coord{Group: "com.acme", Artifact: "demo", Version: "1.0.0", Ext: "jar"})
	if err != nil {
		t.Fatal(err)
	}
	_, sum, err := client.Download(ctx, art, dir, builder.ArtifactFile)
	if err != nil {
		t.Fatal(err)
	}
	req := builder.Request{Tag: tag, Stack: "java-springboot", JDK: "17", Dir: dir, SHA256: sum}
	if cached, err := builder.Build(ctx, d, req); err != nil || cached {
		t.Fatalf("first build: cached=%v err=%v", cached, err)
	}
	if got := imageLabel(t, tag); got != sum {
		t.Errorf("image label %q, want %q", got, sum)
	}
	if cached, err := builder.Build(ctx, d, req); err != nil || !cached {
		t.Fatalf("second build: cached=%v err=%v, want cache hit", cached, err)
	}

	// Run it with the healthcheck the generator emits; curl must exist in the image.
	name := up(t, fmt.Sprintf(`
  demo:
    image: %s
    healthcheck:
      test: ["CMD-SHELL", "test \"$$(curl -s -o /dev/null -w '%%{http_code}' http://localhost:8080/actuator/health)\" = 200"]
      interval: 1s
      timeout: 3s
      retries: 60
`, tag))
	snap := waitFor(t, name, 90*time.Second, func(s map[string]signals.Service) bool {
		return s["demo"].Health == "healthy"
	})
	rep := verify.Verify(ctx, d, snap, []verify.Probe{
		{Service: "demo", HTTP: &verify.HTTPProbe{Port: 8080, Path: "/actuator/health"}},
	})
	if !rep.Passed || rep.Reached != "L3" {
		t.Fatalf("verify: %+v", rep)
	}
}

func TestSnapshotResolvesToTimestampedBuild(t *testing.T) {
	ctx := context.Background()
	repo := maventest.New(t, "dev", "token")
	want := repo.PutSnapshot("com.acme", "demo", "2.0-SNAPSHOT", "20261017.083000", "3", demoJar(t))
	mrepo, err := maven.LoadRepo(repo.SettingsXML(t, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	client := maven.Client{Repo: mrepo}
	art, err := client.Resolve(ctx, maven.Coord{Group: "com.acme", Artifact: "demo", Version: "2.0-SNAPSHOT", Ext: "jar"})
	if err != nil {
		t.Fatal(err)
	}
	if art.Resolved != want {
		t.Fatalf("resolved %q, want %q", art.Resolved, want)
	}
	dir := t.TempDir()
	_, sum, err := client.Download(ctx, art, dir, builder.ArtifactFile)
	if err != nil {
		t.Fatal(err)
	}
	tag := uniqueTag(t, "snapshot-2.0-SNAPSHOT")
	if _, err := builder.Build(ctx, d, builder.Request{Tag: tag, Stack: "java-springboot", JDK: "17", Dir: dir, SHA256: sum}); err != nil {
		t.Fatal(err)
	}
	if got := imageLabel(t, tag); got != sum {
		t.Errorf("image label %q, want %q", got, sum)
	}
}

func TestWorkdirBuildsWithTheDevelopersCommand(t *testing.T) {
	ctx := context.Background()
	proj := t.TempDir()
	if err := os.WriteFile(filepath.Join(proj, "Main.java"), []byte(mainJava), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(proj, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	desc := fmt.Sprintf("name: demo\nkind: app\nstack: java-springboot\nsources:\n  workdir:\n    build: %q\n    output: target/*.jar\n", compileInJDK)
	if err := os.WriteFile(filepath.Join(proj, workdir.DescriptorPath), []byte(desc), 0o644); err != nil {
		t.Fatal(err)
	}
	dl, err := workdir.LoadDescriptor(proj)
	if err != nil {
		t.Fatal(err)
	}
	artifact, err := workdir.Build(ctx, proj, dl)
	if err != nil {
		t.Fatal(err)
	}
	src, err := os.Open(artifact)
	if err != nil {
		t.Fatal(err)
	}
	defer src.Close()
	dir := t.TempDir()
	_, sum, err := workspace.WriteNoFollow(dir, builder.ArtifactFile, src)
	if err != nil {
		t.Fatal(err)
	}
	tag := uniqueTag(t, "workdir-test")
	if cached, err := builder.Build(ctx, d, builder.Request{Tag: tag, Stack: dl.Stack, JDK: "17", Dir: dir, SHA256: sum}); err != nil || cached {
		t.Fatalf("cached=%v err=%v", cached, err)
	}
}
