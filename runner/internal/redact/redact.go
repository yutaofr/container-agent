// Package redact masks secrets in text before it leaves the user's machine.
package redact

import "regexp"

// Mask replaces every secret value.
const Mask = "******"

var (
	// A reference names a secret without holding it: a {{template}} placeholder, or a
	// ${VAR} / ${VAR:default} placeholder. Its default is a value and is masked.
	template    = regexp.MustCompile(`^\{\{\w+\}\}$`)
	placeholder = regexp.MustCompile(`^\$\{([\w.-]+)(:.*)?\}$`)
)

// mask returns Mask for a secret value, and keeps the name of a reference.
func mask(value string) string {
	if template.MatchString(value) {
		return value
	}
	if m := placeholder.FindStringSubmatch(value); m != nil {
		if m[2] == "" {
			return value
		}
		return "${" + m[1] + ":" + Mask + "}"
	}
	return Mask
}

// rules run in order; earlier rules handle shapes later rules would mangle. A rule with
// value set masks only group 2, the value; groups 1 and 3 (if any) are kept.
var rules = []struct {
	re    *regexp.Regexp
	repl  string
	value bool
}{
	// PEM private key blocks.
	{re: regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----`), repl: "-----PRIVATE KEY " + Mask + "-----"},
	// AWS access key IDs.
	{re: regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`), repl: Mask},
	// Credentials embedded in URLs: scheme://user:password@host.
	{re: regexp.MustCompile(`(://[^:/@\s]+:)([^@\s]+)(@)`), value: true},
	// Bearer tokens.
	{re: regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+`), repl: "${1}" + Mask},
	// key=value, key: value, "key": "value" where the key names a secret.
	{re: regexp.MustCompile(`(?i)("?[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*"?\s*[:=]\s*"?)([^"&;,\s]+)`), value: true},
}

// String returns s with every recognised secret replaced by Mask.
func String(s string) string {
	for _, r := range rules {
		if !r.value {
			s = r.re.ReplaceAllString(s, r.repl)
			continue
		}
		s = r.re.ReplaceAllStringFunc(s, func(match string) string {
			m := r.re.FindStringSubmatch(match)
			if len(m) > 3 {
				return m[1] + mask(m[2]) + m[3]
			}
			return m[1] + mask(m[2])
		})
	}
	return s
}
