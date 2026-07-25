# AGENTS.md

Notes for coding agents. Humans should read `README.md`, `CONTRIBUTING.md` and
`docs/adr/` instead; this file exists to front-load the things that are easy to
get wrong here and expensive to get wrong.

## What this is

A CocoIndex custom target connector that keeps a Cognee knowledge graph in sync
with a changing document set. The difficulty is not API wiring. It is identity,
idempotency, replacement, configuration invalidation, deletion, and convergence
after a failed sync. Read `docs/adr/` before changing `src/`, starting with
0003 (consistency model) and 0004 (replace and delete protocol).

## Commands

Python 3.11 to 3.13 and [uv](https://docs.astral.sh/uv/). A `.venv` already
exists, so `uv run --no-sync` skips re-resolution.

```bash
make setup             # uv sync --all-extras
make ci                # lint + typecheck + audit gate + unit + property
make test              # unit only, no services
make test-property     # Hypothesis state machine
make test-integration  # real local Cognee, deterministic LLM (about 2 minutes)
make test-postgres     # needs Docker or POSTGRES_DSN
make test-llm          # opt-in, real provider, costs money
make coverage          # unit + property + integration; currently 89%
make smoke             # build a wheel and import it in a clean venv
```

## Layout

- `src/cogindex/` is the package. Modules are `_`-private; `__init__.py` and its
  `__all__` are the API contract.
- `tests/unit`, `tests/property`, `tests/integration` mirror the CI tiers.
  Markers: `property`, `integration`, `integration_llm`, `postgres`.
  `tests/common/engine_model.py` emulates the engine's tracking semantics and
  is the reason the fault matrix can exist without the real engine.
- `docs/adr/` is where design decisions and their reversals live.
  `docs/upstream-audit/` records what was read in cocoindex and cognee at the
  commits pinned in `UPSTREAM_LOCK.json`.
- `.upstream/` holds gitignored clones at those commits. Read them when in
  doubt. Upstream *tests* are the authoritative statement of upstream
  behaviour; upstream docs have been wrong more than once.

## How a sync flows

`DatasetHandler.reconcile` diffs dataset configuration and ownership and always
returns an output, even when converged, because its sink has to run to hand the
engine a child handler. That sink resolves the `CogneeRuntime` from the
`ContextProvider` and constructs a `DocumentHandler` with the runtime already
bound. `DocumentHandler.reconcile` derives `data_id`, collects stale ids, and
classifies the write. `DocumentHandler._apply` executes one batch per dataset,
under the dataset lock, in a fixed order: hard deletes, derivative purges, one
batched add, one cognify.

## Invariants

1. `reconcile()` is synchronous and does no I/O. The engine calls it while
   holding a mutex over the declared-states map. All external calls go in
   sinks.
2. Target keys and tracking records carry no credentials, URLs, connections or
   document content. External resources are named by `ContextKey` strings and
   resolved at sink time.
3. Identity is `uuid5` over logical coordinates, never content (ADR-0002). The
   namespace constant, `IDENTITY_SCHEMA_VERSION`, and the golden ids in
   `tests/unit/test_identity.py` are frozen: changing any of them renames every
   document anyone has ever indexed.
4. Sink actions are idempotent and safe under `prev_may_be_missing` and
   multiple `prev_possible_records`. Deleting or purging something absent is
   success.
5. `prev_may_be_missing=True` with a non-empty `prev_possible_records` is a
   `replace`, never an `upsert`. A torn hard delete drops derivatives before
   the row that carries the COMPLETED status, so the create path would commit a
   tracking record over a document with no derivatives and never look at it
   again (ADR-0004, second amendment). Empty `prev_records` keeps the create
   path deliberately: nothing recorded could have torn.
6. Content, annotations and processing fingerprints are derivative-affecting
   and force a replace. The metadata fingerprint (label, external metadata) is
   benign and takes the cheap re-add path. Collapsing these makes every label
   edit pay for a re-extraction.
7. Version-sensitive cognee imports go through `src/cogindex/_compat.py` and
   nowhere else. No monkey-patching.
8. Never commit a tracking record for a write that was not attempted, and never
   treat a cognee result containing `PipelineRunErrored` as success.
9. Logs carry phase, counts and timing. Never content, never secrets.

## Testing rules

- Do not weaken a test to make it pass. The fault matrix and the Hypothesis
  machine encode the convergence contract, not error handling.
- A test that passes with and without your fix is not evidence. Revert the fix
  and watch it fail.
- Fake-runtime tests are never integration tests, and the deterministic-LLM
  integration tier is never a real end-to-end test.
- `examples/` has weaker gating than the rest: ruff lints it, mypy does not,
  and only `tests/integration/test_examples.py` executes it. Three defects hid
  there. If you touch an example, run it with a relative folder path and a
  subfolder, because those were the conditions under which it broke.

## Style

Ruff at line length 100 with `E,F,W,I,UP,B,C4,SIM,RUF,ASYNC,S,T20`. Strict mypy
over `src` and `tests`. `from __future__ import annotations` everywhere,
explicit `__all__`, `__slots__` on the hot handler classes, frozen dataclasses
for specs and frozen `msgspec.Struct` for tracking records.

Comments explain why, especially where the reason is an upstream hazard or a
measurement. They do not narrate the code. Rationale that outgrows a comment
becomes an ADR.

## Upstream behaviour that is easy to assume wrongly

- Re-adding changed content under the same `data_id` does not remove the old
  derivatives. Call `forget(memory_only=True)` first.
- `add()` defaults `incremental_loading` and `data_cache` to True, and either
  one makes it skip a `data_id` whose add-pipeline status is COMPLETED. The
  replacement content then never lands, because a memory-only purge resets only
  the cognify pipeline. cogindex passes both as False; do not simplify that
  away.
- The cognify gate compares one per-item status and has no notion of
  configuration. Config invalidation is entirely this connector's job.
- A hard delete removes graph and vector derivatives first and the relational
  row last, so a crash in between leaves a document that looks complete and has
  nothing derived from it. Invariant 5 exists for this.
- Cognee scopes its graph engine per dataset and shuts the worker down when
  that scope closes, on a thread join costing roughly 2.7s. Batch operations
  must share one context (`_compat.dataset_database_context`) or they pay it
  per document. Running those deletions concurrently is faster and leaves
  orphaned nodes behind; this was measured, so do not re-litigate it without
  new measurements.
- Cognee's own dataset lock is process-local asyncio only.
- Cognee's default storage paths are inside its installed package. Always pass
  `data_root` and `system_root`, absolutized.
- The CocoIndex handler attachment method is `attachments() -> dict`, plural.
  The upstream SKILL.md showing `attachment(att_type)` is stale.
- CocoIndex forces `prev_may_be_missing=True` for fresh keys, delete markers,
  pending states, schema-version mismatch and full reprocess. Two live records
  with no delete marker keep it False, and the handler's own comparison decides.

## Known limits

These are upstream-constrained and documented rather than hidden. Do not open
work to "fix" them without an upstream change first.

- No REST-backed runtime: Cognee's REST add takes no `data_id`.
- Unmounting empties a dataset but leaves the empty dataset row.
- `managed_by="user"` means cogindex destroys nothing there, not that it
  removes only what it added.
- `verify_dataset` compares presence, identity, completion and label. It cannot
  see whether derivatives match current content.
- Losing the CocoIndex tracking store leaves documents looking new while Cognee
  still holds their old derivatives. Recovery is one dataset-level
  `forget(memory_only=True)` and a re-run (ADR-0004).
