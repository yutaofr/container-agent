package initscripts

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func read(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestFromDirStagesInDeclaredOrderPerCapability(t *testing.T) {
	root, dest := t.TempDir(), t.TempDir()
	write(t, filepath.Join(root, "db/init/schema.sql"), "CREATE TABLE a (id INT);")
	write(t, filepath.Join(root, "db/init/seed.sql"), "INSERT INTO a VALUES (1);")
	write(t, filepath.Join(root, "cql/schema.cql"), "CREATE TABLE b (id int PRIMARY KEY);")
	got, err := FromDir(root, []Script{
		{"mysql", "db/init/seed.sql"}, {"cassandra", "cql/schema.cql"}, {"mysql", "db/init/schema.sql"},
	}, dest)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"mysql/01-seed.sql", "cassandra/01-schema.cql", "mysql/02-schema.sql"}
	if len(got) != len(want) {
		t.Fatalf("staged %v", got)
	}
	for i, s := range got {
		if s.Capability+"/"+s.File != want[i] || len(s.SHA256) != 64 {
			t.Errorf("staged[%d] = %+v, want %s", i, s, want[i])
		}
	}
	if read(t, filepath.Join(dest, "mysql/01-seed.sql")) != "INSERT INTO a VALUES (1);" {
		t.Error("seed.sql not copied")
	}
}

func TestFromDirClearsWhatAnEarlierBuildStaged(t *testing.T) {
	root, dest := t.TempDir(), t.TempDir()
	write(t, filepath.Join(dest, "mysql/01-old.sql"), "DROP TABLE a;")
	write(t, filepath.Join(root, "new.sql"), "SELECT 1;")
	if _, err := FromDir(root, []Script{{"mysql", "new.sql"}}, dest); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dest, "mysql/01-old.sql")); !os.IsNotExist(err) {
		t.Errorf("stale script kept: %v", err)
	}
}

func TestFromDirRefusesPathsLeavingTheProject(t *testing.T) {
	root, dest := t.TempDir(), t.TempDir()
	outside := t.TempDir()
	write(t, filepath.Join(outside, "secret.sql"), "x")
	if err := os.Symlink(filepath.Join(outside, "secret.sql"), filepath.Join(root, "link.sql")); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{"../secret.sql", "/etc/passwd", "link.sql"} {
		if _, err := FromDir(root, []Script{{"mysql", p}}, dest); err == nil {
			t.Errorf("%s staged", p)
		}
	}
}

func TestFromDirNamesAMissingOrOversizedScript(t *testing.T) {
	root, dest := t.TempDir(), t.TempDir()
	if _, err := FromDir(root, []Script{{"mysql", "db/missing.sql"}}, dest); err == nil ||
		!strings.Contains(err.Error(), "db/missing.sql") {
		t.Errorf("missing: %v", err)
	}
	write(t, filepath.Join(root, "big.sql"), strings.Repeat("x", MaxBytes+1))
	if _, err := FromDir(root, []Script{{"mysql", "big.sql"}}, dest); err == nil ||
		!strings.Contains(err.Error(), "big.sql") {
		t.Errorf("oversized: %v", err)
	}
	if err := os.Mkdir(filepath.Join(root, "dir.sql"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := FromDir(root, []Script{{"mysql", "dir.sql"}}, dest); err == nil {
		t.Error("directory staged")
	}
}

func TestCheckLimitsScriptsAndNames(t *testing.T) {
	var many []Script
	for range MaxScripts + 1 {
		many = append(many, Script{"mysql", "a.sql"})
	}
	for _, scripts := range [][]Script{many, {{"My SQL", "a.sql"}}, {{"mysql", ""}}} {
		if err := Check(scripts); err == nil {
			t.Errorf("accepted %v", scripts[0])
		}
	}
	if err := Check([]Script{{"mysql", "db/a.sql"}, {"cassandra", "b.cql"}}); err != nil {
		t.Error(err)
	}
}
