package maven

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/maven/maventest"
)

func clientFor(t *testing.T, r *maventest.Repo) Client {
	t.Helper()
	repo, err := LoadRepo(r.SettingsXML(t, t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	return Client{Repo: repo}
}

func TestDownloadRelease(t *testing.T) {
	r := maventest.New(t, "dev", "token")
	r.PutRelease("com.acme", "demo", "1.0.0", []byte("release-jar"))
	c := clientFor(t, r)
	ctx := context.Background()

	a, err := c.Resolve(ctx, Coord{"com.acme", "demo", "1.0.0", "jar"})
	if err != nil {
		t.Fatal(err)
	}
	if a.Resolved != "1.0.0" || !strings.HasSuffix(a.URL, "/com/acme/demo/1.0.0/demo-1.0.0.jar") {
		t.Errorf("resolved %+v", a)
	}
	dir := t.TempDir()
	path, sum, err := c.Download(ctx, a, dir, "app.jar")
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(path); string(got) != "release-jar" || len(sum) != 64 {
		t.Errorf("content %q, sha256 %q", got, sum)
	}
}

func TestResolveSnapshotToTimestampedBuild(t *testing.T) {
	r := maventest.New(t, "dev", "token")
	want := r.PutSnapshot("com.acme", "demo", "2.0-SNAPSHOT", "20261017.083000", "3", []byte("snap"))
	a, err := clientFor(t, r).Resolve(context.Background(), Coord{"com.acme", "demo", "2.0-SNAPSHOT", "jar"})
	if err != nil {
		t.Fatal(err)
	}
	if a.Resolved != want || want != "2.0-20261017.083000-3" {
		t.Errorf("resolved %q, want %q", a.Resolved, want)
	}
	if !strings.HasSuffix(a.URL, "/2.0-SNAPSHOT/demo-2.0-20261017.083000-3.jar") {
		t.Errorf("url %s", a.URL)
	}
}

func TestDownloadRejectsChecksumMismatch(t *testing.T) {
	r := maventest.New(t, "dev", "token")
	r.PutRelease("com.acme", "demo", "1.0.0", []byte("original"))
	r.Put("com/acme/demo/1.0.0/demo-1.0.0.jar", []byte("tampered"), false) // keeps the old .sha1
	c := clientFor(t, r)
	a, _ := c.Resolve(context.Background(), Coord{"com.acme", "demo", "1.0.0", "jar"})
	dir := t.TempDir()
	if _, _, err := c.Download(context.Background(), a, dir, "app.jar"); err == nil ||
		!strings.Contains(err.Error(), "does not match published") {
		t.Fatalf("got %v, want checksum mismatch", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "app.jar")); !os.IsNotExist(err) {
		t.Error("mismatched download left on disk")
	}
}

func TestDownloadWithoutPublishedChecksum(t *testing.T) {
	r := maventest.New(t, "dev", "token")
	r.Put("com/acme/demo/1.0.0/demo-1.0.0.jar", []byte("no-sha"), false)
	c := clientFor(t, r)
	a, _ := c.Resolve(context.Background(), Coord{"com.acme", "demo", "1.0.0", "jar"})
	if _, _, err := c.Download(context.Background(), a, t.TempDir(), "app.jar"); err != nil {
		t.Fatalf("download without .sha1 failed: %v", err)
	}
}

func TestErrorsNameTheProblem(t *testing.T) {
	r := maventest.New(t, "dev", "token")
	ctx := context.Background()

	bad := clientFor(t, r)
	bad.Repo.Password = "wrong"
	a, _ := bad.Resolve(ctx, Coord{"com.acme", "demo", "1.0.0", "jar"})
	if _, _, err := bad.Download(ctx, a, t.TempDir(), "app.jar"); err == nil ||
		!strings.Contains(err.Error(), `check the <server> credentials for mirror "company-nexus"`) {
		t.Errorf("wrong password: got %v", err)
	}

	c := clientFor(t, r)
	if _, err := c.Resolve(ctx, Coord{"com.acme", "demo", "9.9-SNAPSHOT", "jar"}); err == nil ||
		!strings.Contains(err.Error(), "not found") {
		t.Errorf("missing snapshot: got %v", err)
	}
}
