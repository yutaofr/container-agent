package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/dockercli"
)

const psDemo = "ps -a -q --no-trunc --filter label=com.docker.compose.project=cagent-demo"

// withDocker returns testDeps whose docker answers only the given commands.
func withDocker(t *testing.T, responses map[string]dockercli.FakeResponse) (deps, *dockercli.Fake, string) {
	t.Helper()
	d := testDeps(t)
	f := &dockercli.Fake{Responses: responses}
	d.docker = f
	root, _ := d.home()
	return d, f, root
}

func TestUpStartsCleanCompose(t *testing.T) {
	src := writeFile(t, "services:\n  web: { image: nginx:1.27 }\n")
	d, f, root := withDocker(t, nil)
	written := filepath.Join(root, "clusters", "demo", "compose.yaml")
	f.Responses = map[string]dockercli.FakeResponse{
		"compose -p cagent-demo -f " + written + " up -d --remove-orphans": {},
	}
	var out, errOut bytes.Buffer
	if code := run([]string{"up", "-name", "demo", "-f", src}, &out, &errOut, d); code != exitOK {
		t.Fatalf("exit %d, stderr: %s", code, errOut.String())
	}
	if !strings.Contains(out.String(), `"project": "cagent-demo"`) {
		t.Errorf("output: %s", out.String())
	}
	data, err := os.ReadFile(written)
	if err != nil || !strings.Contains(string(data), `cagent.managed: "true"`) {
		t.Errorf("workspace compose missing or unlabelled: %v\n%s", err, data)
	}
}

func TestUpRefusesFindings(t *testing.T) {
	src := writeFile(t, "services:\n  web: { image: nginx:1.27, privileged: true }\n")
	d, f, _ := withDocker(t, nil)
	var out, errOut bytes.Buffer
	if code := run([]string{"up", "-name", "demo", "-f", src}, &out, &errOut, d); code != exitFindings {
		t.Fatalf("exit %d, want %d", code, exitFindings)
	}
	for _, c := range f.Calls {
		if strings.Contains(c, " up ") {
			t.Errorf("cluster started despite findings: %v", f.Calls)
		}
	}
}

func TestSubcommandsRequireValidName(t *testing.T) {
	for _, args := range [][]string{
		{"up", "-f", "x.yaml"},
		{"up", "-name", "-rf", "-f", "x.yaml"},
		{"snapshot"},
		{"verify", "-name", "Bad"},
		{"down", "-name", "../x"},
	} {
		var out, errOut bytes.Buffer
		if code := run(args, &out, &errOut, testDeps(t)); code != exitError {
			t.Errorf("%v: exit %d, want %d", args, code, exitError)
		}
	}
}

func TestDownRefusesForeignProject(t *testing.T) {
	d, f, _ := withDocker(t, map[string]dockercli.FakeResponse{
		psDemo:        {Out: "aaa\n"},
		"inspect aaa": {Out: `[{"Id":"aaa","Name":"/theirs","Config":{"Labels":{}}}]`},
	})
	var out, errOut bytes.Buffer
	if code := run([]string{"down", "-name", "demo"}, &out, &errOut, d); code != exitError {
		t.Fatalf("exit %d, want %d", code, exitError)
	}
	if !strings.Contains(errOut.String(), "does not manage") {
		t.Errorf("stderr: %s", errOut.String())
	}
	if len(f.Calls) != 2 {
		t.Errorf("unexpected calls: %v", f.Calls)
	}
}

func TestVerifyExitCodes(t *testing.T) {
	cases := []struct {
		state string
		want  int
	}{
		{`{"Status":"running"}`, exitOK},
		{`{"Status":"exited","ExitCode":1}`, exitFindings},
	}
	for _, c := range cases {
		d, _, _ := withDocker(t, map[string]dockercli.FakeResponse{
			psDemo: {Out: "aaa\n"},
			"inspect aaa": {Out: `[{"Id":"aaa","Name":"/x","State":` + c.state +
				`,"Config":{"Labels":{"com.docker.compose.service":"web"}}}]`},
			"logs --tail 200 aaa": {Out: ""},
		})
		var out, errOut bytes.Buffer
		if code := run([]string{"verify", "-name", "demo"}, &out, &errOut, d); code != c.want {
			t.Errorf("state %s: exit %d, want %d; out: %s", c.state, code, c.want, out.String())
		}
	}
}

func TestOwnPublishedPortsAreNotConflicts(t *testing.T) {
	src := writeFile(t, "services:\n  redis: { image: redis:7.4, ports: [\"61283:6379\"] }\n")
	for _, c := range []struct {
		labels string
		want   int
	}{
		{`{"cagent.managed":"true"}`, exitOK}, // the cluster's own running redis
		{`{}`, exitFindings},                  // someone else's container claiming the name
	} {
		d, _, _ := withDocker(t, map[string]dockercli.FakeResponse{
			psDemo: {Out: "aaa\n"},
			"inspect aaa": {Out: `[{"Id":"aaa","Config":{"Labels":` + c.labels + `},
				"NetworkSettings":{"Ports":{"6379/tcp":[{"HostIp":"0.0.0.0","HostPort":"61283"}]}}}]`},
		})
		d.free = func(port int) bool { return port != 61283 } // the port is held on the host
		var out, errOut bytes.Buffer
		if code := run([]string{"validate", "-name", "demo", "-f", src}, &out, &errOut, d); code != c.want {
			t.Errorf("labels %s: exit %d, want %d; out %s", c.labels, code, c.want, out.String())
		}
	}
}
