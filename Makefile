.PHONY: runner-test runner-integration runner-build platform-test e2e

runner-test:
	cd runner && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

# Needs a Docker daemon; starts and removes throwaway busybox clusters.
runner-integration:
	cd runner && go vet -tags integration ./... && go test -tags integration -count=1 ./test/integration/

runner-build:
	cd runner && go build -o ../bin/cagent-runner ./cmd/cagent-runner

platform-test:
	cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest

# Generated compose files must pass the real validate command, and the real Runner must
# answer the hub and reconnect after a hub restart.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py
