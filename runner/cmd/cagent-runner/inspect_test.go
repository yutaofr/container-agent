package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/yutaofr/container-agent/runner/internal/inspect"
)

func TestInspectPrintsWhatItRead(t *testing.T) {
	proj := project(t, map[string]string{"pom.xml": "<project/>", "README.md": "x"})
	var out, errOut bytes.Buffer
	if code := run([]string{"inspect", "-workdir", proj}, &out, &errOut, testDeps(t)); code != exitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var res inspect.Result
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatal(err)
	}
	if len(res.Files) != 1 || res.Files["pom.xml"] != "<project/>" {
		t.Errorf("files %v", res.Files)
	}
}

func TestInspectArgumentErrors(t *testing.T) {
	for _, args := range [][]string{{"inspect"}, {"inspect", "-workdir", "/does/not/exist"}} {
		var out, errOut bytes.Buffer
		if code := run(args, &out, &errOut, testDeps(t)); code != exitError {
			t.Errorf("%v: exit %d, want %d", args, code, exitError)
		}
	}
}

// Over RPC the platform may inspect only directories under a -workdir-root the user gave.
func TestServeInspectStaysInsideWorkdirRoots(t *testing.T) {
	proj := project(t, map[string]string{"pom.xml": "<project/>"})
	c, _ := startServe(t, testDeps(t), func(cfg *serveConfig) { cfg.workdirRoots = []string{proj} })

	resp := call(t, c, 1, "inspect", map[string]any{"workdir": proj})
	if resp.Error != nil || resp.Result == nil || resp.Result.ExitCode != exitOK {
		t.Fatalf("inspect: %+v", resp)
	}
	if !strings.Contains(string(resp.Result.Output), `"pom.xml"`) {
		t.Errorf("output %s", resp.Result.Output)
	}
	for i, params := range []map[string]any{
		{"workdir": "/etc"},
		{"workdir": proj + "/.."},
		{"workdir": proj, "file": "/etc/passwd"},
		{},
	} {
		resp := call(t, c, i+2, "inspect", params)
		if resp.Error == nil || resp.Error.Code != rpcInvalidParams {
			t.Errorf("%v: got %+v, want invalid params", params, resp)
		}
	}
}
