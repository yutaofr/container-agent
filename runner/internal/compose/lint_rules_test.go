package compose

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// lintYAML lints a compose document with the compose file's directory as the only root.
func lintYAML(t *testing.T, body string, allow map[string]bool) []Finding {
	t.Helper()
	path := writeCompose(t, body)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	return Lint(p, []string{filepath.Dir(path)}, allow)
}

func TestLintServiceRules(t *testing.T) {
	cases := []struct {
		name string
		yaml string // service "web" body, indented under it
		want []Finding
	}{
		{"pid host", "pid: host", []Finding{{"web", RulePidHost, "pid: host"}}},
		{"ipc host", "ipc: host", []Finding{{"web", RuleIpcHost, "ipc: host"}}},
		{"userns host", "userns_mode: host", []Finding{{"web", RuleUserNSHost, "userns_mode: host"}}},
		{"devices", "devices: [\"/dev/fuse:/dev/fuse\", \"/dev/kvm\"]",
			[]Finding{{"web", RuleDevices, "/dev/fuse,/dev/kvm"}}},
		{"cgroup parent", "cgroup_parent: /custom", []Finding{{"web", RuleCgroupParent, "/custom"}}},

		{"seccomp unconfined", "security_opt: [seccomp=unconfined]",
			[]Finding{{"web", RuleSecurityOpt, "seccomp=unconfined"}}},
		{"apparmor unconfined, colon form", "security_opt: [\"apparmor:unconfined\"]",
			[]Finding{{"web", RuleSecurityOpt, "apparmor:unconfined"}}},
		{"label disable, equals form", "security_opt: [label=disable]",
			[]Finding{{"web", RuleSecurityOpt, "label=disable"}}},
		{"label disable, colon form", "security_opt: [\"label:disable\"]",
			[]Finding{{"web", RuleSecurityOpt, "label:disable"}}},
		{"no-new-privileges false", "security_opt: [\"no-new-privileges:false\"]",
			[]Finding{{"web", RuleSecurityOpt, "no-new-privileges:false"}}},
		{"several risky options, one finding", "security_opt: [seccomp=unconfined, \"label:disable\", \"no-new-privileges:true\"]",
			[]Finding{{"web", RuleSecurityOpt, "seccomp=unconfined,label:disable"}}},

		// Settings that look similar but are not escape vectors.
		{"pid of another service", "pid: \"service:db\"", []Finding{}},
		{"safe security options", "security_opt: [\"no-new-privileges:true\", \"seccomp=profile.json\"]", []Finding{}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := lintYAML(t, "services:\n  db: { image: mysql:8.0 }\n  web:\n    image: nginx:1.27\n    "+c.yaml+"\n", nil)
			if !reflect.DeepEqual(got, c.want) {
				t.Errorf("Lint() = %v, want %v", got, c.want)
			}
		})
	}
}

func TestLintServiceRuleOrderAndApproval(t *testing.T) {
	const body = `
services:
  web:
    image: nginx:1.27
    privileged: true
    network_mode: host
    pid: host
    ipc: host
    userns_mode: host
    cap_add: [SYS_ADMIN]
    devices: ["/dev/fuse"]
    security_opt: [seccomp=unconfined]
    cgroup_parent: /custom
`
	got := lintYAML(t, body, nil)
	var rules []string
	for _, f := range got {
		rules = append(rules, f.Rule)
	}
	want := []string{RulePrivileged, RuleHostNetwork, RulePidHost, RuleIpcHost, RuleUserNSHost,
		RuleCapAdd, RuleDevices, RuleSecurityOpt, RuleCgroupParent}
	if !reflect.DeepEqual(rules, want) {
		t.Errorf("rule order = %v, want %v", rules, want)
	}

	allow := map[string]bool{}
	for _, r := range want {
		allow["web:"+r] = true
	}
	if got := lintYAML(t, body, allow); len(got) != 0 {
		t.Errorf("Lint() with all approvals = %v, want none", got)
	}
}

func TestLintTopLevelVolumes(t *testing.T) {
	outside := t.TempDir()
	runDir := filepath.Join(outside, "run")
	if err := os.Mkdir(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	unrelated := t.TempDir() // a host dir outside the workspace that holds no socket
	old := dockerSocketPaths
	dockerSocketPaths = []string{filepath.Join(runDir, "docker.sock")}
	t.Cleanup(func() { dockerSocketPaths = old })

	cases := []struct {
		name string
		vol  string // body of volume "data"
		want []Finding
	}{
		{"non-local driver", "driver: rexray/s3fs", []Finding{{"data", RuleVolumeDriver, "rexray/s3fs"}}},
		{"explicit local driver is fine", "driver: local", []Finding{}},
		{"bind to a host dir outside the workspace",
			fmt.Sprintf("driver_opts: { type: none, o: bind, device: %s }", unrelated),
			[]Finding{{"data", RuleHostMount, unrelated}}},
		{"bind to an ancestor of the socket",
			fmt.Sprintf("driver_opts: { type: none, o: bind, device: %s }", outside),
			[]Finding{{"data", RuleDockerSock, outside}}},
		{"bind to the socket directory",
			fmt.Sprintf("driver_opts: { type: none, o: bind, device: %s }", runDir),
			[]Finding{{"data", RuleDockerSock, runDir}}},
		{"bind with extra mount options",
			fmt.Sprintf("driver_opts: { type: none, o: \"rw,bind\", device: %s }", unrelated),
			[]Finding{{"data", RuleHostMount, unrelated}}},
		{"nfs device is not a host path",
			"driver_opts: { type: nfs, o: \"addr=10.0.0.1,rw\", device: \":/export\" }", []Finding{}},
		{"cifs device is not a host path",
			"driver_opts: { type: cifs, o: \"addr=10.0.0.1,rw\", device: \"//10.0.0.1/share\" }", []Finding{}},
		{"tmpfs needs no device approval",
			"driver_opts: { type: tmpfs, device: tmpfs, o: size=100m }", []Finding{}},
		{"host block device",
			"driver_opts: { type: ext4, device: /dev/sda1 }",
			[]Finding{{"data", RuleVolumeDriver, "ext4:/dev/sda1"}}},
		{"device without a filesystem type",
			"driver_opts: { device: /dev/sda1 }",
			[]Finding{{"data", RuleVolumeDriver, ":/dev/sda1"}}},
		{"bind to a path inside the workspace",
			"driver_opts: { type: none, o: bind, device: ./data }", []Finding{}},
		{"external volume is not inspected", "external: true", []Finding{}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := lintYAML(t, "services:\n  web: { image: nginx:1.27 }\nvolumes:\n  data:\n    "+c.vol+"\n", nil)
			if !reflect.DeepEqual(got, c.want) {
				t.Errorf("Lint() = %v, want %v", got, c.want)
			}
		})
	}

	t.Run("approval uses the volume name", func(t *testing.T) {
		got := lintYAML(t, "services:\n  web: { image: nginx:1.27 }\nvolumes:\n  data: { driver: rexray/s3fs }\n",
			map[string]bool{"data:volume-driver": true})
		if len(got) != 0 {
			t.Errorf("Lint() = %v, want none", got)
		}
	})
}
