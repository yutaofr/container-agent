package initscripts

import (
	"archive/zip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// jar writes a zip with the given entries; a name ending in "@" becomes a symlink entry.
func jar(t *testing.T, entries map[string]string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "app.jar")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	for name, body := range entries {
		h := &zip.FileHeader{Name: strings.TrimSuffix(name, "@"), Method: zip.Deflate}
		if strings.HasSuffix(name, "@") {
			h.SetMode(os.ModeSymlink | 0o777)
		}
		w, err := zw.CreateHeader(h)
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
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestFromJarPrefersBootInfClasses(t *testing.T) {
	j := jar(t, map[string]string{
		"BOOT-INF/classes/db/schema.sql": "CREATE TABLE boot (id INT);",
		"db/schema.sql":                  "CREATE TABLE plain (id INT);",
		"schema.cql":                     "CREATE TABLE c (id int PRIMARY KEY);",
	})
	dest := t.TempDir()
	got, err := FromJar(j, []Script{{"mysql", "db/schema.sql"}, {"cassandra", "schema.cql"}}, dest)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].File != "01-schema.sql" || got[1].File != "01-schema.cql" {
		t.Fatalf("staged %v", got)
	}
	if read(t, filepath.Join(dest, "mysql/01-schema.sql")) != "CREATE TABLE boot (id INT);" {
		t.Error("did not take BOOT-INF/classes first")
	}
	if read(t, filepath.Join(dest, "cassandra/01-schema.cql")) != "CREATE TABLE c (id int PRIMARY KEY);" {
		t.Error("root entry not staged")
	}
}

func TestFromJarRefusesWhatIsNotAPlainEntry(t *testing.T) {
	j := jar(t, map[string]string{
		"BOOT-INF/classes/link.sql@": "/etc/passwd",
		"BOOT-INF/classes/big.sql":   strings.Repeat("0", MaxBytes+1),
		"BOOT-INF/classes/db/":       "",
	})
	for _, p := range []string{"link.sql", "big.sql", "db", "missing.sql", "../BOOT-INF/classes/big.sql"} {
		_, err := FromJar(j, []Script{{"mysql", p}}, t.TempDir())
		if err == nil {
			t.Errorf("%s staged", p)
		} else if p != "../BOOT-INF/classes/big.sql" && !strings.Contains(err.Error(), p) {
			t.Errorf("%s: error does not name it: %v", p, err)
		}
	}
}

func TestFromJarNeedsAJar(t *testing.T) {
	path := filepath.Join(t.TempDir(), "not.jar")
	write(t, path, "plain text")
	if _, err := FromJar(path, []Script{{"mysql", "a.sql"}}, t.TempDir()); err == nil {
		t.Error("a non-zip file was read as a jar")
	}
}

func TestFromJarWithoutScriptsOnlyClears(t *testing.T) {
	path := filepath.Join(t.TempDir(), "app.jar")
	write(t, path, "not read: nothing is declared")
	dest := t.TempDir()
	write(t, filepath.Join(dest, "mysql/01-old.sql"), "x")
	got, err := FromJar(path, nil, dest)
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v, %v", got, err)
	}
	if _, err := os.Stat(filepath.Join(dest, "mysql")); !os.IsNotExist(err) {
		t.Error("stale scripts kept")
	}
}
