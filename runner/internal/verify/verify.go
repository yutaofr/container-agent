// Package verify runs acceptance levels L1–L3 against a cluster snapshot.
//
//	L1  every container is running; a one-shot init container has exited 0
//	L2  every container with a healthcheck is healthy
//	L3  every probe passes
//
// Each level runs only when the one below it passed. Probes run from a throwaway busybox
// container on the cluster's network, because container IPs are not reachable from the
// host on every platform (Docker Desktop on macOS, for one).
package verify

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/signals"
)

// ProbeImage runs probes. busybox ships wget and nc.
const ProbeImage = "busybox:1.37"

// Probe checks one service from inside the cluster network. Set exactly one of HTTP or TCP.
type Probe struct {
	Service string     `json:"service"`
	HTTP    *HTTPProbe `json:"http,omitempty"`
	TCP     *TCPProbe  `json:"tcp,omitempty"`
}

// HTTPProbe expects status Expect (200 when zero) from GET http://service:Port/Path.
type HTTPProbe struct {
	Port   int    `json:"port"`
	Path   string `json:"path"`
	Expect int    `json:"expect"`
}

// TCPProbe expects service:Port to accept a connection.
type TCPProbe struct {
	Port int `json:"port"`
}

// Result is one check at one level.
type Result struct {
	Level   string `json:"level"`
	Service string `json:"service"`
	OK      bool   `json:"ok"`
	Detail  string `json:"detail"`
}

// Report is the outcome. Reached is the highest level whose checks all passed ("" for none).
type Report struct {
	Passed  bool     `json:"passed"`
	Reached string   `json:"reached"`
	Results []Result `json:"results"`
}

// Verify checks snap level by level, running probes only once L1 and L2 pass.
func Verify(ctx context.Context, d dockercli.Docker, snap signals.Snapshot, probes []Probe) Report {
	rep := Report{Results: []Result{}}
	add := func(r Result) bool { rep.Results = append(rep.Results, r); return r.OK }

	l1 := len(snap.Services) > 0
	if !l1 {
		add(Result{Level: "L1", OK: false, Detail: "no containers"})
	}
	for _, s := range snap.Services {
		l1 = add(levelOne(s)) && l1
	}
	if !l1 {
		return rep
	}
	rep.Reached = "L1"

	l2 := true
	for _, s := range snap.Services {
		l2 = add(levelTwo(s)) && l2
	}
	if !l2 {
		return rep
	}
	rep.Reached = "L2"

	services := map[string]bool{}
	for _, s := range snap.Services {
		services[s.Service] = true
	}
	l3 := true
	for _, p := range probes {
		l3 = add(runProbe(ctx, d, snap.Project+"_default", p, services)) && l3
	}
	if l3 {
		if len(probes) > 0 {
			rep.Reached = "L3"
		}
		rep.Passed = true
	}
	return rep
}

func levelOne(s signals.Service) Result {
	if s.Role == signals.RoleInit {
		r := Result{Level: "L1", Service: s.Service, OK: s.Completed(), Detail: "init completed"}
		switch {
		case s.State == "running" || s.State == "created":
			r.Detail = "init still running"
		case !r.OK:
			r.Detail = fmt.Sprintf("init failed, exit %d", s.ExitCode)
			if n := len(s.Tail); n > 0 {
				r.Detail += ": " + s.Tail[n-1]
			}
		}
		return r
	}
	r := Result{Level: "L1", Service: s.Service, OK: s.State == "running", Detail: s.State}
	switch {
	case !r.OK:
		r.Detail = fmt.Sprintf("%s, exit %d", s.State, s.ExitCode)
		if s.OOMKilled {
			r.Detail += ", OOM killed"
		}
	case s.RestartCount > 0:
		r.Detail = fmt.Sprintf("running, restarted %d times", s.RestartCount)
	}
	return r
}

func levelTwo(s signals.Service) Result {
	switch s.Health {
	case "":
		return Result{Level: "L2", Service: s.Service, OK: true, Detail: "no healthcheck"}
	case "healthy":
		return Result{Level: "L2", Service: s.Service, OK: true, Detail: "healthy"}
	default:
		detail := s.Health
		if s.HealthLog != "" {
			detail += ": " + s.HealthLog
		}
		return Result{Level: "L2", Service: s.Service, OK: false, Detail: detail}
	}
}

// statusLine matches the status line wget -S prints; the last one wins after redirects.
var statusLine = regexp.MustCompile(`HTTP/\d(?:\.\d)? (\d{3})`)

func runProbe(ctx context.Context, d dockercli.Docker, network string, p Probe, services map[string]bool) Result {
	fail := func(detail string) Result { return Result{Level: "L3", Service: p.Service, Detail: detail} }
	if !services[p.Service] {
		return fail("unknown service")
	}
	run := []string{"run", "--rm", "--network", network, ProbeImage}
	switch {
	case p.HTTP != nil && p.TCP == nil:
		h := p.HTTP
		if !strings.HasPrefix(h.Path, "/") || strings.ContainsAny(h.Path, " \t\r\n") {
			return fail(fmt.Sprintf("invalid path %q", h.Path))
		}
		expect := h.Expect
		if expect == 0 {
			expect = 200
		}
		url := fmt.Sprintf("http://%s:%d%s", p.Service, h.Port, h.Path)
		out, _ := d.Combined(ctx, append(run, "wget", "-S", "-q", "-O", "/dev/null", "-T", "5", url)...)
		matches := statusLine.FindAllSubmatch(out, -1)
		if len(matches) == 0 {
			return fail(fmt.Sprintf("GET %s: no response: %s", url, strings.TrimSpace(string(out))))
		}
		got, _ := strconv.Atoi(string(matches[len(matches)-1][1]))
		r := Result{Level: "L3", Service: p.Service, OK: got == expect,
			Detail: fmt.Sprintf("GET %s returned %d, want %d", url, got, expect)}
		return r
	case p.TCP != nil && p.HTTP == nil:
		addr := fmt.Sprintf("%s:%d", p.Service, p.TCP.Port)
		_, err := d.Combined(ctx, append(run, "nc", "-z", "-w", "5", p.Service, strconv.Itoa(p.TCP.Port))...)
		if err != nil {
			return fail("TCP " + addr + " refused or timed out")
		}
		return Result{Level: "L3", Service: p.Service, OK: true, Detail: "TCP " + addr + " open"}
	default:
		return fail("probe needs exactly one of http or tcp")
	}
}
