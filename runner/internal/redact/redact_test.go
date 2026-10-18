package redact

import "testing"

func TestString(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"jdbc password param",
			"jdbc:mysql://db:3306/app?user=root&password=s3cret&useSSL=false",
			"jdbc:mysql://db:3306/app?user=root&password=******&useSSL=false"},
		{"env var",
			"SPRING_DATASOURCE_PASSWORD=hunter2",
			"SPRING_DATASOURCE_PASSWORD=******"},
		{"json field",
			`{"api_key": "abc123", "user": "bob"}`,
			`{"api_key": "******", "user": "bob"}`},
		{"url credentials",
			"connecting to postgres://admin:pa55@db:5432/x",
			"connecting to postgres://admin:******@db:5432/x"},
		{"bearer token",
			"Authorization: Bearer eyJhbGciOi.abc.def",
			"Authorization: Bearer ******"},
		{"aws access key id",
			"key AKIAIOSFODNN7EXAMPLE loaded",
			"key ****** loaded"},
		{"aws secret with spaces",
			"aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
			"aws_secret_access_key = ******"},
		{"private key",
			"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
			"-----PRIVATE KEY ******-----"},
		// References name a secret without holding it: the platform needs them to draft and
		// read back descriptors. A reference's default is a value, so it is masked.
		{"descriptor bind template",
			`SPRING_DATASOURCE_PASSWORD: "{{password}}"`,
			`SPRING_DATASOURCE_PASSWORD: "{{password}}"`},
		{"spring placeholder",
			"spring.datasource.password=${DB_PASSWORD}",
			"spring.datasource.password=${DB_PASSWORD}"},
		{"spring placeholder default",
			"password: ${DB_PASSWORD:s3cret}",
			"password: ${DB_PASSWORD:******}"},
		{"url credentials reference",
			"jdbc:mysql://{{user}}:{{password}}@{{host}}/app",
			"jdbc:mysql://{{user}}:{{password}}@{{host}}/app"},
		{"placeholder-shaped secret is still masked",
			"password=${x}y",
			"password=******"},
		{"plain log line untouched",
			"Started Application in 3.2 seconds (tokenizer ready)",
			"Started Application in 3.2 seconds (tokenizer ready)"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := String(c.in); got != c.want {
				t.Errorf("String(%q)\n got %q\nwant %q", c.in, got, c.want)
			}
		})
	}
}

func TestStringIsIdempotent(t *testing.T) {
	in := "password=s3cret token: abc secret: ${API_SECRET:x} url://u:p@h"
	once := String(in)
	if twice := String(once); twice != once {
		t.Errorf("second pass changed output: %q -> %q", once, twice)
	}
}
