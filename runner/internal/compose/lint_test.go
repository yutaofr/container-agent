package compose

import (
	"context"
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
