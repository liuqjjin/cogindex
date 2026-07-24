# Proposal: `cognify(data_ids=...)` — scoped incremental processing

**Target:** cognee (audited at `90b4acaa`, applies to released 1.4.0)
**Status:** draft (not yet filed upstream)

## Problem

`cognify(datasets=...)` processes whole datasets. Incrementality exists, but
only via the per-item completion gate
(`pipeline_status[pipeline][dataset_id] == DATA_ITEM_PROCESSING_COMPLETED`
checked in `cognee/modules/pipelines/operations/pipeline.py`): every call
still enumerates the dataset's data items and evaluates the gate per item.

A connector that knows *exactly which* `data_id`s changed (cogindex always
does — it drives cognify right after targeted adds/purges) cannot express
that. Costs:

- O(dataset) per-item status reads for an O(changed) update, on every batch.
- The connector must trust the status gate rather than being able to state
  its intent ("process these three items") — a wider surface for drift.

## Proposed change

```python
async def cognify(
    datasets: str | list[str] | list[UUID] = None,
    *,
    data_ids: list[UUID] | None = None,   # NEW: requires a single dataset
    ...
)
```

Semantics: when `data_ids` is given, restrict the run to those items (items
already COMPLETED may still be skipped — the existing gate remains the
correctness backstop, `data_ids` is a scoping hint). Unknown ids: skipped
with a warning, not an error, to keep the call idempotent/retry-safe.

## What cogindex does today

One `cognify(datasets=[dataset_id])` per changed batch, relying on the
completion gate to skip unchanged items. Correct, but pays the O(dataset)
enumeration cost per batch; benchmark M11 measures exactly this overhead.
