.PHONY: runner-test runner-build

runner-test:
	cd runner && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

runner-build:
	cd runner && go build -o ../bin/cagent-runner ./cmd/cagent-runner
