package compose

import (
	"strings"

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/guard"
)

// Lint rule IDs. Users approve exceptions as "service:rule".
const (
	RulePrivileged  = "privileged"
	RuleDockerSock  = "docker-sock"
	RuleHostNetwork = "host-network"
	RuleHostMount   = "host-mount"
	RuleCapAdd      = "cap-add"
)

// Finding is one risky setting in one service.
type Finding struct {
	Service string `json:"service"`
	Rule    string `json:"rule"`
	Detail  string `json:"detail"`
}

// Lint reports risky settings. Bind mounts must sit under one of allowedRoots.
// allow holds "service:rule" pairs the user approved; those are not reported.
func Lint(p *types.Project, allowedRoots []string, allow map[string]bool) []Finding {
	out := []Finding{}
	add := func(svc, rule, detail string) {
		if !allow[svc+":"+rule] {
			out = append(out, Finding{Service: svc, Rule: rule, Detail: detail})
		}
	}
	for _, name := range serviceNames(p) {
		s := p.Services[name]
		if s.Privileged {
			add(name, RulePrivileged, "privileged: true")
		}
		if s.NetworkMode == "host" {
			add(name, RuleHostNetwork, "network_mode: host")
		}
		if len(s.CapAdd) > 0 {
			add(name, RuleCapAdd, strings.Join(s.CapAdd, ","))
		}
		for _, v := range s.Volumes {
			if v.Type != types.VolumeTypeBind {
				continue
			}
			if strings.HasSuffix(v.Source, "docker.sock") {
				add(name, RuleDockerSock, v.Source)
				continue
			}
			if !withinAny(v.Source, allowedRoots) {
				add(name, RuleHostMount, v.Source)
			}
		}
	}
	return out
}

func withinAny(path string, roots []string) bool {
	for _, r := range roots {
		if guard.Within(r, path) {
			return true
		}
	}
	return false
}
