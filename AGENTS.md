# AGENTS.md

Guidance for coding agents working in this repository.

## What this project is

`cogindex` materializes CocoIndex-managed documents into Cognee knowledge
graphs as a **CocoIndex custom target connector**. The hard problems are
identity, idempotency, replacement, config invalidation, deletion, and
convergence after failure — not API wiring. Read `docs/adr/` before touching
`src/`; ADR-0003 (consistency model) and ADR-0004 (replace/delete protocol)
are the load-bearing documents.

## Commands

```bash
make setup            # uv sync --all-extras
make lint typecheck   # ruff + strict mypy
make test             # unit tests (no network/services/LLM)
make test-property    # Hypothesis state machines
make test-integration # real local Cognee, deterministic LLM adapters
make ci               # exactly what required CI runs
```

## Hard rules

1. `reconcile()` implementations are synchronous and perform **no I/O** — the
   engine calls them under a lock. All external calls go in action sinks.
2. Target keys and tracking records never contain credentials, URLs,
   connections, or raw document content. External resources are referenced by
   `ContextKey` strings and resolved at sink time.
3. Document identity is `uuid5` over logical coordinates (ADR-0002) — never
   content hashes.
4. Every sink action must be idempotent and safe under `prev_may_be_missing`
   and multiple `prev_possible_records`.
5. All Cognee imports that are version-sensitive (e.g. `DataItem`) go through
   `src/cogindex/_compat.py` only. No monkey-patching upstream.
6. Do not weaken a test to make it pass; fault-injection tests encode the
   convergence contract.
7. Fake-runtime tests must never be presented as real integration tests.

## Layout

- `src/cogindex/` — the package; modules are `_`-private, public API is
  re-exported in `__init__.py`.
- `tests/unit`, `tests/property`, `tests/integration` — tiered as in CI;
  markers: `property`, `integration`, `integration_llm`, `postgres`.
- `docs/upstream-audit/` — audit ledger for the pinned upstream commits
  (`UPSTREAM_LOCK.json`); regenerate with `make upstream-lock`.
- `.upstream/` — gitignored clones of cocoindex/cognee for reference. Read
  them when in doubt; upstream *tests* are the authoritative statement of
  upstream semantics.

## Upstream facts agents commonly get wrong

- Re-adding changed content to Cognee under the same `data_id` does **not**
  remove old graph/vector derivatives — `forget(memory_only=True)` first.
- With `add()`'s defaults (`incremental_loading=True`, `data_cache=True`),
  an already-added `data_id` is **skipped entirely** — replacement content
  silently never lands. cogindex always passes both as False on add; do not
  "simplify" that away (the integration replace tests will catch it).
- Cognee's incremental cognify gate has **no config fingerprint**; config
  invalidation is cogindex's job.
- Cognee's dataset lock is process-local asyncio only.
- The CocoIndex handler attachment method is `attachments() -> dict` (plural);
  the upstream SKILL.md showing `attachment(att_type)` is stale.
