# ADR-0006: Concurrency and dataset locking

Status: accepted · Date: 2026-07-24

## Context

Cognee serializes cognify per dataset with a **process-local**
`asyncio.Lock` (`cognee/modules/pipelines/operations/pipeline.py`,
`_dataset_locks`); the code comments state outright that it "does NOT protect
against multiple processes/workers" and is "to be replaced by a cross-process
mechanism later". Its safety net for crashed runs is age-based stale-run
recovery (default 3600 s), which explicitly tolerates — rather than prevents —
concurrent multi-process runs.

Two cogindex workers materializing into the same dataset can therefore
interleave `forget`/`add`/`cognify` in ways that are individually idempotent
but jointly wasteful and, during replace sequences, transiently inconsistent.

## Decision

cogindex introduces a `LockProvider` abstraction and takes a dataset-scoped
lock around every sink batch (the forget→add→cognify sequence for one
dataset):

- `InProcessLockProvider` — default; `asyncio.Lock` per lock key. Correct for
  the single-process case and for tests.
- `PostgresAdvisoryLockProvider` (extra: `cogindex[postgres]`) — production
  multi-worker implementation using PostgreSQL session-level advisory locks
  (`pg_advisory_lock(classid, objid)` via asyncpg). Key mapping:
  `hash64(canonical(tenant, dataset_key)) → (int32, int32)`, deterministic
  across workers.

Contract (uniform across providers):

- lock keys derive from stable logical identity (tenant/user + dataset key),
  never from connection details;
- acquisition takes a configurable timeout and raises a diagnosable
  `LockTimeoutError` naming the key and holder context where available;
- locks are held for the duration of one dataset batch, released in a
  `finally`; a crashed holder's advisory lock dies with its session
  (PostgreSQL) or its process (in-process) — no lock outlives its owner;
- lock objects and provider configuration never enter tracking records or
  target keys.

## Why not fix it upstream first

A public cross-process lock provider in Cognee is the right long-term home,
and `docs/upstream-proposals/` includes exactly that proposal. But this
connector must be safe on today's released Cognee (v1.4.0); a connector-level
lock around the connector's own write path achieves that without patching
upstream. The two compose: when Cognee grows a real lock, ours remains a
harmless outer guard and can be retired by configuration.

## What the lock does and does not claim

- It serializes *cogindex workers* against each other. It cannot serialize a
  third-party process calling `cognee.cognify()` directly against the same
  dataset — that boundary is documented, not hidden.
- Correctness does not depend on the lock: every action stays idempotent and
  convergent (ADR-0003). The lock removes wasted duplicate cognify work and
  shrinks the replace-sequence inconsistency window; it is an efficiency and
  operational-hygiene mechanism layered on a design that is already safe.

  Two independent observations back that claim, and it is worth being precise
  about which does what. The Hypothesis convergence machine still passes with
  the dataset lock replaced by a no-op, which is the evidence that
  convergence does not need it. What the lock *does* buy is measured by
  `tests/unit/test_fault_matrix.py::test_concurrent_batches_serialize_under_dataset_lock`,
  which asserts two concurrent workers produce two non-overlapping
  lock-delimited batches and fails when the lock is removed.
