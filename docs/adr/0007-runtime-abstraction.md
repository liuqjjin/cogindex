# ADR-0007: Runtime abstraction, local SDK only

Status: accepted · Date: 2026-07-24

## Context

The connector needs a narrow seam between reconcile-plan and Cognee execution,
for three reasons: testability (a deterministic fake), version tolerance (all
Cognee imports in one place), and deployment variety (embedded SDK vs remote
server).

Audited constraints:

- The Python SDK supports explicit stable identity via
  `DataItem(data_id=…)`, but `DataItem` is not exported at the package top
  level (only `cognee.tasks.ingestion.data_item`).
- The REST API (`POST /v1/add`) has **no** parameter for a caller-supplied
  data id; ids are content-derived server-side. Stable identity, the
  foundation of this connector (ADR-0002), cannot be expressed over REST
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

### No remote runtime in 0.1

There is deliberately no REST-backed runtime. A read-only one would be easy
and a writing one would be wrong, so shipping the read-only half alone would
mostly serve to advertise a capability that cannot be completed:

- The write path is blocked upstream, not by us. `POST /v1/add` accepts no
  caller-supplied data id, so every re-ingestion of edited content creates a
  new server-side document instead of replacing the old one. Stable identity
  is the foundation of everything else here (ADR-0002), so a REST writer
  would not be a degraded connector, it would be a broken one.
- Faking identity in metadata is not an option. Cognee's provenance and
  deletion planner key on the real `data_id`; an id smuggled into metadata is
  invisible to them, which would silently break both replace and delete
  while appearing to work.

The path opens if upstream accepts an explicit `data_id` on the add
endpoint, which is filed as `docs/upstream-proposals/0002`.

## Consequences

- One supported path instead of two half-paths. `LocalCogneeRuntime` requires
  the caller to run Cognee in-process, which is the honest cost of stable
  identity today.
- Upgrading Cognee changes exactly one module. `tests/unit/test_compat.py`
  pins the imported surface, so a breaking upstream change fails in CI
  (including the nightly upstream-compatibility job) rather than at runtime.
