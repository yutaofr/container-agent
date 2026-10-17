// Package redact masks secrets in text before it leaves the user's machine.
package redact

import "regexp"

// Mask replaces every secret value.
const Mask = "******"

// rules run in order; earlier rules handle shapes later rules would mangle.
var rules = []struct {
	re   *regexp.Regexp
	repl string
}{
	// PEM private key blocks.
	{regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`), "-----PRIVATE KEY " + Mask + "-----"},
	// AWS access key IDs.
	{regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`), Mask},
	// Credentials embedded in URLs: scheme://user:password@host.
	{regexp.MustCompile(`(://[^:/@\s]+:)[^@\s]+@`), "${1}" + Mask + "@"},
	// Bearer tokens.
	{regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+`), "${1}" + Mask},
	// key=value, key: value, "key": "value" where the key names a secret.
	{regexp.MustCompile(`(?i)("?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*"?\s*[:=]\s*"?)([^"&;,\s]+)`), "${1}" + Mask},
}

// String returns s with every recognised secret replaced by Mask.
func String(s string) string {
	for _, r := range rules {
		s = r.re.ReplaceAllString(s, r.repl)
	}
	return s
}
