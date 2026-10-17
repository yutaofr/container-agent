package fingerprint

import (
	"strings"
	"testing"
)

func TestFromLogKnownPatterns(t *testing.T) {
	cases := []struct{ line, kind string }{
		{"bind: Address already in use", "bind:address_in_use"},
		{"java.net.ConnectException: Connection refused (Connection refused)", "conn:refused"},
		{"java.net.UnknownHostException: mysql", "dns:unresolved"},
		{"java.lang.OutOfMemoryError: Java heap space", "oom:app"},
		{"Access denied for user 'root'@'172.18.0.3'", "auth:denied"},
		{"exec /app/server: exec format error", "image:arch_mismatch"},
		{"open /config/app.yml: no such file or directory", "fs:not_found"},
	}
	for _, c := range cases {
		if got := FromLog("app", c.line); got.Kind != c.kind {
			t.Errorf("FromLog(%q).Kind = %q, want %q", c.line, got.Kind, c.kind)
		}
	}
}

func TestFromLogUnknownIsStableAcrossRuns(t *testing.T) {
	a := FromLog("app", "2026-10-17T08:01:02.123Z ERROR worker 42 failed at 10.0.0.7:9092 id=3f2a9c1b0d4e5f60")
	b := FromLog("app", "2026-10-18 09:11:12,456 ERROR worker 7 failed at 10.0.0.9:9093 id=aa2a9c1b0d4e5f61")
	if a != b {
		t.Errorf("same failure, different signatures: %v vs %v", a, b)
	}
	if !strings.HasPrefix(a.Kind, "unknown:") || len(a.Kind) != len("unknown:")+12 {
		t.Errorf("unexpected unknown kind %q", a.Kind)
	}
	if c := FromLog("app", "ERROR something else entirely"); c == a {
		t.Error("different failures share a signature")
	}
}

func TestFromExit(t *testing.T) {
	cases := []struct {
		code int
		oom  bool
		want string
	}{
		{137, false, "oom:killed@app"},
		{1, true, "oom:killed@app"},
		{1, false, "exit:1@app"},
		{0, false, "exit:0@app"},
	}
	for _, c := range cases {
		if got := FromExit("app", c.code, c.oom).String(); got != c.want {
			t.Errorf("FromExit(%d, %v) = %q, want %q", c.code, c.oom, got, c.want)
		}
	}
}
