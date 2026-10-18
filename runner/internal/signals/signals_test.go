package signals

import (
	"context"
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
)

func container(t *testing.T, js string) dockercli.Container {
	t.Helper()
	var c dockercli.Container
	if err := json.Unmarshal([]byte(js), &c); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestFromContainerCrashed(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/cagent-demo-api-1",
		"State":{"Status":"exited","ExitCode":3},
		"Config":{"Labels":{"com.docker.compose.service":"api"}}}`)
	logs := "Starting api\n" +
		"connecting with password=hunter2\n" +
		"java.net.ConnectException: Connection refused\n" +
		"\tat com.acme.Db.open(Db.java:42)\n"
	s := FromContainer(c, logs, DefaultOptions)

	if s.Service != "api" || s.Container != "cagent-demo-api-1" || s.State != "exited" || s.ExitCode != 3 {
		t.Errorf("state wrong: %+v", s)
	}
	want := []string{"conn:refused@api", "exit:3@api"}
	if !reflect.DeepEqual(s.Signatures, want) {
		t.Errorf("signatures = %v, want %v", s.Signatures, want)
	}
	if !reflect.DeepEqual(s.ErrorLines, []string{"java.net.ConnectException: Connection refused"}) {
		t.Errorf("error lines = %v", s.ErrorLines)
	}
	joined := strings.Join(s.Tail, "\n")
	if strings.Contains(joined, "hunter2") || !strings.Contains(joined, "password=******") {
		t.Errorf("tail not redacted: %v", s.Tail)
	}
}

func TestFromContainerOOMKilled(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/x","State":{"Status":"exited","OOMKilled":true,"ExitCode":137},
		"Config":{"Labels":{"com.docker.compose.service":"worker"}}}`)
	s := FromContainer(c, "", DefaultOptions)
	if !s.OOMKilled || !reflect.DeepEqual(s.Signatures, []string{"oom:killed@worker"}) {
		t.Errorf("got %+v", s)
	}
}

func TestFromContainerHealthyRunning(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/x","RestartCount":1,
		"State":{"Status":"running","Health":{"Status":"healthy","Log":[{"ExitCode":0,"Output":"ok token=abc\n"}]}},
		"Config":{"Labels":{"com.docker.compose.service":"web"}}}`)
	s := FromContainer(c, "GET / 200\n", DefaultOptions)
	if s.Health != "healthy" || s.HealthLog != "ok token=******" || s.RestartCount != 1 {
		t.Errorf("got %+v", s)
	}
	if len(s.Signatures) != 0 || len(s.ErrorLines) != 0 {
		t.Errorf("healthy service has evidence: %+v", s)
	}
}

func TestFromContainerBoundsEvidence(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/x","State":{"Status":"running"},
		"Config":{"Labels":{"com.docker.compose.service":"noisy"}}}`)
	var b strings.Builder
	for i := 0; i < 50; i++ {
		b.WriteString("ERROR retrying\n")
	}
	b.WriteString("last line\n")
	s := FromContainer(c, b.String(), Options{TailLines: 200, MaxErrorLines: 3, KeepTail: 2})
	if len(s.ErrorLines) != 3 {
		t.Errorf("kept %d error lines, want 3", len(s.ErrorLines))
	}
	if !reflect.DeepEqual(s.Tail, []string{"ERROR retrying", "last line"}) {
		t.Errorf("tail = %v", s.Tail)
	}
}

func TestCollectSortsServices(t *testing.T) {
	f := &dockercli.Fake{Responses: map[string]dockercli.FakeResponse{
		"ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-demo": {Out: "b\na\n"},
		"inspect b a": {Out: `[
			{"Id":"b","Name":"/web","State":{"Status":"running"},"Config":{"Labels":{"com.docker.compose.service":"web"}}},
			{"Id":"a","Name":"/api","State":{"Status":"running"},"Config":{"Labels":{"com.docker.compose.service":"api"}}}]`},
		"logs --tail 200 b": {Out: "up\n"},
		"logs --tail 200 a": {Out: "up\n"},
	}}
	snap, err := Collect(context.Background(), f, "cagent-demo", DefaultOptions)
	if err != nil {
		t.Fatal(err)
	}
	if snap.Project != "cagent-demo" || len(snap.Services) != 2 || snap.Services[0].Service != "api" {
		t.Errorf("got %+v", snap)
	}
}

func TestCompletedInitContainerIsNoProblem(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/cagent-demo-mysql-init-1",
		"State":{"Status":"exited","ExitCode":0},
		"Config":{"Labels":{"com.docker.compose.service":"mysql-init","cagent.role":"init"}}}`)
	s := FromContainer(c, "run 01-schema.sql\nskip 02-seed.sql: error table already there\n", DefaultOptions)
	if s.Role != "init" || len(s.Signatures) != 0 {
		t.Errorf("role %q, signatures %v", s.Role, s.Signatures)
	}
}

func TestFailedInitContainerKeepsItsEvidence(t *testing.T) {
	c := container(t, `{"Id":"a","Name":"/cagent-demo-mysql-init-1",
		"State":{"Status":"exited","ExitCode":1},
		"Config":{"Labels":{"com.docker.compose.service":"mysql-init","cagent.role":"init"}}}`)
	s := FromContainer(c, "ERROR 1064 (42000) at line 1: You have an error in your SQL syntax\n", DefaultOptions)
	if !slices.Contains(s.Signatures, "exit:1@mysql-init") {
		t.Errorf("signatures %v", s.Signatures)
	}
	if len(s.ErrorLines) != 1 {
		t.Errorf("error lines %v", s.ErrorLines)
	}
}
