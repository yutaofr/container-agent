package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/maven/maventest"
)

const (
	webTag     = "cagent/web:release-1.0.0"
	inspectWeb = `image inspect --format {{index .Config.Labels "cagent.artifact-sha256"}} ` + webTag
)

// buildDeps returns deps whose docker treats the image as missing and accepts any
// `docker build` of webTag into the web build dir.
func buildDeps(t *testing.T) (deps, *dockercli.Fake, string) {
	t.Helper()
	d, f, root := withDocker(t, nil)
	f.Responses = map[string]dockercli.FakeResponse{inspectWeb: {Err: errors.New("No such image")}}
	return d, f, filepath.Join(root, "clusters", "demo", "build", "web")
}

func expectBuild(f *dockercli.Fake, dir, sha string) {
	f.Responses["build -t "+webTag+" --label cagent.managed=true --label cagent.artifact-sha256="+sha+
		" -f "+filepath.Join(dir, "Dockerfile")+" "+dir] = dockercli.FakeResponse{}
}

func runBuildCmd(t *testing.T, d deps, args ...string) (int, BuildResult, string) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := run(append([]string{"build", "-name", "demo", "-service", "web", "-tag", webTag}, args...), &out, &errOut, d)
	var res BuildResult
	if out.Len() > 0 {
		if err := json.Unmarshal(out.Bytes(), &res); err != nil {
			t.Fatalf("bad JSON %q: %v", out.String(), err)
		}
	}
	return code, res, errOut.String()
}

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func TestBuildFromMavenRelease(t *testing.T) {
	repo := maventest.New(t, "dev", "token")
	repo.PutRelease("com.acme", "web", "1.0.0", []byte("release-jar"))
	settings := repo.SettingsXML(t, t.TempDir())
	d, f, dir := buildDeps(t)
	expectBuild(f, dir, sha256Hex("release-jar"))

	code, res, stderr := runBuildCmd(t, d, "-maven", "com.acme:web:1.0.0", "-settings", settings)
	if code != exitOK {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
	if res.Version != "1.0.0" || res.Source != "maven:com.acme:web:1.0.0:jar" || res.Cached {
		t.Errorf("result %+v", res)
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "app.jar")); string(got) != "release-jar" {
		t.Errorf("artifact = %q", got)
	}
}

func TestBuildFromMavenSnapshotReportsResolvedVersion(t *testing.T) {
	repo := maventest.New(t, "dev", "token")
	want := repo.PutSnapshot("com.acme", "web", "2.0-SNAPSHOT", "20261017.083000", "3", []byte("snap"))
	d, _, _ := buildDeps(t)
	_, res, _ := runBuildCmd(t, d, "-maven", "com.acme:web:2.0-SNAPSHOT", "-settings", repo.SettingsXML(t, t.TempDir()))
	if res.Version != want {
		t.Errorf("version %q, want %q", res.Version, want)
	}
}

