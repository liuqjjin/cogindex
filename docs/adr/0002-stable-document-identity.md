# ADR-0002: Stable document identity, decoupled from content

Status: accepted · Date: 2026-07-24

## Context

Cognee's default data identity is content-derived:
`uuid5(NAMESPACE_OID, f"{md5(content)}{user.id}{tenant_id}")`
(`cognee/modules/data/methods/get_unique_data_id.py`). Under that scheme an
edited document becomes a *new* document, and the old one, with its graph and
vector derivatives, is orphaned rather than replaced.

Cognee's Python API accepts an explicit override:
`DataItem(data=..., data_id=...)` (`cognee/tasks/ingestion/data_item.py`).
When `data_id` is supplied, ingestion upserts the same row and resets the
item's pipeline status if the content changed.

That last sentence held only conditionally, which the integration tier later
established: reaching ingestion at all requires bypassing `add()`'s own
skip gate, or the re-add never happens. ADR-0004's first amendment carries
the correction and the resulting call convention.

## Decision

cogindex always supplies an explicit, deterministic `data_id`:

```
data_id = uuid5(
    COGINDEX_NAMESPACE,
    canonical_join(
        IDENTITY_SCHEMA_VERSION,   # bumped only on breaking identity changes
        runtime_key,               # ContextKey string of the Cognee runtime
        physical_identity_scope,   # resolved user id + active tenant id
        connector_tenant,          # logical tenant in the target key
        dataset_key,               # logical dataset identity
        normalized_external_key,   # caller-provided stable document key
    ),
)
```

`COGINDEX_NAMESPACE` is a fixed UUID constant. `canonical_join` is an
injective encoding (length-prefixed segments), so distinct component tuples can
never collide by concatenation.

**Content never participates in identity.** Content participates only in the
*fingerprints* stored in the tracking record:

- `content_fingerprint`: over the normalized document payload;
- `annotations_fingerprint`: over external metadata and node-set;
- `importance_weight_fingerprint`: over importance weight;
- `metadata_fingerprint`: over the label;
- `processing_fingerprint`: over everything else that affects derivatives
  (ADR-0005).

Content, external metadata, annotations and processing config can change
Cognee derivatives, so a difference forces replacement. Importance weight is
separate because Cognee cannot update it on an existing row; a difference
forces hard recreation instead (ADR-0004). The label can be updated without
extraction.

Fingerprints decide *what action to take*; `data_id` decides *which Cognee row
is acted upon*.

### Identity schema 2 amendment (2026-07-27)

The version 1 implementation passed only the connector tenant into the
identity function. That did not implement the physical ownership scope
described above: `LocalCogneeRuntime` supports only the logical tenant
`"default"`, while different Cognee users or active tenants can use the same
storage roots and dataset name.
Cognee's `Data.id` is a global relational primary key, so two such users
received the same id and the later ingestion could overwrite the first user's
raw row.

`DatasetHandle` now carries a runtime-resolved, non-secret
`identity_scope`. The local runtime derives it from the acting Cognee user's
canonical UUID **and** that user's active `tenant_id`; `None` is encoded as an
explicit no-active-tenant marker. It returns this scope even when the named
dataset does not exist. Document identity and the dataset lock both use the
same value. Custom runtimes must provide an equally stable physical ownership
identity; a URL, connection string or credential is not an identity scope.

This is an incompatible identity change, so `IDENTITY_SCHEMA_VERSION` is 2.
On the next sync under the same acting user and active tenant, the child
tracking record exposes the version 1 id as stale; the replace protocol
hard-deletes it and writes the version 2 id. Changing the acting user or active
tenant at the same time is a different operation: the new scope cannot safely
clean rows owned only by the old scope.

The `ContextKey` binding is therefore part of the deployment contract, not
just a convenient lookup name. A runtime instance pins the user-and-tenant
scope after its first resolution, but that check does not survive a process
restart. Rebinding an existing `ContextKey` to another user or active tenant
while retaining the old CocoIndex tracking state can make a later sync or
unmount act on the *current* same-name dataset rather than the historical one.
Do not rebind it. If a scope must change, first clean up or reconcile the old
scope using the old binding, then use a new `ContextKey` for the new scope.

A deployment that already wrote the same version 1 coordinates through
multiple Cognee users may contain a row that one user overwrote and that is
linked to more than one dataset. No id migration can reconstruct overwritten
content. Stop all writers, hard-teardown each exclusively managed affected
dataset under its owning user, then run a full sync with schema 2. Shared or
user-managed datasets need manual reconciliation instead of automatic
teardown.

## Rejected alternative

Using a content hash as identity (or accepting Cognee's default) was rejected:
a content edit would create a new document and leave the old graph behind,
which is precisely the staleness bug this project exists to prevent.

## Consequences

- `runtime_key` is persisted for ContextProvider lookup, so it is limited to
  1–128 ASCII letters, digits, dots, underscores or hyphens and must start
  with a letter or digit. This admits ordinary logical names while rejecting
  URL and DSN forms. It is still caller-chosen and must not contain an opaque
  credential.
- The caller must provide a stable external key per document (e.g. a relative
  file path). Normalization is deliberately minimal: NFC only, so that two
  Unicode spellings of the same name are one identity, plus rejection of
  empty keys and NUL bytes. Path separators are *not* canonicalized. Keys are
  opaque strings that often are not paths at all (source record ids, URLs),
  and rewriting `\` to `/` inside one would silently merge two distinct keys
  on the platforms where a backslash is a legal filename character. Callers
  that feed in filesystem paths should normalize on their side, as
  `examples/quickstart_live.py` does. Pinned by `tests/unit/test_identity.py`.
- The same logical document declared through the same runtime, Cognee user id,
  active tenant id, connector tenant and dataset always maps to the same
  Cognee row, across processes and machines. That is the precondition for
  idempotent replay.
- Renames are, by design, a delete + create of two identities. Rename
  detection is out of scope.
