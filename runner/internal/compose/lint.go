package compose

import (
	"path/filepath"
	"sort"
	"strings"

	"github.com/compose-spec/compose-go/v2/types"

	"github.com/yutaofr/container-agent/runner/internal/guard"
)

// Lint rule IDs. Users approve exceptions as "service:rule".
const (
	RulePrivileged   = "privileged"
	RuleDockerSock   = "docker-sock"
	RuleHostNetwork  = "host-network"
	RuleHostMount    = "host-mount"
	RuleCapAdd       = "cap-add"
	RulePidHost      = "pid-host"
	RuleIpcHost      = "ipc-host"
	RuleUserNSHost   = "userns-host"
	RuleDevices      = "devices"
	RuleSecurityOpt  = "security-opt"
	RuleCgroupParent = "cgroup-parent"
	RuleVolumeDriver = "volume-driver"
)

// dockerSocketPaths are the well-known Docker daemon socket locations. A var so tests can
// substitute paths that exist on every machine.
var dockerSocketPaths = []string{"/var/run/docker.sock", "/run/docker.sock"}

// Finding is one risky setting. Service is the compose service name; for a finding on a
// top-level volume it is the volume name, so approvals read "volume:rule".
type Finding struct {
	Service string `json:"service"`
	Rule    string `json:"rule"`
	Detail  string `json:"detail"`
}

// Lint reports risky settings. Bind mounts must sit under one of allowedRoots.
// allow holds "service:rule" pairs the user approved; those are not reported.
//
// Mount sources and roots are compared after symlink resolution, so a link inside the
// workspace cannot lead outside it. A source that cannot be resolved is reported as a
// host mount: the check fails closed.
func Lint(p *types.Project, allowedRoots []string, allow map[string]bool) []Finding {
	out := []Finding{}
	add := func(svc, rule, detail string) {
		if !allow[svc+":"+rule] {
			out = append(out, Finding{Service: svc, Rule: rule, Detail: detail})
		}
	}
	roots := resolveRoots(allowedRoots)
	for _, name := range serviceNames(p) {
		s := p.Services[name]
		if s.Privileged {
			add(name, RulePrivileged, "privileged: true")
		}
		if s.NetworkMode == "host" {
			add(name, RuleHostNetwork, "network_mode: host")
		}
		if s.Pid == "host" {
			add(name, RulePidHost, "pid: host")
		}
		if s.Ipc == "host" {
			add(name, RuleIpcHost, "ipc: host")
		}
		if s.UserNSMode == "host" {
			add(name, RuleUserNSHost, "userns_mode: host")
		}
		if len(s.CapAdd) > 0 {
			add(name, RuleCapAdd, strings.Join(s.CapAdd, ","))
		}
		if len(s.Devices) > 0 {
			sources := make([]string, len(s.Devices))
			for i, d := range s.Devices {
				sources[i] = d.Source
			}
			add(name, RuleDevices, strings.Join(sources, ","))
		}
		if risky := riskySecurityOpts(s.SecurityOpt); len(risky) > 0 {
			add(name, RuleSecurityOpt, strings.Join(risky, ","))
		}
		if s.CgroupParent != "" {
			add(name, RuleCgroupParent, s.CgroupParent)
		}
		for _, v := range s.Volumes {
			if v.Type == types.VolumeTypeBind {
				checkHostPath(add, name, v.Source, roots)
			}
		}
	}
	for _, name := range volumeNames(p) {
		lintVolume(add, name, p.Volumes[name], p.WorkingDir, roots)
	}
	return out
}

// riskySecurityOpts returns the entries that switch off a confinement layer. Docker
// accepts both key=value and key:value, so entries are compared in the first form.
func riskySecurityOpts(opts []string) []string {
	var risky []string
	for _, o := range opts {
		norm := strings.Replace(strings.ToLower(strings.TrimSpace(o)), ":", "=", 1)
		switch norm {
		case "seccomp=unconfined", "apparmor=unconfined", "label=disable", "no-new-privileges=false":
			risky = append(risky, o)
		}
	}
	return risky
}

// lintVolume checks one top-level volume. Only the local driver is understood: with
// bind-style options its device is a host path and gets the same checks as a bind mount;
// any other driver is reported for approval, since its options are opaque to the Runner.
func lintVolume(add func(svc, rule, detail string), name string, v types.VolumeConfig, workingDir string, roots []string) {
	if bool(v.External) {
		return
	}
	if v.Driver != "" && v.Driver != "local" {
		add(name, RuleVolumeDriver, v.Driver)
		return
	}
	device := v.DriverOpts["device"]
	if device == "" || !isBindOpts(v.DriverOpts) {
		return
	}
	if !filepath.IsAbs(device) {
		device = filepath.Join(workingDir, device)
	}
	checkHostPath(add, name, device, roots)
}

// isBindOpts reports whether local-driver options make "device" a bind-mounted host path.
func isBindOpts(opts types.Options) bool {
	if t := opts["type"]; t == "none" || t == "bind" {
		return true
	}
	for _, o := range strings.Split(opts["o"], ",") {
		if o = strings.TrimSpace(o); o == "bind" || o == "rbind" {
			return true
		}
	}
	return false
}

func volumeNames(p *types.Project) []string {
	names := make([]string, 0, len(p.Volumes))
	for n := range p.Volumes {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// resolveRoots resolves allowed roots through symlinks. A root that cannot be resolved
// is dropped, which can only make the check stricter.
func resolveRoots(roots []string) []string {
	out := make([]string, 0, len(roots))
	for _, r := range roots {
		if rr, err := guard.Resolve(r); err == nil {
			out = append(out, rr)
		}
	}
	return out
}

// checkHostPath reports a host path that is a Docker socket (or holds one) or lies
// outside roots. Detail shows the path as declared; the checks use the resolved path.
func checkHostPath(add func(svc, rule, detail string), svc, source string, roots []string) {
	if strings.HasSuffix(source, "docker.sock") {
		add(svc, RuleDockerSock, source)
		return
	}
	resolved, err := guard.Resolve(source)
	if err != nil {
		add(svc, RuleHostMount, source)
		return
	}
	if isDockerSocketOrParent(resolved) {
		add(svc, RuleDockerSock, source)
		return
	}
	if !withinAny(resolved, roots) {
		add(svc, RuleHostMount, source)
	}
}

// isDockerSocketOrParent reports whether resolved is a Docker socket, or a directory
// containing one such as /var/run or /run, which exposes the socket just as well.
func isDockerSocketOrParent(resolved string) bool {
	if strings.HasSuffix(resolved, "docker.sock") {
		return true
	}
	for _, sock := range dockerSocketPaths {
		if guard.Within(resolved, sock) {
			return true
		}
		// The socket path itself may sit behind a symlink such as /var/run -> /run.
		if rs, err := guard.Resolve(sock); err == nil && guard.Within(resolved, rs) {
			return true
		}
	}
	return false
}

func withinAny(path string, roots []string) bool {
	for _, r := range roots {
		if guard.Within(r, path) {
			return true
		}
	}
	return false
}
