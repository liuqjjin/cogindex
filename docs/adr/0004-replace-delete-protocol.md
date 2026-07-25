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

**Create** (no previous record at all):
1. `add([DataItem(data, data_id=…)], dataset_id=…)` — upsert, safe if the row
   already exists from a torn earlier attempt;
2. one incremental `cognify(dataset)` per dataset per batch.

**Replace** (content or processing fingerprint changed, or previous state
uncertain — including a *recorded* document whose state may be missing; see
the second amendment):
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

## Amendment: the add-side skip gate (integration-tier discovery)

`cognee.add()`'s per-item pipeline ALSO has a skip gate, routed whenever
`data_cache or incremental_loading` — and both default to True. A data_id
whose `add_pipeline` status is COMPLETED is then skipped before ingestion
runs: replacement content would silently never be written, because
`forget(memory_only=True)` deliberately resets only `cognify_pipeline`.

The connector therefore always calls
`add(..., incremental_loading=False, data_cache=False)`. Idempotency for
unchanged content is preserved by ingestion's own content-hash comparison
(no pipeline-status reset when the hash is equal, so cognify still skips),
and the cognify-side incremental gate is unaffected. Found by — and pinned
in — `tests/integration/test_local_cognee.py`; the initial code audit
missed the gate's routing condition.

## Amendment: uncertain state over a recorded document is Replace, not Create

The original classification sent every statediff `insert`/`upsert` down the
Create path. That is wrong whenever a *recorded* document's state cannot be
confirmed, because the hard delete tears in a specific order:
`datasets.delete_data` removes graph and vector derivatives first
(`delete_data_nodes_and_edges`) and the relational row — which carries
`pipeline_status` — last. A crash in between leaves the document present
with **no derivatives and a COMPLETED cognify status**.

The engine then hands the next reconcile `prev=[last record]` with
`prev_may_be_missing=True`. Under the Create path the sink issues only
add + cognify: the add sees unchanged content so it resets no status, the
cognify gate skips the still-COMPLETED item, and the tracking record commits
over a document that will never be cognified again. The next `reconcile()`
returns `None` — a permanent non-convergent fixed point, and one
`verify_dataset` cannot see, since presence, label and completion all match.

The rule is therefore: **`prev_may_be_missing=True` with a non-empty
`prev_possible_records` classifies as Replace.** The extra
`forget(memory_only=True)` is idempotent and a no-op when the state is
intact. Pinned by `tests/unit/test_fault_matrix.py::
test_torn_delete_then_redeclare_rebuilds_derivatives`, which needs the
fake's `inject_fault("delete_documents", torn=True)` to reproduce the
ordering — an atomic delete cannot express this hazard.

**Empty `prev_possible_records` deliberately keeps the Create path**, even
under `prev_may_be_missing=True` (which the engine forces for every fresh
key). Nothing is recorded that could have torn, and purging unconditionally
would cost one `forget()` round trip per document on every first ingest:
measured at ~1.17 s per document against a real local stack (24 documents
took 28 s to purge, versus 3.4 s to add), because `forget()` is per-`data_id`
and re-resolves and re-authorizes the dataset each call.

The residual gap that leaves: a document whose tracking was **lost** rather
than made uncertain (CocoIndex's store deleted or reset, or a destructive
provider-generation bump) reaches reconcile as `prev=[]` while Cognee still
holds its old derivatives; a subsequent content change would then orphan
them. This is a one-off operational event, not a steady-state hazard, and it
has an O(1) recovery that costs nothing in the normal path:

```python
# After losing or resetting the CocoIndex tracking store, purge the dataset's
# derivatives once, then re-run the flow to rebuild them.
await cognee.forget(dataset_id=..., memory_only=True)
```
