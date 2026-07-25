# ADR-0005: Configuration invalidation

Status: accepted · Date: 2026-07-24

## Context

Cognee's incremental gate checks a single per-item status
(`pipeline_status[pipeline][dataset_id] == COMPLETED`). It carries **no
fingerprint of the prompt, graph model, chunker, LLM, or embedding model** —
changing any of these upstream re-processes nothing. Derivative correctness
under configuration change is therefore entirely this connector's job.

## Decision

A `processing_fingerprint` is computed over every input that shapes
derivatives:

- graph model identity plus a fingerprint of its JSON schema, so a structural
  edit to a model that keeps its name still invalidates,
- extraction prompt content,
- chunker identity and chunk size,
- LLM model identifier,
- embedding model identifier and vector dimensions, which cognee reads from
  the environment independently of each other,
- cogindex's own record-schema version.

Values the caller leaves unset are resolved to the installed cognee's
effective defaults before fingerprinting, so upgrading cognee is itself a
config change when it moves a default.

The fingerprint uses canonical serialization (sorted keys, explicit types), so
dict ordering or equivalent representations cannot cause spurious changes.

Two invalidation mechanisms are used **together**:

1. **Per-document:** the `processing_fingerprint` is stored in every document
   tracking record. `reconcile()` treats a fingerprint mismatch exactly like a
   content change → *replace* (purge derivatives via
   `forget(memory_only=True)`, re-add, re-cognify).
2. **Dataset-level:** when the dataset spec's processing configuration
   changes, the dataset handler also returns `child_invalidation="lossy"`,
   which makes the engine pass `prev_may_be_missing=True` to every child —
   forcing conservative replay even for documents whose tracking records the
   engine can no longer trust.

Mechanism 1 alone is sufficient in the common case; mechanism 2 covers the
uncertainty window around a torn dataset-level update and makes the behavior
explainable from either side. The redundancy is cheap (replays are
fingerprint-gated no-ops when nothing actually changed at the document level).

## What must NOT invalidate

Connection URLs, credentials, lock providers, batching knobs and log levels
cannot change derivatives, so they stay out of the fingerprint. Putting them
in would be worse than useless: it would rebuild an entire dataset's graph
because someone rotated a password.

Both directions are pinned by tests, in
`tests/unit/test_records_and_spec.py` at the fingerprint level and
`tests/unit/test_engine_lifecycle.py` at the reconcile level:

- every field of `ProcessingConfig` changed one at a time produces a different
  fingerprint (parametrized, with a guard test asserting the parametrization
  covers every field, so a new field cannot be added without covering it);
- a derivative-affecting change purges and re-cognifies every document;
- a change to something outside `ProcessingConfig` produces an identical
  fingerprint and therefore issues no purge and no cognify;
- an identical re-run is a no-op with zero mutating calls.
