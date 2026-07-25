# ADR-0003: Consistency model — at-least-once, idempotent, eventually convergent

Status: accepted · Date: 2026-07-24

## Invariant

```
CogneeTargetState = F(
    current source documents,
    processing configuration,
    connector schema version,
)
```

After any finite sequence of declare/update/delete operations and any number
of *recoverable* failures, retrying synchronization must converge to the state
`F` prescribes — no stale documents, no orphaned derivatives, no duplicates.

## Why cross-system ACID is impossible

CocoIndex persists tracking state in its own embedded store (LMDB); Cognee
persists across three stores (relational + graph + vector). There is no shared
transaction manager, and Cognee's own writes are not atomic across its three
stores either. Two-phase commit is unavailable at every layer. Any design
promising atomicity would be lying.

## What is promised instead

**At-least-once delivery of idempotent actions, with tracked uncertainty.**

1. CocoIndex's engine order is: precommit (persist intended tracking records,
   keeping *both* old and new as possible states) → external sink → commit
   (collapse to the confirmed state). A failure in sink or commit leaves
   multiple `prev_possible_records`; a failed create/delete additionally sets
   `prev_may_be_missing=True`. This is persisted, so uncertainty survives
   crashes and process restarts.
2. cogindex's `reconcile()` treats uncertainty conservatively: if any possible
   previous record differs from the desired record, or the previous state may
   be missing, it emits a *convergent* action (replay the full
   replace-or-create sequence). A no-op is emitted only when every possible
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

Let `D` be the desired state. After any failure, the persisted tracking state
either (a) equals `D` and is confirmed — retry is a no-op; or (b) records
uncertainty — retry replays an idempotent sequence whose *post-state is `D`
regardless of the actual Cognee state within the uncertainty set*. Each
successful retry strictly shrinks the uncertainty set (CocoIndex commits
collapse multi-state tracking), and no step widens it. Hence finitely many
retries reach (a). The fault-injection matrix (`tests/` — nine injection
points, two-worker contention) exists to demonstrate exactly this property,
not merely to exercise error paths.

## Visible anomalies (documented, not hidden)

- Between a Cognee write and the corresponding CocoIndex commit, Cognee may
  briefly contain state newer than the confirmed tracking record
  (read-your-writes across systems is not promised).
- A crash after `forget(memory_only=True)` but before re-add leaves the
  document temporarily absent from search until retry (at-least-once, not
  exactly-once).
- `verify()` exists to detect residual drift (external mutation, operator
  error) that the model cannot prevent, e.g. a human deleting Cognee data
  behind the connector's back. It compares presence, identity, completion
  and label — **not** whether derivatives match current content, so stale or
  absent derivatives under an otherwise healthy row are invisible to it.
- Losing the tracking store is outside the convergence argument: uncertainty
  the engine *records* is replayed conservatively, but uncertainty that is
  *erased* leaves documents looking brand new while Cognee still holds their
  old derivatives. Recovery is one dataset-level
  `forget(dataset_id=…, memory_only=True)` followed by a re-run (ADR-0004's
  second amendment).
