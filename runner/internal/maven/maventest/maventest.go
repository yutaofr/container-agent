// Package maventest serves a Maven-layout repository with basic auth. It stands in for
// Nexus or Artifactory in tests: the Runner only ever uses the standard repository layout.
package maventest

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// Repo is a fake repository. Files are served at /<path>; every request needs the
// configured basic-auth credentials.
type Repo struct {
	*httptest.Server
	User, Password string

	mu    sync.Mutex
	files map[string][]byte
}

// New starts a repository and stops it when the test ends.
func New(t testing.TB, user, password string) *Repo {
	r := &Repo{User: user, Password: password, files: map[string][]byte{}}
	r.Server = httptest.NewServer(http.HandlerFunc(r.serve))
	t.Cleanup(r.Close)
	return r
}

func (r *Repo) serve(w http.ResponseWriter, req *http.Request) {
	if u, p, ok := req.BasicAuth(); !ok || u != r.User || p != r.Password {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	r.mu.Lock()
	data, ok := r.files[strings.TrimPrefix(req.URL.Path, "/")]
	r.mu.Unlock()
	if !ok {
		http.NotFound(w, req)
		return
	}
	_, _ = w.Write(data)
}

// Put stores data at path, plus path.sha1 when withSHA1 is set.
func (r *Repo) Put(path string, data []byte, withSHA1 bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.files[path] = data
	if withSHA1 {
		sum := sha1.Sum(data)
		r.files[path+".sha1"] = []byte(hex.EncodeToString(sum[:]))
	}
}

// PutRelease stores group:artifact:version as a jar with its .sha1.
func (r *Repo) PutRelease(group, artifact, version string, data []byte) {
	dir := strings.ReplaceAll(group, ".", "/") + "/" + artifact + "/" + version
	r.Put(fmt.Sprintf("%s/%s-%s.jar", dir, artifact, version), data, true)
}

// PutSnapshot stores a timestamped snapshot jar, its .sha1, and maven-metadata.xml, and
// returns the resolved version, e.g. 2.0-20261017.083000-3.
func (r *Repo) PutSnapshot(group, artifact, version, timestamp, build string, data []byte) string {
	dir := strings.ReplaceAll(group, ".", "/") + "/" + artifact + "/" + version
	resolved := strings.TrimSuffix(version, "-SNAPSHOT") + "-" + timestamp + "-" + build
	r.Put(fmt.Sprintf("%s/%s-%s.jar", dir, artifact, resolved), data, true)
	r.Put(dir+"/maven-metadata.xml", []byte(fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<metadata modelVersion="1.1.0">
  <groupId>%s</groupId>
  <artifactId>%s</artifactId>
  <version>%s</version>
  <versioning>
    <snapshot><timestamp>%s</timestamp><buildNumber>%s</buildNumber></snapshot>
    <snapshotVersions>
      <snapshotVersion><extension>pom</extension><value>%s</value></snapshotVersion>
      <snapshotVersion><extension>jar</extension><value>%s</value></snapshotVersion>
    </snapshotVersions>
  </versioning>
</metadata>
`, group, artifact, version, timestamp, build, resolved, resolved)), false)
	return resolved
}

// SettingsXML writes a settings.xml into dir whose mirror points at the repository, with
// the repository's credentials, and returns its path.
func (r *Repo) SettingsXML(t testing.TB, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "settings.xml")
	body := fmt.Sprintf(`<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror><id>company-nexus</id><mirrorOf>*</mirrorOf><url>%s/</url></mirror>
  </mirrors>
  <servers>
    <server><id>company-nexus</id><username>%s</username><password>%s</password></server>
  </servers>
</settings>
`, r.URL, r.User, r.Password)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}