func TestBuildFromWorkdir(t *testing.T) {
	proj := t.TempDir()
	if err := os.MkdirAll(filepath.Join(proj, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	desc := "name: web\nstack: java-springboot\nsources:\n  workdir:\n" +
		"    build: 'mkdir -p target && printf release-jar > target/web.jar'\n    output: 'target/*.jar'\n"
	if err := os.WriteFile(filepath.Join(proj, ".cagent", "service.yaml"), []byte(desc), 0o644); err != nil {
		t.Fatal(err)
	}
	d, f, dir := buildDeps(t)
	expectBuild(f, dir, sha256Hex("release-jar"))
	code, res, stderr := runBuildCmd(t, d, "-workdir", proj)
	if code != exitOK || res.Source != "workdir:"+proj {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
}

func TestBuildFromWorkdirFailureReportsLog(t *testing.T) {
	proj := t.TempDir()
	if err := os.MkdirAll(filepath.Join(proj, ".cagent"), 0o755); err != nil {
		t.Fatal(err)
	}
	desc := "name: web\nsources:\n  workdir:\n    build: 'echo token=abc; echo COMPILATION ERROR; exit 1'\n    output: 'target/*.jar'\n"
	if err := os.WriteFile(filepath.Join(proj, ".cagent", "service.yaml"), []byte(desc), 0o644); err != nil {
		t.Fatal(err)
	}
	d, f, _ := buildDeps(t)
	code, res, _ := runBuildCmd(t, d, "-workdir", proj)
	if code != exitFindings || len(res.Log) != 2 || res.Log[0] != "token=******" || res.Log[1] != "COMPILATION ERROR" {
		t.Fatalf("exit %d, res %+v", code, res)
	}
	if len(f.Calls) != 0 {
		t.Errorf("docker called after a failed build: %v", f.Calls)
	}
}

// project writes files (relative path -> content) into a fresh working directory.
func project(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for rel, body := range files {
		path := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

func TestBuildFromWorkdirWithTheProjectsDockerfile(t *testing.T) {
	proj := project(t, map[string]string{
		"api/Dockerfile": "FROM busybox\n",
		".cagent/services/web.yaml": "name: web\nstack: dockerfile\nsources:\n  workdir:\n" +
			"    build: 'touch api/built'\n    dockerfile: api/Dockerfile\n    context: api\n",
	})
	d, f, _ := buildDeps(t)
	cmd := "build -t " + webTag + " --label cagent.managed=true -f " + filepath.Join(proj, "api", "Dockerfile") + " " + filepath.Join(proj, "api")
	f.Responses[cmd] = dockercli.FakeResponse{}
	code, res, stderr := runBuildCmd(t, d, "-workdir", proj, "-stack", "dockerfile")
	if code != exitOK || res.Source != "workdir:"+proj || res.SHA256 != "" || res.Cached {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
	if _, err := os.Stat(filepath.Join(proj, "api", "built")); err != nil {
		t.Error("the descriptor's build command did not run first")
	}
}

func TestBuildRefusesAStackOtherThanTheProjects(t *testing.T) {
	proj := project(t, map[string]string{
		"Dockerfile":           "FROM busybox\n",
		".cagent/service.yaml": "name: web\nstack: java-springboot\nsources:\n  workdir: { build: 'true', output: 'x.jar' }\n",
	})
	d, f, _ := buildDeps(t)
	code, _, stderr := runBuildCmd(t, d, "-workdir", proj, "-stack", "dockerfile")
	if code != exitError || !strings.Contains(stderr, `stack "dockerfile" differs from the project's descriptor ("java-springboot")`) {
		t.Fatalf("exit %d, stderr %s", code, stderr)
	}
	if len(f.Calls) != 0 {
		t.Errorf("docker called: %v", f.Calls)
	}
}

func TestBuildArgumentErrors(t *testing.T) {
	for _, args := range [][]string{
		{"build", "-name", "demo", "-service", "web", "-tag", webTag},
		{"build", "-name", "demo", "-service", "web", "-tag", webTag, "-maven", "a:b:1", "-workdir", "."},
		{"build", "-name", "demo", "-service", "web", "-tag", "nginx:latest", "-maven", "a:b:1"},
		{"build", "-name", "demo", "-service", "../x", "-tag", webTag, "-maven", "a:b:1"},
	} {
		var out, errOut bytes.Buffer
		if code := run(args, &out, &errOut, testDeps(t)); code != exitError {
			t.Errorf("%v: exit %d, want %d", args, code, exitError)
		}
	}
}

// zipBytes returns a jar holding entries (name -> body).
func zipBytes(t *testing.T, entries map[string]string) []byte {
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

func TestBuildFromMavenStagesInitScriptsFromTheJar(t *testing.T) {
	jar := zipBytes(t, map[string]string{"BOOT-INF/classes/db/schema.sql": "CREATE TABLE t (id INT);"})
	repo := maventest.New(t, "dev", "token")
	repo.PutRelease("com.acme", "web", "1.0.0", jar)
	d, f, dir := buildDeps(t)
	expectBuild(f, dir, sha256Hex(string(jar)))

	code, res, stderr := runBuildCmd(t, d, "-maven", "com.acme:web:1.0.0",
		"-settings", repo.SettingsXML(t, t.TempDir()), "-init", "mysql=db/schema.sql")
	if code != exitOK {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
	if len(res.Init) != 1 || res.Init[0].File != "01-schema.sql" || res.Init[0].SHA256 != sha256Hex("CREATE TABLE t (id INT);") {
		t.Errorf("init %+v", res.Init)
	}
	staged := filepath.Join(filepath.Dir(filepath.Dir(dir)), "init", "web", "mysql", "01-schema.sql")
	if got, _ := os.ReadFile(staged); string(got) != "CREATE TABLE t (id INT);" {
		t.Errorf("staged %q", got)
	}
}

func TestBuildFailsOnAScriptTheJarLacks(t *testing.T) {
	repo := maventest.New(t, "dev", "token")
	repo.PutRelease("com.acme", "web", "1.0.0", zipBytes(t, map[string]string{"x.txt": "x"}))
	d, f, _ := buildDeps(t)
	code, res, _ := runBuildCmd(t, d, "-maven", "com.acme:web:1.0.0",
		"-settings", repo.SettingsXML(t, t.TempDir()), "-init", "mysql=db/schema.sql")
	if code != exitFindings || !strings.Contains(res.Error, "db/schema.sql") {
		t.Fatalf("exit %d, res %+v", code, res)
	}
	for _, c := range f.Calls {
		if strings.HasPrefix(c, "build ") {
			t.Errorf("image built despite a missing script: %s", c)
		}
	}
}

func TestBuildFromWorkdirStagesTheProjectsOwnScripts(t *testing.T) {
	proj := project(t, map[string]string{
		"Dockerfile":    "FROM busybox\n",
		"db/schema.sql": "CREATE TABLE t (id INT);",
		".cagent/services/web.yaml": "name: web\nstack: dockerfile\nsources:\n  workdir: {dockerfile: Dockerfile}\n" +
			"requires:\n  - capability: mysql\n    init: {scripts: [db/schema.sql]}\n",
	})
	d, f, dir := buildDeps(t)
	f.Responses["build -t "+webTag+" --label cagent.managed=true -f "+filepath.Join(proj, "Dockerfile")+" "+proj] = dockercli.FakeResponse{}
	code, res, stderr := runBuildCmd(t, d, "-workdir", proj)
	if code != exitOK || len(res.Init) != 1 || res.Init[0].Capability != "mysql" {
		t.Fatalf("exit %d, stderr %s, res %+v", code, stderr, res)
	}
	staged := filepath.Join(filepath.Dir(filepath.Dir(dir)), "init", "web", "mysql", "01-schema.sql")
	if _, err := os.Stat(staged); err != nil {
		t.Error(err)
	}
	// The project's descriptor decides what runs; the caller cannot add scripts.
	code, _, stderr = runBuildCmd(t, d, "-workdir", proj, "-init", "mysql=db/schema.sql")
	if code != exitError || !strings.Contains(stderr, "-init") {
		t.Errorf("exit %d, stderr %s", code, stderr)
	}
}

func TestBuildToolPassesInitScriptsForMavenOnly(t *testing.T) {
	argv, err := buildTool(serveConfig{}, deps{}, json.RawMessage(`{"name":"demo","service":"web",`+
		`"tag":"cagent/web:x","maven":"a:b:1","init":[{"capability":"mysql","path":"db/schema.sql"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(argv, " "); !strings.HasSuffix(got, "-init mysql=db/schema.sql") {
		t.Errorf("argv %s", got)
	}
	root := t.TempDir()
	_, err = buildTool(serveConfig{workdirRoots: []string{root}}, deps{}, json.RawMessage(`{"name":"demo",`+
		`"service":"web","tag":"cagent/web:x","workdir":"`+root+`","init":[{"capability":"mysql","path":"a.sql"}]}`))
	if err == nil {
		t.Error("init accepted with workdir")
	}
}
