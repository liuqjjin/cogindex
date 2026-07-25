# Development entry points. Everything here must run identically in CI.

UV ?= uv

.PHONY: setup lint format typecheck test test-property test-integration \
        test-postgres test-llm coverage audit smoke benchmark-smoke build ci \
        clean upstream-lock

setup:            ## Install the dev environment
	$(UV) sync --all-extras

lint:             ## Ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:           ## Auto-format
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:        ## Strict mypy
	$(UV) run mypy

test:             ## Fast tests: no network, no services, no LLM
	$(UV) run pytest tests/unit -q

test-property:    ## Hypothesis property/state-machine tests
	$(UV) run pytest tests/property -q -m property

test-integration: ## Real local Cognee stack (SQLite+LanceDB+embedded graph), deterministic LLM
	$(UV) run pytest tests/integration -q -m integration

test-llm:         ## Opt-in: real LLM provider (requires LLM_API_KEY)
	$(UV) run pytest tests/integration -q -m integration_llm

test-postgres:    ## PostgreSQL advisory-lock tests (Docker/testcontainers or POSTGRES_DSN)
	$(UV) run pytest tests/integration -q -m postgres

coverage:         ## Line+branch coverage over the unit and property tiers
	$(UV) run coverage run -m pytest tests/unit tests/property -q
	$(UV) run coverage report
	$(UV) run coverage xml

audit:            ## Every upstream first-party file carries a review status
	$(UV) run python docs/upstream-audit/tools/check_coverage.py

smoke:            ## Build wheel and import it in a clean venv
	scripts/smoke_install.sh

benchmark-smoke:  ## Tiny benchmark run to validate the harness end to end
	$(UV) run python -m benchmarks.run --profile smoke

build:            ## Build sdist + wheel
	$(UV) build

ci: lint typecheck audit test test-property   ## What the required CI job runs

upstream-lock:    ## Regenerate UPSTREAM_LOCK.json + audit inventories from .upstream/ clones
	$(UV) run python docs/upstream-audit/tools/generate_inventory.py .upstream/cocoindex docs/upstream-audit/cocoindex --name cocoindex
	$(UV) run python docs/upstream-audit/tools/generate_inventory.py .upstream/cognee docs/upstream-audit/cognee --name cognee

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .hypothesis
