# ADR-0004: Replacement and deletion protocol

Status: accepted · Date: 2026-07-24

## Context (audited Cognee behavior)

- Re-`add` of the same `data_id` with new content updates the relational row
  and resets its pipeline status, **but leaves the previous content's graph
  nodes, edges, and vectors in place** (`ingest_data.py`). Chunk/entity ids
  are content-derived, so the old derivatives become unreferenced leftovers.
- `cognee.forget()` is the unified deletion API (keyword-only):
  - `forget(data_id=…, dataset_id=…, memory_only=True)` deletes the item's
    graph/vector derivatives and resets its cognify status, preserving the raw
    data row — explicitly framed upstream as the "re-cognify with different
    settings" primitive.
  - `forget(data_id=…, dataset_id=…)` hard-deletes raw data, dataset link,
    derivatives, and provenance refs.
- Deletion runs through Cognee's provenance delete planner
  (`provenance_delete_planner.py`): artifacts whose source-ref set becomes
  empty are hard-deleted; artifacts still referenced by another
  `(dataset, data)` survive with only the target ref detached. The ordering is
  retry-safe (vectors → detach survivors → delete unowned → orphan cleanup).
  On the default stack (Ladybug graph + LanceDB), provenance refs are folded
  into graph writes atomically — there is no write-then-attach window.

## Decision

`reconcile()` classifies each document into four actions; the sink executes
them per dataset in a fixed order.

**Create** (no previous record, or previous may be missing):
1. `add([DataItem(data, data_id=…)], dataset_id=…)` — upsert, safe if the row
   already exists from a torn earlier attempt;
2. one incremental `cognify(dataset)` per dataset per batch.

**Replace** (content or processing fingerprint changed, or previous state
uncertain):
1. `forget(data_id=…, dataset_id=…, memory_only=True)` — purge old
   derivatives; harmless if none exist;
2. `add(...)` with the *same* `data_id` and new content;
3. incremental `cognify(dataset)`.

Skipping step 1 is the classic staleness bug (old entities survive alongside
new ones); skipping step 2 loses the document. The sequence is idempotent:
each step tolerates the world already being in its post-state.

**Hard delete** (desired state is NON_EXISTENCE):
1. `forget(data_id=…, dataset_id=…)`; already-missing data is success.
   cogindex's runtime layer enforces this tolerance and a contract test pins
   it, independent of upstream's internal behavior.

**No-op**: all possible previous records equal the desired record and the
previous state cannot be missing.

## Shared-entity provenance

cogindex deliberately does **not** implement its own graph deletion. It always
deletes through `forget`, which routes through the provenance planner, so an
entity supported by two documents survives deletion of one (only its ref is
detached) and is removed when its last supporting document goes. cogindex's
integration tests assert this end-to-end (mirroring upstream's
`test_shared_node_preservation.py`), because it is the property users depend
on — but the mechanism is Cognee's, not ours.

## Batching

Actions are grouped by `(runtime, dataset)`. Within a dataset batch:
deletes and replace-purges first, then all adds (batched), then exactly one
`cognify`. Deterministic order (sorted by document key) keeps runs comparable
and logs reproducible. Partial failures propagate; nothing is swallowed.
Structured logs record phase and timing only — never content, never secrets.

## Dataset teardown and unmount semantics (verified against the engine)

When a dataset target stops being declared (its component path disappears),
the engine reconciles the container to non-existence and runs its sink once
more. Engine-verified behavior (tests/unit/test_engine_lifecycle.py):

- **System-managed** (`managed_by="system"`): the container sink calls
  `teardown_dataset`, which empties the dataset via `forget(dataset_id=...)`
  — raw data, graph, and vector derivatives. The dataset *row* survives:
  upstream's `empty_dataset` keeps it, and there is no public dataset-row
  delete API. Documented as an upstream limitation, not papered over.
- **User-managed** (`managed_by="user"`): `resolve_system_transition` yields
  no action; the runtime observes **zero mutating calls**. Note the
  consequence: the engine drops child tracking when the container goes away
  without issuing per-document deletes, so documents that cogindex itself
  added remain in the dataset. `managed_by="user"` therefore means "cogindex
  never destroys anything in this dataset", not "cogindex removes only what
  it added". Users who want managed cleanup must use `managed_by="system"`.

A dataset that never materialized (declared but nothing was ever added)
tears down as a no-op: `teardown_dataset` resolves the name, finds no
dataset, and returns.
