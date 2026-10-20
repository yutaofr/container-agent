.PHONY: runner-test runner-integration runner-build platform-test browser-test e2e e2e-llm

runner-test:
	cd runner && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

# Needs a Docker daemon; starts and removes throwaway busybox clusters.
runner-integration:
	cd runner && go vet -tags integration ./... && go test -tags integration -count=1 ./test/integration/

runner-build:
	cd runner && go build -o ../bin/cagent-runner ./cmd/cagent-runner

platform-test:
	cd platform && uv run ruff check . && uv run ruff format --check . && uv run pytest --ignore=tests/browser

# The browser suite: Playwright's async fixtures need the session event loop, given here
# explicitly. Needs Chromium: cd platform && uv run playwright install chromium
browser-test:
	cd platform && uv run pytest tests/browser -o asyncio_default_fixture_loop_scope=session -o asyncio_default_test_loop_scope=session --browser chromium --tracing retain-on-failure --screenshot only-on-failure --output test-results

# Generated compose files must pass the real validate command, the real Runner must
# answer the hub and reconnect after a hub restart, the orchestrator must heal a real
# cluster with T0 rules alone, an unregistered project must be inspected, drafted, and
# built, init scripts, keyspaces, and topics must exist before the application starts,
# and a run created over HTTP must reach healthy with the real Runner.
e2e: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner uv run pytest tests/test_cli.py tests/test_hub_e2e.py tests/test_orchestrator_e2e.py tests/test_inspect_e2e.py tests/test_init_e2e.py tests/test_platform_e2e.py

# Model tiers on real Docker, and Clarify's and drafting's model calls. Needs a local Ollama
# serving the models in platform/models.yaml; CI does not run it.
e2e-llm: runner-build
	cd platform && CAGENT_RUNNER=$(CURDIR)/bin/cagent-runner CAGENT_LLM_E2E=1 uv run pytest -s tests/test_llm_e2e.py tests/test_clarify_llm_e2e.py tests/test_draft_init_llm_e2e.py
