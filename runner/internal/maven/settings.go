// Package maven downloads artifacts from a Maven-layout repository (Nexus, Artifactory)
// using the repository and credentials the developer already has in ~/.m2/settings.xml.
package maven

import (
	"encoding/xml"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// ErrEncryptedPassword means settings.xml holds a {…} password encrypted with
// settings-security.xml, which the Runner does not decrypt.
var ErrEncryptedPassword = errors.New("encrypted settings.xml passwords are not supported; use a token, or ${env.VAR}")

// ErrNoMirror means settings.xml names no repository the Runner can use.
var ErrNoMirror = errors.New("settings.xml has no <mirror> whose mirrorOf covers * or central")

// Repo is a repository base URL with optional basic-auth credentials.
type Repo struct {
	ID       string
	URL      string
	Username string
	Password string
}

type settings struct {
	Mirrors []struct {
		ID       string `xml:"id"`
		URL      string `xml:"url"`
		MirrorOf string `xml:"mirrorOf"`
	} `xml:"mirrors>mirror"`
	Servers []struct {
		ID       string `xml:"id"`
		Username string `xml:"username"`
		Password string `xml:"password"`
	} `xml:"servers>server"`
}

// DefaultSettingsPath is ~/.m2/settings.xml.
func DefaultSettingsPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".m2", "settings.xml"), nil
}

// LoadRepo reads settings.xml and returns the first mirror covering * or central, with
// the credentials of the <server> that has the same id.
func LoadRepo(path string) (Repo, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Repo{}, err
	}
	var s settings
	if err := xml.Unmarshal(data, &s); err != nil {
		return Repo{}, fmt.Errorf("parse %s: %w", path, err)
	}
	for _, m := range s.Mirrors {
		if !coversCentral(m.MirrorOf) {
			continue
		}
		repo := Repo{ID: m.ID, URL: strings.TrimRight(strings.TrimSpace(m.URL), "/")}
		for _, srv := range s.Servers {
			if srv.ID != m.ID {
				continue
			}
			if repo.Username, err = expandEnv(strings.TrimSpace(srv.Username)); err != nil {
				return Repo{}, err
			}
			pw := strings.TrimSpace(srv.Password)
			if strings.HasPrefix(pw, "{") && strings.HasSuffix(pw, "}") {
				return Repo{}, fmt.Errorf("server %s: %w", srv.ID, ErrEncryptedPassword)
			}
			if repo.Password, err = expandEnv(pw); err != nil {
				return Repo{}, err
			}
		}
		return repo, nil
	}
	return Repo{}, ErrNoMirror
}

func coversCentral(mirrorOf string) bool {
	for _, part := range strings.Split(mirrorOf, ",") {
		if p := strings.TrimSpace(part); p == "*" || p == "central" || p == "external:*" {
			return true
		}
	}
	return false
}

var envRef = regexp.MustCompile(`\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}`)

// expandEnv replaces ${env.NAME} with the environment variable, as Maven does.
func expandEnv(s string) (string, error) {
	var missing []string
	out := envRef.ReplaceAllStringFunc(s, func(ref string) string {
		name := envRef.FindStringSubmatch(ref)[1]
		v, ok := os.LookupEnv(name)
		if !ok {
			missing = append(missing, name)
		}
		return v
	})
	if len(missing) > 0 {
		return "", fmt.Errorf("settings.xml references unset environment variable %s", strings.Join(missing, ", "))
	}
	return out, nil
}
