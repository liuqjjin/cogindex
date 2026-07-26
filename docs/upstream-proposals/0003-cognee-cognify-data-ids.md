# Proposal: scope a cognify run with `cognify(data_ids=...)`

**Target:** cognee (audited at `90b4acaa`, applies to released 1.4.0)
**Status:** analysis, not filed upstream (see README in this directory)

## Problem

`cognify(datasets=...)` processes whole datasets. Incrementality exists, but
only via the per-item completion gate
(`pipeline_status[pipeline][dataset_id] == DATA_ITEM_PROCESSING_COMPLETED`
checked in `cognee/modules/pipelines/operations/pipeline.py`): every call
still enumerates the dataset's data items and evaluates the gate per item.

A connector that knows *exactly which* `data_id`s changed cannot express
that. cogindex has this information because it drives cognify immediately
after targeted adds and purges. Costs:

- O(dataset) per-item status reads for an O(changed) update, on every batch.
- The connector must trust the status gate rather than being able to state
  its intent ("process these three items"), which is a wider surface for drift.

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
already COMPLETED may still be skipped; the existing gate remains the
correctness backstop, `data_ids` is a scoping hint). Unknown ids: skipped
with a warning, not an error, to keep the call idempotent/retry-safe.

## What cogindex does today

One `cognify(datasets=[dataset_id])` per changed batch, relying on the
completion gate to skip unchanged items. Correct, but it pays the O(dataset)
enumeration cost on every batch. The `freshness` category of `benchmarks/`
measures exactly this: the latency of a single-document change against a
corpus of a given size.
