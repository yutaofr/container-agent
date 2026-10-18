package inspect

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// project writes files (relative path -> content) into a fresh directory.
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
	return dir
}

func inspect(t *testing.T, dir string) Result {
	t.Helper()
	res, err := Inspect(dir)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func keys(m map[string]string) []string {
	var ks []string
	for k := range m {
		ks = append(ks, k)
	}
	slices.Sort(ks)
	return ks
}

func pom(modules ...string) string {
	var b strings.Builder
	b.WriteString("<project><artifactId>x</artifactId><modules>")
	for _, m := range modules {
		fmt.Fprintf(&b, "<module>%s</module>", m)
	}
	b.WriteString("</modules></project>")
	return b.String()
}

func TestInspectReadsOnlyTheFixedFiles(t *testing.T) {
	dir := project(t, map[string]string{
		"pom.xml":                                pom(),
		"Dockerfile":                             "FROM busybox\n",
		"src/main/resources/application.yml":     "server:\n  port: 8080\n",
		"README.md":                              "hello",
		"src/main/java/App.java":                 "class App {}",
		"src/main/resources/application-dev.yml": "x: 1",
	})
	got := keys(inspect(t, dir).Files)
	want := []string{"Dockerfile", "pom.xml", "src/main/resources/application.yml"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("files %v, want %v", got, want)
	}
}

func TestInspectFollowsMavenModules(t *testing.T) {
	dir := project(t, map[string]string{
		"pom.xml":          pom("api", "lib", "../escape", "/etc", "missing"),
		"api/pom.xml":      pom("impl"),
		"api/Dockerfile":   "FROM busybox\n",
		"api/impl/pom.xml": pom(),
		"lib/pom.xml":      pom(),
	})
	res := inspect(t, dir)
	if want := []string{"api", "lib", "api/impl"}; !reflect.DeepEqual(res.Modules, want) {
		t.Errorf("modules %v, want %v", res.Modules, want)
	}
	for _, f := range []string{"api/pom.xml", "api/Dockerfile", "api/impl/pom.xml", "lib/pom.xml"} {
		if _, ok := res.Files[f]; !ok {
			t.Errorf("missing %s in %v", f, keys(res.Files))
		}
	}
}

func TestInspectFollowsGradleIncludes(t *testing.T) {
	dir := project(t, map[string]string{
		"settings.gradle.kts":   "rootProject.name = \"shop\"\ninclude(\"web\", \":lib:core\")\ninclude 'api'\n",
		"web/build.gradle.kts":  "plugins { id(\"org.springframework.boot\") }",
		"lib/core/build.gradle": "",
		"api/build.gradle":      "",
	})
	res := inspect(t, dir)
	if want := []string{"web", "lib/core", "api"}; !reflect.DeepEqual(res.Modules, want) {
		t.Errorf("modules %v, want %v", res.Modules, want)
	}
	if _, ok := res.Files["web/build.gradle.kts"]; !ok {
		t.Errorf("files %v", keys(res.Files))
	}
}

func TestInspectReadsExistingDescriptors(t *testing.T) {
	dir := project(t, map[string]string{
		".cagent/service.yaml":       "name: a\n",
		".cagent/services/api.yaml":  "name: api\n",
		".cagent/services/notes.txt": "x",
		"api/.cagent/service.yaml":   "name: nested\n",
	})
	got := keys(inspect(t, dir).Files)
	want := []string{".cagent/service.yaml", ".cagent/services/api.yaml"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("files %v, want %v", got, want)
	}
}

func TestInspectSkipsSymlinksLeavingTheProject(t *testing.T) {
	outside := project(t, map[string]string{"pom.xml": pom(), "secret/pom.xml": pom()})
	dir := project(t, map[string]string{"pom.xml": pom("evil")})
	if err := os.Symlink(filepath.Join(outside, "secret"), filepath.Join(dir, "evil")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(outside, "pom.xml"), filepath.Join(dir, "Dockerfile")); err != nil {
		t.Fatal(err)
	}
	res := inspect(t, dir)
	if len(res.Modules) != 0 || !reflect.DeepEqual(keys(res.Files), []string{"pom.xml"}) {
		t.Errorf("modules %v, files %v", res.Modules, keys(res.Files))
	}
}

func TestInspectRedactsSecrets(t *testing.T) {
	dir := project(t, map[string]string{
		"src/main/resources/application.properties": "spring.datasource.password=hunter2\n" +
			"app.secret=${APP_SECRET}\nserver.port=8080\n",
	})
	got := inspect(t, dir).Files["src/main/resources/application.properties"]
	// Secrets are masked; references to them stay, so a draft knows which variable to set.
	if strings.Contains(got, "hunter2") || !strings.Contains(got, "app.secret=${APP_SECRET}") ||
		!strings.Contains(got, "server.port=8080") {
		t.Errorf("content %q", got)
	}
}

func TestInspectTruncatesLargeFiles(t *testing.T) {
	big := strings.Repeat("a=b\n", MaxFileBytes/2)
	dir := project(t, map[string]string{"src/main/resources/application.properties": big})
	res := inspect(t, dir)
	if n := len(res.Files["src/main/resources/application.properties"]); n != MaxFileBytes {
		t.Errorf("kept %d bytes, want %d", n, MaxFileBytes)
	}
	if !reflect.DeepEqual(res.Truncated, []string{"src/main/resources/application.properties"}) {
		t.Errorf("truncated %v", res.Truncated)
	}
}

func TestInspectStopsAtTheTotalLimit(t *testing.T) {
	files := map[string]string{}
	var modules []string
	for i := range 40 {
		m := fmt.Sprintf("m%02d", i)
		modules = append(modules, m)
		files[m+"/pom.xml"] = pom() + strings.Repeat(" ", MaxFileBytes)
	}
	files["pom.xml"] = pom(modules...)
	res := inspect(t, project(t, files))
	total := 0
	for _, c := range res.Files {
		total += len(c)
	}
	if total > MaxTotalBytes || len(res.Truncated) == 0 {
		t.Errorf("total %d, truncated %d", total, len(res.Truncated))
	}
}

func TestInspectCapsModules(t *testing.T) {
	files := map[string]string{}
	var modules []string
	for i := range MaxModules + 5 {
		m := fmt.Sprintf("m%02d", i)
		modules = append(modules, m)
		files[m+"/pom.xml"] = pom()
	}
	files["pom.xml"] = pom(modules...)
	res := inspect(t, project(t, files))
	if len(res.Modules) != MaxModules || !reflect.DeepEqual(res.Truncated, []string{"modules"}) {
		t.Errorf("modules %d, truncated %v", len(res.Modules), res.Truncated)
	}
}

func TestInspectListsInitResourcesByPath(t *testing.T) {
	dir := project(t, map[string]string{
		"api/src/main/resources/db/migration/V1__init.sql": "create table t (id int);",
		"api/src/main/resources/db/changelog/master.xml":   "<databaseChangeLog/>",
		"cassandra/keyspace.cql":                           "CREATE KEYSPACE k;",
		"sql/schema-mysql.sql":                             "create table s (id int);",
		"docker/db/init/01-users.sql":                      "insert into u values (1);",
		"node_modules/pkg/schema.sql":                      "ignored",
		"api/target/classes/db/migration/V1__init.sql":     "ignored",
		"docs/notes.sql":                                   "not an init resource",
	})
	got := inspect(t, dir).Resources
	want := []string{
		"api/src/main/resources/db/changelog/master.xml",
		"api/src/main/resources/db/migration/V1__init.sql",
		"cassandra/keyspace.cql",
		"docker/db/init/01-users.sql",
		"sql/schema-mysql.sql",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("resources %v, want %v", got, want)
	}
	for _, f := range inspect(t, dir).Files {
		if strings.Contains(f, "create table") {
			t.Error("a resource file's content was read")
		}
	}
}

func TestInspectNeedsADirectory(t *testing.T) {
	if _, err := Inspect(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Error("missing directory accepted")
	}
}
