package compose

import (
	"context"
	"net"
	"reflect"
	"testing"
)

func TestHostPortConflicts(t *testing.T) {
	path := writeCompose(t, `
services:
  a: { image: nginx, ports: ["8080:80", "9000-9001:9000-9001"] }
  b: { image: nginx, ports: ["8080:80"] }
  c: { image: nginx, ports: ["5353:53/udp", "80"] }
`)
	p, err := Load(context.Background(), path, "demo")
	if err != nil {
		t.Fatal(err)
	}
	inUse := map[int]bool{9001: true}
	got := HostPortConflicts(p, func(port int) bool { return !inUse[port] })
	want := []PortConflict{
		{"a", 9001, "in-use"},
		{"b", 8080, "duplicate"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("HostPortConflicts() = %v, want %v", got, want)
	}
}

func TestTCPPortFree(t *testing.T) {
	// Another program may hold a port on any one address; Docker publishing on 0.0.0.0
	// would then fail, so every holder must make the port count as taken.
	for _, holder := range []struct{ network, addr string }{
		{"tcp4", "0.0.0.0:0"},
		{"tcp4", "127.0.0.1:0"},
		{"tcp6", "[::]:0"},
	} {
		l, err := net.Listen(holder.network, holder.addr)
		if err != nil {
			t.Logf("skip %s: %v", holder.addr, err) // e.g. no IPv6 in this environment
			continue
		}
		port := l.Addr().(*net.TCPAddr).Port
		if TCPPortFree(port) {
			t.Errorf("port %d held on %s reported free", port, holder.addr)
		}
		l.Close()
	}
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	if !TCPPortFree(port) {
		t.Errorf("released port %d reported taken", port)
	}
}
