package dockercli

import (
	"context"
	"encoding/json"
	"testing"
)

// inspectJSON is trimmed real `docker inspect` output for one exited and one healthy container.
const inspectJSON = `[
 {"Id":"aaa","Name":"/cagent-demo-crash-1","RestartCount":0,
  "State":{"Status":"exited","Restarting":false,"OOMKilled":false,"ExitCode":3},
  "Config":{"Labels":{"com.docker.compose.service":"crash","cagent.managed":"true"}}},
 {"Id":"bbb","Name":"/cagent-demo-web-1","RestartCount":2,
  "State":{"Status":"running","Restarting":false,"OOMKilled":false,"ExitCode":0,
           "Health":{"Status":"healthy","Log":[{"ExitCode":0,"Output":"ok"}]}},
  "Config":{"Labels":{"com.docker.compose.service":"web","cagent.managed":"true"}}}
]`

func TestProjectContainers(t *testing.T) {
	f := &Fake{Responses: map[string]FakeResponse{
		"ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-demo": {Out: "aaa\nbbb\n"},
		"inspect aaa bbb": {Out: inspectJSON},
	}}
	cs, err := ProjectContainers(context.Background(), f, "cagent-demo")
	if err != nil {
		t.Fatal(err)
	}
	if len(cs) != 2 {
		t.Fatalf("got %d containers, want 2", len(cs))
	}
	crash, web := cs[0], cs[1]
	if crash.Service() != "crash" || crash.State.Status != "exited" || crash.State.ExitCode != 3 {
		t.Errorf("crash parsed wrong: %+v", crash)
	}
	if crash.State.Health != nil {
		t.Error("container without healthcheck has Health")
	}
	if web.RestartCount != 2 || web.State.Health == nil || web.State.Health.Status != "healthy" {
		t.Errorf("web parsed wrong: %+v", web)
	}
}

func TestProjectContainersEmpty(t *testing.T) {
	f := &Fake{Responses: map[string]FakeResponse{
		"ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-none": {Out: ""},
	}}
	cs, err := ProjectContainers(context.Background(), f, "cagent-none")
	if err != nil || cs != nil {
		t.Errorf("got %v, %v; want nil, nil", cs, err)
	}
	if len(f.Calls) != 1 {
		t.Errorf("inspect should not run for an empty project; calls: %v", f.Calls)
	}
}

func TestPublishedPorts(t *testing.T) {
	var cs []Container
	if err := json.Unmarshal([]byte(`[{"NetworkSettings":{"Ports":{
		"6379/tcp":[{"HostIp":"0.0.0.0","HostPort":"61283"},{"HostIp":"::","HostPort":"61283"}],
		"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"18080"}],
		"9090/tcp":null}}}]`), &cs); err != nil {
		t.Fatal(err)
	}
	if got := cs[0].PublishedPorts(); len(got) != 2 || got[0] != 18080 || got[1] != 61283 {
		t.Errorf("PublishedPorts() = %v, want [18080 61283]", got)
	}
}
