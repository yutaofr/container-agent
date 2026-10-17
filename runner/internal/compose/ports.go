package compose

import (
	"net"
	"strconv"
	"strings"

	"github.com/compose-spec/compose-go/v2/types"
)

// PortConflict is a published host port that cannot be bound.
type PortConflict struct {
	Service string `json:"service"`
	Port    int    `json:"port"`
	Reason  string `json:"reason"` // "in-use" or "duplicate"
}

// HostPortConflicts lists published TCP host ports that are taken on the host
// or published twice within the project. free reports whether a port can be bound.
func HostPortConflicts(p *types.Project, free func(port int) bool) []PortConflict {
	out := []PortConflict{}
	seen := map[int]string{}
	for _, name := range serviceNames(p) {
		for _, pc := range p.Services[name].Ports {
			if pc.Protocol != "" && pc.Protocol != "tcp" {
				continue
			}
			for _, port := range expandPorts(pc.Published) {
				if owner, dup := seen[port]; dup && owner != name {
					out = append(out, PortConflict{name, port, "duplicate"})
					continue
				}
				seen[port] = name
				if !free(port) {
					out = append(out, PortConflict{name, port, "in-use"})
				}
			}
		}
	}
	return out
}

// TCPPortFree reports whether a TCP port can be bound on all interfaces.
func TCPPortFree(port int) bool {
	l, err := net.Listen("tcp", ":"+strconv.Itoa(port))
	if err != nil {
		return false
	}
	_ = l.Close()
	return true
}

// expandPorts turns "8080" or "8000-8002" into ports. Empty or invalid input yields none.
func expandPorts(published string) []int {
	if published == "" {
		return nil
	}
	lo, hi, isRange := strings.Cut(published, "-")
	start, err := strconv.Atoi(lo)
	if err != nil {
		return nil
	}
	end := start
	if isRange {
		if end, err = strconv.Atoi(hi); err != nil || end < start {
			return nil
		}
	}
	ports := make([]int, 0, end-start+1)
	for p := start; p <= end; p++ {
		ports = append(ports, p)
	}
	return ports
}
