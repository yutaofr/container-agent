package compose

import "github.com/compose-spec/compose-go/v2/types"

// Labels the Runner puts on every service it starts. Docker operations only
// ever target containers that carry LabelManaged.
const (
	LabelManaged = "cagent.managed"
	LabelCluster = "cagent.cluster"
)

// ApplyManagedLabels marks every service in p as belonging to cluster.
func ApplyManagedLabels(p *types.Project, cluster string) {
	for name, s := range p.Services {
		if s.Labels == nil {
			s.Labels = types.Labels{}
		}
		s.Labels[LabelManaged] = "true"
		s.Labels[LabelCluster] = cluster
		p.Services[name] = s
	}
}

// IsManaged reports whether a container's labels mark it as Runner-managed.
func IsManaged(labels map[string]string) bool {
	return labels[LabelManaged] == "true"
}
