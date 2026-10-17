// Package fingerprint turns container failures into stable error signatures,
// so the self-healing loop can count attempts per problem and spot repeats.
package fingerprint

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
)

// Signature identifies one kind of failure in one service.
type Signature struct {
	Kind    string `json:"kind"`    // e.g. "bind:address_in_use", "unknown:3f2a9c1b0d4e"
	Service string `json:"service"` // compose service name
}

// String renders the signature as kind@service.
func (s Signature) String() string { return s.Kind + "@" + s.Service }

// classifiers map well-known log patterns to kinds. First match wins.
var classifiers = []struct {
	re   *regexp.Regexp
	kind string
}{
	{regexp.MustCompile(`(?i)address already in use`), "bind:address_in_use"},
	{regexp.MustCompile(`(?i)connection refused`), "conn:refused"},
	{regexp.MustCompile(`(?i)unknownhostexception|no such host|name or service not known|could not resolve`), "dns:unresolved"},
	{regexp.MustCompile(`(?i)outofmemoryerror|out of memory`), "oom:app"},
	{regexp.MustCompile(`(?i)access denied for user|authentication failed`), "auth:denied"},
	{regexp.MustCompile(`(?i)exec format error`), "image:arch_mismatch"},
	{regexp.MustCompile(`(?i)permission denied`), "fs:permission_denied"},
	{regexp.MustCompile(`(?i)no such file or directory`), "fs:not_found"},
}

// FromLog fingerprints one error log line from service.
func FromLog(service, line string) Signature {
	for _, c := range classifiers {
		if c.re.MatchString(line) {
			return Signature{Kind: c.kind, Service: service}
		}
	}
	sum := sha1.Sum([]byte(Normalize(line)))
	return Signature{Kind: "unknown:" + hex.EncodeToString(sum[:])[:12], Service: service}
}

// FromExit fingerprints a container that stopped.
func FromExit(service string, exitCode int, oomKilled bool) Signature {
	if oomKilled || exitCode == 137 {
		return Signature{Kind: "oom:killed", Service: service}
	}
	return Signature{Kind: fmt.Sprintf("exit:%d", exitCode), Service: service}
}

// normalizers strip values that vary between runs of the same failure. Order matters:
// timestamps and UUIDs go before the generic number rule eats their digits.
var normalizers = []struct {
	re   *regexp.Regexp
	repl string
}{
	{regexp.MustCompile(`\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?`), "<ts>"},
	{regexp.MustCompile(`(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b`), "<uuid>"},
	{regexp.MustCompile(`\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b`), "<ip>"},
	{regexp.MustCompile(`(?i)\b[0-9a-f]{12,}\b`), "<hex>"},
	{regexp.MustCompile(`\d+`), "<n>"},
	{regexp.MustCompile(`\s+`), " "},
}

// Normalize lowercases line and replaces run-specific values with placeholders.
func Normalize(line string) string {
	s := line
	for _, n := range normalizers {
		s = n.re.ReplaceAllString(s, n.repl)
	}
	return strings.ToLower(strings.TrimSpace(s))
}
