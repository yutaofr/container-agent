package maven

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeSettings(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.xml")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadRepoPicksCentralMirrorAndItsServer(t *testing.T) {
	t.Setenv("NEXUS_TOKEN", "s3cret")
	path := writeSettings(t, `<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror><id>internal-only</id><mirrorOf>internal</mirrorOf><url>https://other/</url></mirror>
    <mirror><id>nexus</id><mirrorOf>external:*, central</mirrorOf><url>https://nexus.acme.com/repository/maven-public/</url></mirror>
  </mirrors>
  <servers>
    <server><id>other</id><username>x</username><password>y</password></server>
    <server><id>nexus</id><username>dev</username><password>${env.NEXUS_TOKEN}</password></server>
  </servers>
</settings>`)
	repo, err := LoadRepo(path)
	if err != nil {
		t.Fatal(err)
	}
	want := Repo{ID: "nexus", URL: "https://nexus.acme.com/repository/maven-public", Username: "dev", Password: "s3cret"}
	if repo != want {
		t.Errorf("LoadRepo() = %+v, want %+v", repo, want)
	}
}

func TestLoadRepoErrors(t *testing.T) {
	cases := []struct {
		name, body string
		want       error
		wantText   string
	}{
		{"no usable mirror",
			`<settings><mirrors><mirror><id>a</id><mirrorOf>internal</mirrorOf><url>u</url></mirror></mirrors></settings>`,
			ErrNoMirror, ""},
		{"encrypted password",
			`<settings><mirrors><mirror><id>n</id><mirrorOf>*</mirrorOf><url>u</url></mirror></mirrors>
			 <servers><server><id>n</id><username>u</username><password>{COQLCE6DU6GtcS5P=}</password></server></servers></settings>`,
			ErrEncryptedPassword, ""},
		{"unset env var",
			`<settings><mirrors><mirror><id>n</id><mirrorOf>*</mirrorOf><url>u</url></mirror></mirrors>
			 <servers><server><id>n</id><username>u</username><password>${env.CAGENT_TEST_UNSET}</password></server></servers></settings>`,
			nil, "unset environment variable CAGENT_TEST_UNSET"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := LoadRepo(writeSettings(t, c.body))
			if c.want != nil && !errors.Is(err, c.want) {
				t.Errorf("got %v, want %v", err, c.want)
			}
			if c.wantText != "" && (err == nil || !strings.Contains(err.Error(), c.wantText)) {
				t.Errorf("got %v, want text %q", err, c.wantText)
			}
		})
	}
}

func TestParseCoord(t *testing.T) {
	c, err := ParseCoord("com.acme:order-service:2.0-SNAPSHOT")
	if err != nil {
		t.Fatal(err)
	}
	if c != (Coord{"com.acme", "order-service", "2.0-SNAPSHOT", "jar"}) || !c.Snapshot() {
		t.Errorf("got %+v", c)
	}
	if c.dir() != "com/acme/order-service/2.0-SNAPSHOT" {
		t.Errorf("dir = %s", c.dir())
	}
	for _, bad := range []string{"a:b", "a:b:c:d:e", "a:..:1", "a:b/c:1", "a:b:1:", "-x:b:1"} {
		if _, err := ParseCoord(bad); err == nil {
			t.Errorf("ParseCoord(%q) accepted", bad)
		}
	}
}
