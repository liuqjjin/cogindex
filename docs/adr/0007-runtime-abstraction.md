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
resolve a dataset, add documents with explicit data ids, forget item memory,
forget an item, cognify a dataset, list stored document status, tear down
managed content, and acquire a dataset-scoped lock.

`resolve_dataset()` returns a `DatasetHandle` containing both the optional
upstream dataset id and a required, non-secret `identity_scope`. The latter
identifies the physical ownership coordinates that own document rows even
when the dataset has not materialized. Identity derivation and locking consume
that same value, so a runtime cannot accidentally isolate one while colliding
the other.

- **`LocalCogneeRuntime`** (supported): drives the Cognee Python SDK.
  All version-sensitive imports live in `cogindex/_compat.py`, which performs
  an explicit capability check at first use (cognee present, version in the
  supported range, `DataItem` importable with a `data_id` field, `forget`
  keyword signature as expected) and raises one actionable error otherwise.
  No scattered private imports; no monkey-patching.
- **`FakeCogneeRuntime`** (tests): deterministic in-memory reference
  implementation with injectable fault points. Focused real-Cognee tests pin
  the upstream behavior that the fake models.

### Local runtime boundaries

Cognee's storage roots are process-global configuration, not instance fields.
`LocalCogneeRuntime` therefore requires both `data_root` and `system_root`.
Two live local runtimes may share the same normalized pair; constructing a
different pair while either is alive fails. Before setup and every direct
operation, the runtime also checks that outside code has not changed the
effective roots.

`cognee.serve()` installs a process-global remote client. Cognee's top-level
`add`, `cognify`, and `forget` functions then route to REST even for an
already-created local runtime. The remote `add` call drops the caller-supplied
`data_id`, so `LocalCogneeRuntime` checks that state before every SDK operation
and raises `CompatibilityError` while remote mode is active. Call
`await cognee.disconnect()` before using the local runtime.

The connector-level `tenant` coordinate is supported by the general protocol,
but `LocalCogneeRuntime` accepts only `"default"`. Physical Cognee tenancy is
selected by its `user` object. The local handle's identity scope is a
canonical encoding of the user's UUID and active `tenant_id`, with an explicit
marker when no tenant is active. Letting two arbitrary connector tenant
strings point to the same physical dataset would still give one physical
resource multiple names.

Dataset lookup matches both `owner_id` and `tenant_id`. A shared dataset or a
same-name dataset in another active tenant is not selected implicitly, and
duplicate matches fail instead of using list order. This keeps a name-only
declaration from writing into a dataset merely shared with the acting user or
owned by that user in another tenant.

Before every user-scoped SDK operation, the runtime refreshes the configured
user, compares both ownership coordinates with the resolved handle, and passes
that exact `User` object to Cognee. The runtime is pinned to the first resolved
scope, so changing the default user or changing an explicit user's active
tenant between operations fails before the SDK mutation. Cognee's active
tenant is mutable global database state, however; callers must not switch it
concurrently with a running sync. No connector-side check can close a change
that occurs after validation but before Cognee re-reads the user internally.

Hard dataset teardown deletes the dataset record. A handle containing its old
UUID is invalid afterwards; callers that continue must resolve the dataset by
name and use the fresh handle. The runtime does not treat an authorization
error from a stale UUID as proof that the dataset is absent.

Handles are obtained from `resolve_dataset()`, not assembled from a dataset
name. This is especially important before teardown: resolution must happen
before acquiring the lock so the lock uses the acting user's user-and-tenant
scope.

An existing runtime instance detects scope drift, but CocoIndex persists the
`ContextKey` name rather than a live runtime binding. Across a restart,
reusing the same key for a different user or active tenant is therefore not
detectable. With old tracking state still present, synchronization or unmount
can act on the current scope's same-name dataset. Keep a key bound to one
scope; clean up the old scope under its old binding and choose a new
`ContextKey` when switching.

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
- Local storage and tenant restrictions are explicit rather than treating
  process-global Cognee configuration as instance or tenant isolation.
- Upgrading Cognee changes exactly one module. `tests/unit/test_compat.py`
  pins the imported surface, so a breaking upstream change fails in CI
  (including the nightly upstream-compatibility job) rather than at runtime.
