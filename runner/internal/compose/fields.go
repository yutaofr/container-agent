package compose

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/compose-spec/compose-go/v2/cli"
)

// RuleFieldPrefix starts the rule ID of a key outside the reviewed list, e.g. "field:sysctls".
// Users approve one key at a time as "service:field:sysctls".
const RuleFieldPrefix = "field:"

// TopLevel is the Finding.Service value for a top-level key, so approvals read "*:field:include".
const TopLevel = "*"

// allowedFieldsJSON is the reviewed key list. The platform's generator tests read the same
// file, so the generator can never emit a key that validate would reject.
//
//go:embed allowed_fields.json
var allowedFieldsJSON []byte

type allowedFields struct {
	TopLevel []string `json:"top_level"`
	Service  []string `json:"service"`
}

var allowedTop, allowedService = mustParseAllowed(allowedFieldsJSON)

func mustParseAllowed(data []byte) (top, service map[string]bool) {
	var a allowedFields
	if err := json.Unmarshal(data, &a); err != nil {
		panic(fmt.Sprintf("allowed_fields.json: %v", err))
	}
	return toSet(a.TopLevel), toSet(a.Service)
}

func toSet(keys []string) map[string]bool {
	s := make(map[string]bool, len(keys))
	for _, k := range keys {
		s[k] = true
	}
	return s
}

// Fields holds the keys in effect in a compose file, sorted, after YAML anchors, extends,
// and includes are applied. Checking effective keys stops a risky key hiding behind extends.
type Fields struct {
	TopLevel []string
	Services map[string][]string
}

// LoadFields returns the effective keys of a compose file.
func LoadFields(ctx context.Context, path, name string) (Fields, error) {
	opts, err := cli.NewProjectOptions([]string{path},
		cli.WithName(name),
		cli.WithResolvedPaths(true),
	)
	if err != nil {
		return Fields{}, fmt.Errorf("compose options: %w", err)
	}
	model, err := opts.LoadModel(ctx)
	if err != nil {
		return Fields{}, fmt.Errorf("load %s: %w", path, err)
	}
	f := Fields{TopLevel: sortedKeys(model), Services: map[string][]string{}}
	services, _ := model["services"].(map[string]any)
	for name, s := range services {
		svc, _ := s.(map[string]any)
		f.Services[name] = sortedKeys(svc)
	}
	return f, nil
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// specificRuleField maps rules that inspect a single service key to that key.
var specificRuleField = map[string]string{
	RulePrivileged:   "privileged",
	RuleHostNetwork:  "network_mode",
	RulePidHost:      "pid",
	RuleIpcHost:      "ipc",
	RuleUserNSHost:   "userns_mode",
	RuleCapAdd:       "cap_add",
	RuleDevices:      "devices",
	RuleSecurityOpt:  "security_opt",
	RuleCgroupParent: "cgroup_parent",
}

// UnreviewedFields reports keys outside the reviewed list. specific must be the unfiltered
// Lint findings: a key that already has a specific finding is skipped, so one setting needs
// one approval. Keys starting with "x-" are compose extensions and always allowed.
func UnreviewedFields(f Fields, specific []Finding) []Finding {
	covered := map[string]bool{}
	for _, s := range specific {
		if key, ok := specificRuleField[s.Rule]; ok {
			covered[s.Service+"/"+key] = true
		}
	}
	out := []Finding{}
	for _, k := range f.TopLevel {
		if !isExtension(k) && !allowedTop[k] {
			out = append(out, unreviewed(TopLevel, k))
		}
	}
	names := make([]string, 0, len(f.Services))
	for n := range f.Services {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, svc := range names {
		for _, k := range f.Services[svc] {
			if isExtension(k) || allowedService[k] || covered[svc+"/"+k] {
				continue
			}
			out = append(out, unreviewed(svc, k))
		}
	}
	return out
}

func isExtension(key string) bool { return strings.HasPrefix(key, "x-") }

func unreviewed(svc, key string) Finding {
	return Finding{Service: svc, Rule: RuleFieldPrefix + key, Detail: key + " is not on the reviewed field list"}
}

// WithoutApproved drops findings the user approved as "service:rule".
func WithoutApproved(findings []Finding, allow map[string]bool) []Finding {
	out := []Finding{}
	for _, f := range findings {
		if !allow[f.Service+":"+f.Rule] {
			out = append(out, f)
		}
	}
	return out
}
