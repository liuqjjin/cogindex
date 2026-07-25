# Contributing to cogindex

Thanks for considering a contribution.

## Setup

Requirements: Python 3.11 to 3.13, [uv](https://docs.astral.sh/uv/).

```bash
git clone <your fork>
cd cogindex
make setup          # uv sync --all-extras
uv run pre-commit install
```

## Development loop

```bash
make lint typecheck test    # fast gates, no services
make test-property          # Hypothesis state-machine tests
make test-integration       # real local Cognee stack, deterministic LLM
make test-postgres          # needs Docker or POSTGRES_DSN
make test-llm               # opt-in, needs LLM_API_KEY
```

`make ci` mirrors the required GitHub Actions job exactly.

## Ground rules

- **Read the ADRs first** (`docs/adr/`). PRs that violate the consistency
  model (I/O in `reconcile()`, content-derived identity, non-idempotent sink
  actions, credentials in keys or tracking records) will be declined
  regardless of test status.
- **No vendored upstream code, no monkey-patching.** Version-sensitive Cognee
  imports live only in `src/cogindex/_compat.py`. If an upstream API gap
  blocks you, write a proposal in `docs/upstream-proposals/` instead.
- **Honest tests.** Tests using `FakeCogneeRuntime` or the deterministic LLM
  adapters must not be named or documented as real end-to-end tests.
- **No secrets** in code, tests, fixtures, or logs. Structured logs carry
  phases and timings, never document content.
- Strict typing (`mypy --strict`) and Ruff are enforced; `py.typed` ships.

## Upstream version changes

Bumping the supported `cocoindex`/`cognee` range requires:
1. updating `UPSTREAM_LOCK.json` via `make upstream-lock` (with refreshed
   `.upstream/` clones),
2. re-running `tests/unit/test_compat.py`, which pins the exact upstream
   surface `src/cogindex/_compat.py` depends on, and
   `tests/integration -m integration`, which is where behavior changes that
   type signatures cannot reveal actually show up,
3. a CHANGELOG entry describing observed upstream behavior changes.

## Commit / PR conventions

- Conventional-commit style subjects (`feat:`, `fix:`, `docs:`, `test:` …).
- One logical change per PR; include the failure mode a fix addresses.
- New behavior needs tests at the appropriate tier (unit / property /
  integration); fault-model changes need a fault-injection case.
