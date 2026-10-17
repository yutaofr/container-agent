package compose

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

const riskyCompose = `
services:
  web:
    image: nginx:1.27
    privileged: true
    network_mode: host
    cap_add: [NET_ADMIN]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /etc:/host-etc:ro
      - ./data:/data
`

func TestLintFindsRiskySettings(t *testing.T) {
	path := writeCompose(t, riskyCompose)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	got := Lint(p, []string{filepath.Dir(path)}, nil)
	want := []Finding{
		{"web", RulePrivileged, "privileged: true"},
		{"web", RuleHostNetwork, "network_mode: host"},
		{"web", RuleCapAdd, "NET_ADMIN"},
		{"web", RuleDockerSock, "/var/run/docker.sock"},
		{"web", RuleHostMount, "/etc"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Lint() =\n%v\nwant\n%v", got, want)
	}
}

func TestLintHonoursApprovals(t *testing.T) {
	path := writeCompose(t, riskyCompose)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	allow := map[string]bool{
		"web:privileged": true, "web:host-network": true, "web:cap-add": true,
		"web:docker-sock": true, "web:host-mount": true,
	}
	if got := Lint(p, []string{filepath.Dir(path)}, allow); len(got) != 0 {
		t.Errorf("Lint() with all approvals = %v, want none", got)
	}
}

// bindFindings lints a one-service project whose only volume is a bind mount of source.
func bindFindings(t *testing.T, ws, source string) []Finding {
	t.Helper()
	path := filepath.Join(ws, "compose.yaml")
	body := fmt.Sprintf("services:\n  web:\n    image: nginx:1.27\n    volumes: [\"%s:/data\"]\n", source)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	return Lint(p, []string{ws}, nil)
}

func TestLintBindSourcesAreSymlinkResolved(t *testing.T) {
	outside := t.TempDir()
	elsewhere := t.TempDir() // outside the workspace, unrelated to the socket
	sockFile := filepath.Join(outside, "docker.sock")
	if err := os.WriteFile(sockFile, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	runDir := filepath.Join(outside, "run")
	if err := os.Mkdir(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// Stand-in for /var/run/docker.sock so the test does not depend on the machine.
	old := dockerSocketPaths
	dockerSocketPaths = []string{filepath.Join(runDir, "docker.sock")}
	t.Cleanup(func() { dockerSocketPaths = old })

	cases := []struct {
		name   string
		setup  func(t *testing.T, ws string)
		source func(ws string) string
		want   string // expected rule, "" for none
	}{
		{"symlink escaping the workspace",
			func(t *testing.T, ws string) { link(t, elsewhere, ws, "escape") },
			func(ws string) string { return filepath.Join(ws, "escape") }, RuleHostMount},
		{"symlink to a docker socket file",
			func(t *testing.T, ws string) { link(t, sockFile, ws, "sock") },
			func(ws string) string { return filepath.Join(ws, "sock") }, RuleDockerSock},
		{"directory holding the socket",
			nil, func(string) string { return runDir }, RuleDockerSock},
		{"ancestor of the socket directory",
			nil, func(string) string { return outside }, RuleDockerSock},
		{"dangling symlink fails closed",
			func(t *testing.T, ws string) { link(t, filepath.Join(outside, "gone"), ws, "dangling") },
			func(ws string) string { return filepath.Join(ws, "dangling") }, RuleHostMount},
		{"not-yet-created nested dir in workspace",
			nil, func(ws string) string { return filepath.Join(ws, "a", "b", "data") }, ""},
		{"symlink that stays inside the workspace",
			func(t *testing.T, ws string) {
				if err := os.Mkdir(filepath.Join(ws, "real"), 0o755); err != nil {
					t.Fatal(err)
				}
				link(t, filepath.Join(ws, "real"), ws, "inlink")
			},
			func(ws string) string { return filepath.Join(ws, "inlink") }, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ws := t.TempDir()
			if c.setup != nil {
				c.setup(t, ws)
			}
			src := c.source(ws)
			got := bindFindings(t, ws, src)
			var want []Finding
			if c.want != "" {
				want = []Finding{{"web", c.want, src}}
			} else {
				want = []Finding{}
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("Lint() = %v, want %v", got, want)
			}
		})
	}
}

// link creates ws/name pointing at target.
func link(t *testing.T, target, ws, name string) {
	t.Helper()
	if err := os.Symlink(target, filepath.Join(ws, name)); err != nil {
		t.Fatal(err)
	}
}
