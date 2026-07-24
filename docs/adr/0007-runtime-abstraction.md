# ADR-0007: Runtime abstraction — local SDK first, REST honestly limited

Status: accepted · Date: 2026-07-24

## Context

The connector needs a narrow seam between reconcile-plan and Cognee execution,
for three reasons: testability (a deterministic fake), version tolerance (all
Cognee imports in one place), and deployment variety (embedded SDK vs remote
server).

Audited constraints:

- The Python SDK supports explicit stable identity via
  `DataItem(data_id=…)` — but `DataItem` is not exported at the package top
  level (only `cognee.tasks.ingestion.data_item`).
- The REST API (`POST /v1/add`) has **no** parameter for a caller-supplied
  data id; ids are content-derived server-side. Stable identity — the
  foundation of this connector (ADR-0002) — cannot be expressed over REST
  today.

## Decision

A `CogneeRuntime` Protocol exposes only the operations the connector needs:
ensure/resolve dataset, upsert documents with explicit data ids, forget item
memory, forget item, cognify dataset, inspect status/provenance, optional
search, and a dataset-scoped lock hook.

- **`LocalCogneeRuntime`** (supported): drives the Cognee Python SDK.
  All version-sensitive imports live in `cogindex/_compat.py`, which performs
  an explicit capability check at first use (cognee present, version in the
  supported range, `DataItem` importable with a `data_id` field, `forget`
  keyword signature as expected) and raises one actionable error otherwise.
  No scattered private imports; no monkey-patching.
- **`FakeCogneeRuntime`** (tests): deterministic in-memory reference
  implementation with injectable fault points; the contract test suite runs
  against both Fake and Local to keep them honest.
- **`RemoteCogneeRuntime`** (experimental, read/verify only): may resolve
  datasets, list data, and search against a Cognee server. Every write-path
  method raises `UnsupportedCapabilityError` at *configuration* time — fail
  fast, not at first write. We do not emulate stable ids by stuffing them
  into metadata: identity faked in metadata is invisible to Cognee's
  provenance and deletion planner, which would silently break replace and
  delete. The write path opens only if/when upstream accepts an explicit
  `data_id` on the add endpoint (see `docs/upstream-proposals/`).

## Consequences

- Users get one supported, fully-featured path (local SDK) and one clearly
  fenced experimental path, instead of a REST mode that corrupts on write.
- Upgrading Cognee changes exactly one module; compatibility tests pin the
  imported surface so a breaking upstream change fails loudly in CI
  (including the nightly upstream-compatibility job) rather than at runtime.
