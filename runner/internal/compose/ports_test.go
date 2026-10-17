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
	l, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	if TCPPortFree(l.Addr().(*net.TCPAddr).Port) {
		t.Error("port held by a listener reported free")
	}
}
