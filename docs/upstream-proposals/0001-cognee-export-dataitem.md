# Proposal: export `DataItem` from the top-level `cognee` package

**Target:** cognee (audited at `90b4acaa`, applies to released 1.4.0)
**Status:** analysis, not filed upstream (see README in this directory)

## Problem

`DataItem` is the only supported way to attach a caller-controlled `data_id`
(and `label`/`external_metadata`) to ingested content:

- Definition: `cognee/tasks/ingestion/data_item.py`
- Accepted by `cognee.add()` (`cognee/api/v1/add/add.py` type union)

But it is **not exported** from `cognee/__init__.py`. Integrators must import
from a deep, internal-looking path:

```python
from cognee.tasks.ingestion.data_item import DataItem  # feels private
```

Stable external identity is the foundation of any incremental integration
(without a caller-provided `data_id`, the default id is content-derived,
`uuid5(NAMESPACE_OID, md5(content) + user + tenant)` in
`cognee/modules/data/methods/get_unique_data_id.py`, so every content edit
creates a new document instead of replacing the old one). A capability this
central should be part of the public API surface.

## Proposed change

Add to `cognee/__init__.py`:

```python
from cognee.tasks.ingestion.data_item import DataItem as DataItem
```

and mention `DataItem(data_id=...)` in the `add()` docstring.

Semver impact: additive, none breaking.

## What cogindex does today

`cogindex._compat` imports `DataItem` from the deep path behind a
capability check (verifying the `data_id` field exists) and fails with an
actionable `CompatibilityError` if the module moves. A public export would
let that shim be deleted.
