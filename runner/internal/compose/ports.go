package compose

import (
	"context"
	"net"
	"strconv"
	"strings"
	"syscall"

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

// TCPPortFree reports whether Docker could publish a TCP port: it must bind on the IPv4
// wildcard, IPv4 loopback, and (where the host has IPv6) the IPv6 wildcard. Each probe
// clears SO_REUSEADDR, which Go sets by default: with it, BSD-derived kernels such as
// macOS let a wildcard bind succeed while another program holds the port on a specific
// address or the other address family, and the check would miss a real conflict.
func TCPPortFree(port int) bool {
	p := strconv.Itoa(port)
	for _, probe := range []struct{ network, addr string }{
		{"tcp4", "0.0.0.0:" + p},
		{"tcp4", "127.0.0.1:" + p},
		{"tcp6", "[::]:" + p},
	} {
		l, err := noReuseListen.Listen(context.Background(), probe.network, probe.addr)
		if err != nil {
			if probe.network == "tcp6" && !ipv6Available() {
				continue
			}
			return false
		}
		_ = l.Close()
	}
	return true
}

var noReuseListen = net.ListenConfig{
	Control: func(_, _ string, c syscall.RawConn) error {
		var serr error
		if err := c.Control(func(fd uintptr) {
			serr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 0)
		}); err != nil {
			return err
		}
		return serr
	},
}

// ipv6Available reports whether the host can bind IPv6 at all.
func ipv6Available() bool {
	l, err := net.Listen("tcp6", "[::1]:0")
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
