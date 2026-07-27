# ADR-0006: Concurrency and dataset locking

Status: accepted · Date: 2026-07-24

## Context

Cognee serializes cognify per dataset with a **process-local**
`asyncio.Lock` (`cognee/modules/pipelines/operations/pipeline.py`,
`_dataset_locks`); the code comments state outright that it "does NOT protect
against multiple processes/workers" and is "to be replaced by a cross-process
mechanism later". Its safety net for crashed runs is age-based stale-run
recovery (default 3600 s), which explicitly tolerates, rather than prevents,
concurrent multi-process runs.

Two cogindex workers materializing into the same dataset can therefore
interleave `forget`/`add`/`cognify` in ways that are individually idempotent
but jointly wasteful and, during replace sequences, transiently inconsistent.
A system-managed whole-dataset teardown is another writer: without the same
guard it can erase a dataset while another connector worker is adding or
rebuilding one of its documents.

## Decision

cogindex introduces a `LockProvider` abstraction and takes a dataset-scoped
lock around every document sink batch (the forget→add→cognify sequence for
one dataset) and every system-managed dataset teardown:

- `InProcessLockProvider`: default; `asyncio.Lock` per lock key. Correct for
  one process and one event loop, and for tests.
- `PostgresAdvisoryLockProvider` (extra: `cogindex[postgres]`): cross-process
  implementation using PostgreSQL session-level advisory locks. It polls
  `pg_try_advisory_lock(bigint)` through asyncpg. Key mapping:
  `BLAKE2b-64(canonical(tenant, dataset_key)) → signed int64`, deterministic
  across workers. A hash collision only adds serialization; it cannot allow
  overlapping connector writes.

Contract (uniform across providers):

- lock keys derive from stable logical identity (tenant/user + dataset key),
  never from connection details;
- acquisition takes a configurable timeout covering connection and polling,
  and raises `LockTimeoutError` with the logical scope and advisory key;
- locks are held for the duration of one document batch or one teardown and
  released in a `finally`; a crashed holder's advisory lock dies with its
  session (PostgreSQL) or its process (in-process), no lock outlives its
  owner;
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
  dataset. That boundary is documented, not hidden.
- The document action sequence remains idempotent under replay (ADR-0003),
  and the Hypothesis model also runs with a no-op lock. This supports
  convergence for modeled document batches after the desired state stops
  changing.
- Whole-dataset teardown is different: it must take the same lock as document
  batches. Otherwise teardown can erase a dataset while a batch is rebuilding
  it, and the older operation may finish last. The lock does not provide
  generation fencing, so operators must not run two different desired-state
  generations for the same dataset concurrently.

`tests/unit/test_fault_matrix.py::test_concurrent_batches_serialize_under_dataset_lock`
asserts that two document batches do not overlap.
`tests/unit/test_reconcile_dataset_and_apply.py::
test_dataset_teardown_waits_for_document_batch_lock` asserts that teardown
does not enter while a document batch holds the lock. Removing the respective
outer lock makes each regression test fail.
