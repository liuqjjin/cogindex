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
    data row: explicitly framed upstream as the "re-cognify with different
    settings" primitive.
  - `forget(data_id=…, dataset_id=…)` hard-deletes raw data, dataset link,
    derivatives, and provenance refs.
- Deletion runs through Cognee's provenance delete planner
  (`provenance_delete_planner.py`): artifacts whose source-ref set becomes
  empty are hard-deleted; artifacts still referenced by another
  `(dataset, data)` survive with only the target ref detached. The ordering is
  retry-safe (vectors → detach survivors → delete unowned → orphan cleanup).
  On the default stack (Ladybug graph + LanceDB), provenance refs are folded
  into graph writes atomically. There is no write-then-attach window.

## Decision

`reconcile()` classifies each document into five write actions; the sink
executes them per dataset in a fixed order.

**Create** (no previous record at all):
1. `add([DataItem(data, data_id=…)], dataset_id=…)`: upsert, safe if the row
   already exists from a torn earlier attempt;
2. one incremental `cognify(dataset)` per dataset per batch.

**Replace** (content, external metadata, node-set annotations or processing
fingerprint changed, or previous state is uncertain over a recorded document;
see the amendments below):
1. `forget(data_id=…, dataset_id=…, memory_only=True)`: purge old
   derivatives; harmless if none exist;
2. `add(...)` with the *same* `data_id` and new content;
3. incremental `cognify(dataset)`.

Skipping step 1 leaves old entities alongside new ones; skipping step 2 loses
the document. The sequence is idempotent: each step tolerates the world already
being in its post-state.

**Recreate** (`importance_weight` changed):
1. hard `forget(data_id=…, dataset_id=…)`;
2. `add(...)` with the same `data_id` and desired weight;
3. incremental `cognify(dataset)`.

The hard delete is necessary because Cognee 1.4's existing-row ingestion
branch does not update `importance_weight`. Recreate remains retry-safe:
deleting an absent row succeeds, and any failed attempt leaves both the old
and intended weight fingerprints possible, so the next reconcile recreates
again.

**Metadata update** (`label` alone changed): re-add the document without a
purge or cognify. Identical content preserves its completed pipeline status,
and label does not shape derivatives.

**Hard delete** (desired state is NON_EXISTENCE):
1. `forget(data_id=…, dataset_id=…)`; a missing data item in an existing
   dataset is already a no-op upstream. The runtime also treats an explicit
   `DatasetNotFoundError` as success. It does not swallow
   `UnauthorizedDataAccessError` or a generic `ValueError`: pinned Cognee uses
   the latter for the ambiguous "not found or not accessible" case, so treating
   it as absence could hide an authorization or configuration failure.

**No-op**: all possible previous records equal the desired record and the
previous state cannot be missing.

## Shared-entity provenance

cogindex deliberately does **not** implement its own graph deletion. It always
deletes through `forget`, which routes through the provenance planner, so an
entity supported by two documents survives deletion of one (only its ref is
detached) and is removed when its last supporting document goes. cogindex's
integration tests assert this end-to-end (mirroring upstream's
`test_shared_node_preservation.py`), because it is the property users depend
on, but the mechanism is Cognee's, not ours.

## Batching

Actions are grouped by `(runtime, dataset)`. Within a dataset batch: hard
deletes and recreate-deletes first, then replace-purges, then all adds
(batched), then exactly one `cognify`. Document ids are sorted lexically before
each runtime call so logs and tests are reproducible. Partial failures
propagate; nothing is swallowed. Structured logs record phase and timing only,
never content, never secrets.

## Dataset teardown and unmount semantics (verified against the engine)

When a dataset target stops being declared (its component path disappears),
the engine reconciles the container to non-existence and runs its sink once
more. Engine-verified behavior (tests/unit/test_engine_lifecycle.py):

- **System-managed** (`managed_by="system"`): the container sink calls
  `teardown_dataset`, which deletes the dataset via `forget(dataset_id=...)`,
  including raw data, graph, vector derivatives and the dataset record. The
  sink holds the same
  runtime-provided dataset lock used by document add/replace batches, so a
  whole-dataset teardown cannot interleave with a connector document write.
- **User-managed** (`managed_by="user"`): on target unmount,
  `resolve_system_transition` yields no action and the runtime observes
  **zero mutating calls**. The engine drops child tracking without issuing
  per-document deletes, so documents that cogindex itself added remain in the
  dataset. While the target is still mounted, document reconciliation is
  unchanged: a tracked key that stops being declared is deleted normally.
  This option controls whole-dataset teardown; it is not per-document
  ownership.

A dataset that never materialized (declared but nothing was ever added)
tears down as a no-op: `teardown_dataset` resolves the name, finds no
dataset, and returns.

## Amendment: the add-side skip gate (integration-tier discovery)

`cognee.add()`'s per-item pipeline ALSO has a skip gate, routed whenever
`data_cache or incremental_loading`, and both default to True. A data_id
whose `add_pipeline` status is COMPLETED is then skipped before ingestion
runs: replacement content would silently never be written, because
`forget(memory_only=True)` deliberately resets only `cognify_pipeline`.

