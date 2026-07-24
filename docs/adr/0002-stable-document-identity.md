# ADR-0002: Stable document identity, decoupled from content

Status: accepted · Date: 2026-07-24

## Context

Cognee's default data identity is content-derived:
`uuid5(NAMESPACE_OID, f"{md5(content)}{user.id}{tenant_id}")`
(`cognee/modules/data/methods/get_unique_data_id.py`). Under that scheme an
edited document becomes a *new* document, and the old one — with its graph and
vector derivatives — is orphaned rather than replaced.

Cognee's Python API accepts an explicit override:
`DataItem(data=..., data_id=...)` (`cognee/tasks/ingestion/data_item.py`).
When `data_id` is supplied, re-adding is an upsert on the same row and resets
the item's pipeline status when content changed.

## Decision

cogindex always supplies an explicit, deterministic `data_id`:

```
data_id = uuid5(
    COGINDEX_NAMESPACE,
    canonical_join(
        IDENTITY_SCHEMA_VERSION,   # bumped only on breaking identity changes
        runtime_key,               # ContextKey string of the Cognee runtime
        tenant_or_user_identity,   # stable user/tenant scope
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

- `content_fingerprint` — over the normalized document payload;
- `metadata_fingerprint` — over label / external metadata / node-set /
  importance weight;
- `processing_fingerprint` — over everything that affects derivatives
  (ADR-0005).

Fingerprints decide *what action to take*; `data_id` decides *which Cognee row
is acted upon*.

## Rejected alternative

Using a content hash as identity (or accepting Cognee's default) was rejected:
a content edit would create a new document and leave the old graph behind,
which is precisely the staleness bug this project exists to prevent.

## Consequences

- The caller must provide a stable external key per document (e.g. a relative
  file path). Key normalization (NFC, path-separator canonicalization) is part
  of the identity contract and covered by unit tests.
- The same logical document declared through the same runtime/tenant/dataset
  always maps to the same Cognee row, across processes and machines — the
  precondition for idempotent replay.
- Renames are, by design, a delete + create of two identities. Rename
  detection is out of scope.
