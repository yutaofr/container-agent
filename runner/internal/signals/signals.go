// Package signals collects a redacted, fingerprinted diagnostic snapshot of a cluster.
// Raw logs never leave this package: only filtered, redacted lines do.
package signals

import (
	"context"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/fingerprint"
	"github.com/yutaofr/container-agent/runner/internal/redact"
)

// Options bound how much log text a snapshot carries.
type Options struct {
	TailLines     int // lines read from each container's log
	MaxErrorLines int // error lines kept per service
	KeepTail      int // last lines kept per service, error or not
}

// DefaultOptions match design section 5.1.
var DefaultOptions = Options{TailLines: 200, MaxErrorLines: 20, KeepTail: 5}

// Service is one container's state and evidence.
type Service struct {
	Service      string   `json:"service"`
	Container    string   `json:"container"`
	State        string   `json:"state"`
	ExitCode     int      `json:"exit_code"`
	OOMKilled    bool     `json:"oom_killed"`
	RestartCount int      `json:"restart_count"`
	Health       string   `json:"health,omitempty"`     // "", starting, healthy, unhealthy
	HealthLog    string   `json:"health_log,omitempty"` // last healthcheck output, redacted
	ErrorLines   []string `json:"error_lines"`
	Tail         []string `json:"tail"`
	Signatures   []string `json:"signatures"`
}

// Snapshot is what the Runner reports to the platform for diagnosis.
type Snapshot struct {
	Project  string    `json:"project"`
	Services []Service `json:"services"`
}

var errorLine = regexp.MustCompile(`(?i)error|exception|fatal|panic|caused by`)

// Collect inspects every container of a compose project and reads its recent logs.
func Collect(ctx context.Context, d dockercli.Docker, project string, opt Options) (Snapshot, error) {
	cs, err := dockercli.ProjectContainers(ctx, d, project)
	if err != nil {
		return Snapshot{}, err
	}
	snap := Snapshot{Project: project, Services: []Service{}}
	for _, c := range cs {
		// A container removed since inspect has no logs; its state still belongs in the snapshot.
		logs, _ := d.Combined(ctx, "logs", "--tail", strconv.Itoa(opt.TailLines), c.ID)
		snap.Services = append(snap.Services, FromContainer(c, string(logs), opt))
	}
	sort.Slice(snap.Services, func(i, j int) bool { return snap.Services[i].Service < snap.Services[j].Service })
	return snap, nil
}

// FromContainer builds one service entry. Every line is redacted before it is kept or
// fingerprinted.
func FromContainer(c dockercli.Container, logs string, opt Options) Service {
	s := Service{
		Service:      c.Service(),
		Container:    strings.TrimPrefix(c.Name, "/"),
		State:        c.State.Status,
		ExitCode:     c.State.ExitCode,
		OOMKilled:    c.State.OOMKilled,
		RestartCount: c.RestartCount,
		ErrorLines:   []string{},
		Tail:         []string{},
	}
	if h := c.State.Health; h != nil {
		s.Health = h.Status
		if n := len(h.Log); n > 0 {
			s.HealthLog = redact.String(strings.TrimSpace(h.Log[n-1].Output))
		}
	}

	sigs := map[string]bool{}
	var lines []string
	for _, l := range strings.Split(logs, "\n") {
		if l = strings.TrimRight(l, "\r "); l != "" {
			lines = append(lines, redact.String(l))
		}
	}
	for _, l := range lines {
		if !errorLine.MatchString(l) {
			continue
		}
		if len(s.ErrorLines) < opt.MaxErrorLines {
			s.ErrorLines = append(s.ErrorLines, l)
		}
		sigs[fingerprint.FromLog(s.Service, l).String()] = true
	}
	if start := len(lines) - opt.KeepTail; start > 0 {
		lines = lines[start:]
	}
	s.Tail = append(s.Tail, lines...)

	if stopped := c.State.Status == "exited" || c.State.Status == "dead"; stopped || c.State.Restarting || c.State.OOMKilled {
		sigs[fingerprint.FromExit(s.Service, c.State.ExitCode, c.State.OOMKilled).String()] = true
	}
	s.Signatures = make([]string, 0, len(sigs))
	for k := range sigs {
		s.Signatures = append(s.Signatures, k)
	}
	sort.Strings(s.Signatures)
	return s
}
