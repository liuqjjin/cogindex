# ADR-0001: A CocoIndex target connector, not a memoized function

Status: accepted · Date: 2026-07-24

## Context

CocoIndex offers two ways to avoid redundant work: memoized functions
(`@coco.fn(memo=True)`), which skip re-execution when inputs and code are
unchanged, and **target states**, which declare what must exist in an external
system and are reconciled by the engine (create / update / delete / no-op).

A naive integration would call `cognee.add()` + `cognee.cognify()` inside a
memoized function. That is not equivalent, because memoization only prevents
*re-execution*; it knows nothing about the *external state* the execution
produced:

- **No deletion.** When a source document disappears, a memoized function is
  simply never called again. Nothing removes the document's raw data, graph
  nodes, edges, or vectors from Cognee. Target states are owned by their
  declaring component path; when the path or declaration disappears, the
  engine calls `reconcile(key, NON_EXISTENCE, prev, ...)` and cleanup runs,
  even in a later process where the declaring code never executes.
- **No replacement semantics.** Re-adding changed content to Cognee under the
  same `data_id` resets its pipeline status but leaves the previous content's
  graph/vector derivatives in place (verified against Cognee
  `ingest_data.py`). Something must know the *previous* state to clean it up.
  Target tracking records carry exactly that.
- **No failure-state tracking.** If a process dies between the Cognee write
  and CocoIndex's own bookkeeping, memoization either re-runs everything or,
  worse, considers the work done. The target-state protocol persists
  *multiple possible previous records* across a failed sink/commit and
  replays conservatively (`prev_possible_records`, `prev_may_be_missing`).
- **No ownership or conflict detection.** Two flows writing the same Cognee
  document are invisible to memoization; target keys give the engine a stable
  identity to detect ownership transfer and contention.

## Decision

cogindex is implemented as a CocoIndex **custom target connector** using the
public v1 extension points: a `TargetHandler` with a synchronous, I/O-free
`reconcile()`, asynchronous `TargetActionSink`s for all external I/O, and a
two-level container/child design (dataset → documents), registered via
`register_root_target_states_provider`.

## Source of truth

- **Source state** is owned by CocoIndex: the set of currently declared
  documents, their content, and the declared processing configuration.
- **Derived state** is owned by Cognee: raw data rows, chunks, graph
  nodes/edges, vectors, provenance refs, search indexes.
- cogindex's tracking records (persisted by CocoIndex) describe *what cogindex
  last did to Cognee*, never the reverse. Cognee is never consulted during
  `reconcile()`; drift between the two is detected offline by `verify()`
  (ADR-0003).

## Consequences

- Deletion, replacement, and crash recovery become engine-driven instead of
  application-driven.
- cogindex depends on the compiled `cocoindex` wheel (the engine is Rust);
  supported platforms are those with published wheels.
- The connector must obey the target-connector contract: no I/O in
  `reconcile()`, idempotent actions, stable keys without credentials.
