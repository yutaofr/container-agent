package compose

import (
	"context"
	"path/filepath"
	"reflect"
	"testing"
)

// checkFields runs the full field check the way validate does: specific rules first,
// then unreviewed keys that no specific finding already covers.
func checkFields(t *testing.T, body string) []Finding {
	t.Helper()
	path := writeCompose(t, body)
	ctx := context.Background()
	p, err := Load(ctx, path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	f, err := LoadFields(ctx, path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	return UnreviewedFields(f, Lint(p, []string{filepath.Dir(path)}, nil))
}

func TestUnreviewedFieldsAllowsReviewedKeys(t *testing.T) {
	got := checkFields(t, `
x-defaults: &defaults
  restart: "no"
services:
  web:
    <<: *defaults
    image: nginx:1.27
    environment: { A: "1" }
    ports: ["8080:80"]
    healthcheck: { test: ["CMD", "true"] }
    mem_limit: 256m
    x-note: extensions are fine
`)
	if len(got) != 0 {
		t.Errorf("reviewed keys reported: %v", got)
	}
}

func TestUnreviewedFieldsReportsOtherKeys(t *testing.T) {
	got := checkFields(t, `
services:
  web:
    image: nginx:1.27
    uts: host
    sysctls: { net.core.somaxconn: 1024 }
configs:
  app: { content: "x" }
`)
	want := []Finding{
		{TopLevel, "field:configs", "configs is not on the reviewed field list"},
		{"web", "field:sysctls", "sysctls is not on the reviewed field list"},
		{"web", "field:uts", "uts is not on the reviewed field list"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("UnreviewedFields() =\n%v\nwant\n%v", got, want)
	}
}

func TestUnreviewedFieldsSeesThroughExtends(t *testing.T) {
	got := checkFields(t, `
services:
  base:
    image: busybox
    runtime: runc
  child:
    extends: { service: base }
`)
	want := []Finding{
		{"base", "field:runtime", "runtime is not on the reviewed field list"},
		{"child", "field:runtime", "runtime is not on the reviewed field list"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("UnreviewedFields() =\n%v\nwant\n%v", got, want)
	}
}

func TestUnreviewedFieldsSkipsKeysWithSpecificFindings(t *testing.T) {
	// privileged: true fires the specific rule, so no duplicate field finding.
	// privileged: false fires nothing specific, so the key itself still needs review.
	got := checkFields(t, `
services:
  a: { image: nginx:1.27, privileged: true }
  b: { image: nginx:1.27, privileged: false }
`)
	want := []Finding{{"b", "field:privileged", "privileged is not on the reviewed field list"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("UnreviewedFields() =\n%v\nwant\n%v", got, want)
	}
}

func TestWithoutApproved(t *testing.T) {
	in := []Finding{
		{"web", "field:uts", ""},
		{"web", RulePrivileged, ""},
		{TopLevel, "field:configs", ""},
	}
	allow := map[string]bool{"web:field:uts": true, "*:field:configs": true}
	want := []Finding{{"web", RulePrivileged, ""}}
	if got := WithoutApproved(in, allow); !reflect.DeepEqual(got, want) {
		t.Errorf("WithoutApproved() = %v, want %v", got, want)
	}
}