The connector therefore always calls
`add(..., incremental_loading=False, data_cache=False)`. Idempotency for
unchanged content is preserved by ingestion's own content-hash comparison
(no pipeline-status reset when the hash is equal, so cognify still skips),
and the cognify-side incremental gate is unaffected. Found by, and pinned in,
`tests/integration/test_local_cognee.py`; the initial code audit
missed the gate's routing condition.

## Amendment: external metadata changes require replacement

The original record split treated `external_metadata` like a label and used a
bare re-add when it changed. Cognee 1.4 uses this metadata during document
classification: `node_set` creates graph membership, and DLT metadata can
change document type and schema edges. A bare re-add updates the relational
row but does not remove or rebuild those derivatives.

`external_metadata` is therefore part of `annotations_fingerprint`. Any change
uses Replace; only `label` remains on the metadata-only path. Record schema
version 2 forces records created under the old classification to rebuild.

## Amendment: importance weight changes require recreation

The original annotations fingerprint combined `node_set` and
`importance_weight`, sending both changes through memory-only Replace. That
protocol preserves the raw Data row. In Cognee 1.4, the existing-row branch of
`ingest_data` updates content and metadata but omits `importance_weight`; only
the new-row branch writes it. Purge + re-add + cognify therefore rebuilds
derivatives from the old weight and can commit a permanently false tracking
state.

`importance_weight` now has a separate fingerprint. A mismatch selects
Recreate, while external metadata and node-set changes continue to use
memory-only Replace. The field was added during the existing record-schema-2
migration and has an empty legacy default: old records remain decodable, but
the sentinel cannot equal a real fingerprint and forces one conservative
recreation. A real local integration test reads the relational Data row and
pins `0.25 → 0.9`.

## Amendment: uncertain state over a recorded document is Replace, not Create

The original classification sent every statediff `insert`/`upsert` down the
Create path. That is wrong whenever a *recorded* document's state cannot be
confirmed, because the hard delete tears in a specific order:
`datasets.delete_data` removes graph and vector derivatives first
(`delete_data_nodes_and_edges`) and the relational row, which carries
`pipeline_status`, last. A crash in between leaves the document present
with **no derivatives and a COMPLETED cognify status**.

The engine then hands the next reconcile `prev=[last record]` with
`prev_may_be_missing=True`. Under the Create path the sink issues only
add + cognify: the add sees unchanged content so it resets no status, the
cognify gate skips the still-COMPLETED item, and the tracking record commits
over a document that will never be cognified again. The next `reconcile()`
returns `None`, a permanent non-convergent fixed point, and one
`verify_dataset` cannot see, since presence, label and completion all match.

The rule is therefore: **`prev_may_be_missing=True` with a non-empty
`prev_possible_records` classifies as Replace.** The extra
`forget(memory_only=True)` is idempotent and a no-op when the state is
intact. Pinned by `tests/unit/test_fault_matrix.py::
test_torn_delete_then_redeclare_rebuilds_derivatives`, which needs the
fake's `inject_fault("delete_documents", torn=True)` to reproduce the
ordering, an atomic delete cannot express this hazard.

**Empty `prev_possible_records` deliberately keeps the Create path**, even
under `prev_may_be_missing=True` (which the engine forces for every fresh
key). Nothing is recorded that could have torn, and purging unconditionally
would cost one `forget()` round trip per document on every first ingest:
measured at ~1.17 s per document against a real local stack (24 documents
took 28 s to purge, versus 3.4 s to add), because `forget()` is per-`data_id`
and re-resolves and re-authorizes the dataset each call.

The residual gap that leaves: a document whose tracking was **lost** rather
than made uncertain (CocoIndex's store deleted or reset, or a destructive
provider-generation bump) reaches reconcile as `prev=[]` while Cognee may
still hold an older version. A fresh create overwrites known source keys, but
it cannot identify a Cognee row whose source document has disappeared.

## Amendment: tracking-store loss requires a hard reset of owned data

An earlier version of this ADR advised a dataset-level
`forget(memory_only=True)` followed by a full run. That is incorrect.
Memory-only forget removes graph and vector derivatives but deliberately keeps
every raw row. A source document deleted before tracking loss therefore
survives as an undeclared row; the fresh run schedules no delete for it and
may cognify it again.

Recovery depends on ownership:

- Stop all writers before any recovery operation.
- If the dataset is exclusively system-managed by this target, hard-empty it
  through the runtime and then run a full sync:

  ```python
  handle = await runtime.resolve_dataset("docs", "default")
  await runtime.teardown_dataset(handle)
  ```

  `teardown_dataset` calls hard dataset-level `forget`, removing raw rows,
  graph data, vectors and the dataset record.
- A shared or user-managed dataset cannot be cleared safely because tracking
  loss also erased per-document ownership. Automatic recovery is not
  available; reconcile it manually or sync into a fresh dataset name.

Cognee's hard dataset path gathers per-row raw-data deletions with
`return_exceptions=True` and logs individual failures without propagating
them. The normal path removes every row, as the integration test verifies, but
an upstream deletion error can still leave an inaccessible orphan row after
the dataset, graph and vectors are gone. The current SDK result gives the
runtime no reliable way to detect that partial cleanup.

`tests/integration/test_local_cognee.py::
test_tracking_loss_recovery_requires_hard_dataset_teardown` pins the
difference between a memory-only purge and hard teardown.
