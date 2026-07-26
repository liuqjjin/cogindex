# ADR-0003: Consistency model (at-least-once, idempotent, eventually convergent)

Status: accepted · Date: 2026-07-24

## Invariant

```
CogneeTargetState = F(
    current source documents,
    processing configuration,
    connector schema version,
)
```

If the desired state stops changing, the tracking store is retained, upstream
services recover, and failures eventually stop, a later complete
synchronization must establish the state `F` prescribes: no stale managed
documents, no orphaned managed derivatives, and no duplicate managed
identities.

## Why cross-system ACID is impossible

CocoIndex persists tracking state in its own embedded store (LMDB); Cognee
persists across three stores (relational + graph + vector). There is no shared
transaction manager, and Cognee's own writes are not atomic across its three
stores either. Two-phase commit is unavailable at every layer, so cogindex
does not claim cross-system atomicity.

## What is promised instead

**At-least-once delivery of idempotent actions, with tracked uncertainty.**

1. CocoIndex's engine order is: precommit (persist intended tracking records,
   keeping *both* old and new as possible states) → external sink → commit
   (collapse to the confirmed state). A failure in sink or commit leaves
   multiple `prev_possible_records`; a failed create/delete additionally sets
   `prev_may_be_missing=True`. This is persisted, so uncertainty survives
   crashes and process restarts.
2. cogindex's `reconcile()` treats uncertainty conservatively. It chooses
   create, replace, hard recreation, metadata update, or deletion from all
   possible previous records; a no-op is allowed only when every possible
   previous record equals the desired record and none may be missing.
3. Every action is idempotent against Cognee:
   - `add` with an explicit `data_id` is an upsert (verified in
     `ingest_data.py`, including its `IntegrityError` retry path);
   - `forget(..., memory_only=True)` removes derivatives that exist and is
     harmless when they don't;
   - hard `forget(data_id=...)` treats already-missing data as success
     (enforced and contract-tested in cogindex's runtime layer);
   - `cognify` skips items whose per-item pipeline status is completed, and
     re-processes items whose status was reset.

## Convergence argument

Let `D` be a desired state that has stopped changing. A precommit can add
another possible tracking record, so the uncertainty set is not required to
shrink after every attempt. What matters is the next attempt that completes
both the external sink and CocoIndex commit:

1. reconciliation sees every persisted possible record and selects an action
   that is safe for all of them;
2. the completed external sequence establishes `D` for the managed document
   or dataset;
3. commit collapses the possible records to the confirmed desired record.

Later retries are no-ops unless the desired state changes again. This argument
assumes the desired state becomes stable, the tracking store is not lost,
upstream services recover, failures are finite, and at least one complete
sink-plus-commit attempt is allowed to finish.

The fault-injection matrix and Hypothesis state machine exercise bounded
models of the named failure windows, including failures before commit and
two-worker contention. They are evidence for the implementation, not a proof
of every possible upstream or deployment failure.

## Visible anomalies (documented, not hidden)

- Between a Cognee write and the corresponding CocoIndex commit, Cognee may
  briefly contain state newer than the confirmed tracking record
  (read-your-writes across systems is not promised).
- A crash after `forget(memory_only=True)` but before re-add leaves the
  document temporarily absent from search until retry (at-least-once, not
  exactly-once).
- `verify_dataset()` exists to detect residual drift (external mutation, operator
  error) that the model cannot prevent, e.g. a human deleting Cognee data
  behind the connector's back. It compares presence, identity, completion
  and label, but **not** whether derivatives match current content, so stale or
  absent derivatives under an otherwise healthy row are invisible to it.
- Losing the tracking store is outside the convergence argument: uncertainty
  the engine *records* is replayed conservatively, but uncertainty that is
  *erased* leaves documents looking brand new while Cognee still holds their
  old derivatives. Recovery is one dataset-level
  `forget(dataset_id=…, memory_only=True)` followed by a re-run (ADR-0004's
  second amendment).
