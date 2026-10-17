package compose

import (
	"context"
	"strings"
	"testing"
)

func TestApplyManagedLabels(t *testing.T) {
	path := writeCompose(t, `
services:
  web: { image: nginx:1.27, labels: { team: core } }
  db:  { image: mysql:8.0 }
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	ApplyManagedLabels(p, "orders-poc")

	for name, s := range p.Services {
		if !IsManaged(s.Labels) {
			t.Errorf("%s: not managed: %v", name, s.Labels)
		}
		if s.Labels[LabelCluster] != "orders-poc" {
			t.Errorf("%s: cluster label = %q", name, s.Labels[LabelCluster])
		}
	}
	if p.Services["web"].Labels["team"] != "core" {
		t.Error("existing label was dropped")
	}

	out, err := p.MarshalYAML()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), "cagent.managed: \"true\"") {
		t.Errorf("marshalled YAML lacks managed label:\n%s", out)
	}
}

func TestIsManaged(t *testing.T) {
	if IsManaged(map[string]string{"other": "x"}) {
		t.Error("unlabelled container reported as managed")
	}
}
