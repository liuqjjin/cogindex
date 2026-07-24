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

- graph model / schema identity and version,
- extraction prompt identity and version,
- chunker configuration and version,
- LLM model identifier,
- embedding model identifier (and dimensions),
- cogindex's own derivation-schema version.

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

Connection URLs, credentials, lock providers, batching knobs, log levels —
anything that cannot change derivatives — is excluded from the fingerprint.
Tests pin both directions:

- content unchanged + prompt version bumped → re-cognify happens;
- content unchanged + embedding model changed → vectors rebuilt;
- unrelated config changed → **zero** re-processing;
- identical config re-run → no-op.
