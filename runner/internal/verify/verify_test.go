package verify

import (
	"context"
	"errors"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
	"github.com/yutaofr/container-agent/runner/internal/signals"
)

const (
	wgetWeb = "run --rm --network cagent-demo_default busybox:1.37 wget -S -q -O /dev/null -T 5 http://web:8080/"
	ncDB    = "run --rm --network cagent-demo_default busybox:1.37 nc -z -w 5 db 3306"
)

func snapshot(services ...signals.Service) signals.Snapshot {
	return signals.Snapshot{Project: "cagent-demo", Services: services}
}

var (
	webHealthy = signals.Service{Service: "web", State: "running", Health: "healthy"}
	dbRunning  = signals.Service{Service: "db", State: "running"}
)

func TestL1FailureStopsEarly(t *testing.T) {
	crashed := signals.Service{Service: "api", State: "exited", ExitCode: 137, OOMKilled: true}
	f := &dockercli.Fake{}
	rep := Verify(context.Background(), f, snapshot(webHealthy, crashed),
		[]Probe{{Service: "web", HTTP: &HTTPProbe{Port: 8080, Path: "/"}}})
	if rep.Passed || rep.Reached != "" {
		t.Errorf("got passed=%v reached=%q, want false and none", rep.Passed, rep.Reached)
	}
	if got := rep.Results[1]; got.OK || got.Detail != "exited, exit 137, OOM killed" {
		t.Errorf("api result = %+v", got)
	}
	if len(f.Calls) != 0 {
		t.Errorf("probes ran despite L1 failure: %v", f.Calls)
	}
}

func TestL2Unhealthy(t *testing.T) {
	sick := signals.Service{Service: "web", State: "running", Health: "unhealthy", HealthLog: "curl: (7) refused"}
	rep := Verify(context.Background(), &dockercli.Fake{}, snapshot(sick, dbRunning), nil)
	if rep.Passed || rep.Reached != "L1" {
		t.Errorf("got passed=%v reached=%q, want false and L1", rep.Passed, rep.Reached)
	}
	last := rep.Results[len(rep.Results)-2]
	if last.Detail != "unhealthy: curl: (7) refused" {
		t.Errorf("web L2 detail = %q", last.Detail)
	}
}

func TestNoProbesPassesAtL2(t *testing.T) {
	rep := Verify(context.Background(), &dockercli.Fake{}, snapshot(webHealthy, dbRunning), nil)
	if !rep.Passed || rep.Reached != "L2" {
		t.Errorf("got passed=%v reached=%q, want true and L2", rep.Passed, rep.Reached)
	}
}

func TestProbes(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		wgetWeb: {Out: "  HTTP/1.1 301 Moved\n  Location: /home\n  HTTP/1.1 200 OK\n  Content-Length: 3\n"},
		ncDB:    {},
	}}
	probes := []Probe{
		{Service: "web", HTTP: &HTTPProbe{Port: 8080, Path: "/"}},
		{Service: "db", TCP: &TCPProbe{Port: 3306}},
	}
	rep := Verify(context.Background(), f, snapshot(webHealthy, dbRunning), probes)
	if !rep.Passed || rep.Reached != "L3" {
		t.Fatalf("got %+v", rep)
	}
	if d := rep.Results[4].Detail; d != "GET http://web:8080/ returned 200, want 200" {
		t.Errorf("http detail = %q", d)
	}
}

func TestProbeFailures(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		wgetWeb: {Out: "  HTTP/1.1 503 Service Unavailable\nwget: server returned error", Err: errors.New("exit 1")},
		ncDB:    {Err: errors.New("exit 1")},
	}}
	probes := []Probe{
		{Service: "web", HTTP: &HTTPProbe{Port: 8080, Path: "/"}},
		{Service: "db", TCP: &TCPProbe{Port: 3306}},
		{Service: "ghost", TCP: &TCPProbe{Port: 1}},
		{Service: "web", HTTP: &HTTPProbe{Port: 8080, Path: "/a b"}},
		{Service: "web"},
	}
	rep := Verify(context.Background(), f, snapshot(webHealthy, dbRunning), probes)
	if rep.Passed || rep.Reached != "L2" {
		t.Fatalf("got passed=%v reached=%q", rep.Passed, rep.Reached)
	}
	want := []string{
		"GET http://web:8080/ returned 503, want 200",
		"TCP db:3306 refused or timed out",
		"unknown service",
		`invalid path "/a b"`,
		"probe needs exactly one of http or tcp",
	}
	for i, w := range want {
		if r := rep.Results[4+i]; r.OK || r.Detail != w {
			t.Errorf("probe %d = %+v, want failed %q", i, r, w)
		}
	}
}

func TestCompletedInitContainerPassesL1(t *testing.T) {
	done := signals.Service{Service: "mysql-init", Role: "init", State: "exited", ExitCode: 0}
	rep := Verify(context.Background(), &dockercli.Fake{}, snapshot(dbRunning, done), nil)
	if !rep.Passed || rep.Reached != "L2" {
		t.Fatalf("got %+v", rep)
	}
	if got := rep.Results[1]; !got.OK || got.Detail != "init completed" {
		t.Errorf("init L1 = %+v", got)
	}
}

func TestFailedOrRunningInitContainerFailsL1(t *testing.T) {
	for _, tc := range []struct {
		s    signals.Service
		want string
	}{
		{signals.Service{Service: "mysql-init", Role: "init", State: "exited", ExitCode: 1,
			Tail: []string{"ERROR 1064 at line 1"}}, "init failed, exit 1: ERROR 1064 at line 1"},
		{signals.Service{Service: "mysql-init", Role: "init", State: "running"}, "init still running"},
	} {
		rep := Verify(context.Background(), &dockercli.Fake{}, snapshot(dbRunning, tc.s), nil)
		if rep.Passed || rep.Results[1].OK || rep.Results[1].Detail != tc.want {
			t.Errorf("got %+v, want detail %q", rep.Results[1], tc.want)
		}
	}
}
