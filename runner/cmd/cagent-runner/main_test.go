package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func allFree(int) bool { return true }

func writeFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "compose.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestValidateClean(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, ports: [\"8080:80\"] }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitOK {
		t.Fatalf("exit %d, stderr: %s", code, errOut.String())
	}
	var rep Report
	if err := json.Unmarshal(out.Bytes(), &rep); err != nil {
		t.Fatalf("bad JSON %q: %v", out.String(), err)
	}
	if rep.Project != "validate" || len(rep.Findings) != 0 || len(rep.PortConflicts) != 0 {
		t.Errorf("unexpected report %+v", rep)
	}
	// Consumers expect empty arrays, never null.
	for _, field := range []string{`"findings": []`, `"port_conflicts": []`} {
		if !bytes.Contains(out.Bytes(), []byte(field)) {
			t.Errorf("report lacks %s:\n%s", field, out.String())
		}
	}
}

func TestValidateReportsFindings(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, privileged: true }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitFindings {
		t.Fatalf("exit %d, want %d", code, exitFindings)
	}
	if !bytes.Contains(out.Bytes(), []byte(`"rule": "privileged"`)) {
		t.Errorf("report lacks privileged finding:\n%s", out.String())
	}
}

func TestValidateAllowFlag(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, privileged: true }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path, "-allow", "web:privileged"}, &out, &errOut, allFree); code != exitOK {
		t.Fatalf("exit %d, want %d; out: %s", code, exitOK, out.String())
	}
}

func TestValidateInvalidFile(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx, bogus_key: 1 }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitError {
		t.Fatalf("exit %d, want %d", code, exitError)
	}
	if errOut.Len() == 0 {
		t.Error("no error message on stderr")
	}
}

func TestValidateReportsUnreviewedField(t *testing.T) {
	path := writeFile(t, "services:\n  web: { image: nginx:1.27, uts: host }\n")
	var out, errOut bytes.Buffer
	if code := run([]string{"validate", "-f", path}, &out, &errOut, allFree); code != exitFindings {
		t.Fatalf("exit %d, want %d; out: %s", code, exitFindings, out.String())
	}
	if !bytes.Contains(out.Bytes(), []byte(`"rule": "field:uts"`)) {
		t.Errorf("report lacks field:uts finding:\n%s", out.String())
	}
	out.Reset()
	if code := run([]string{"validate", "-f", path, "-allow", "web:field:uts"}, &out, &errOut, allFree); code != exitOK {
		t.Fatalf("approved field: exit %d, want %d; out: %s", code, exitOK, out.String())
	}
}
