# Upstream proposals

Four gaps that a full-repository audit of cocoindex and cognee turned up, each
one something cogindex currently works around and would delete code to get.
They are written as proposals rather than complaints: every one names the file,
says what it would take, and states what cogindex does in the meantime.

| # | Target | Ask | What it would remove from cogindex |
|---|---|---|---|
| [0001](0001-cognee-export-dataitem.md) | cognee | export `DataItem` from the package root | a deep import behind a capability check in `_compat.py` |
| [0002](0002-cognee-rest-add-data-id.md) | cognee | accept a caller-supplied `data_id` in the REST add endpoint | the reason there is no remote runtime at all |
| [0003](0003-cognee-cognify-data-ids.md) | cognee | `cognify(data_ids=...)` to scope a run | an O(dataset) status scan on every batch |
| [0004](0004-cognee-pluggable-dataset-lock.md) | cognee | a pluggable cross-process dataset lock | `LockProvider`, or at least its reason for existing |

Two of these are worth reading even if you never use cogindex, because they
document behaviour that is easy to get wrong:

- 0002 explains why an integration built on the REST API cannot do in-place
  replacement at all. Content-derived ids mean an edited document is a new
  document.
- 0004 quotes cognee's own comment acknowledging that its dataset lock does not
  protect against multiple processes.

## Status

These are not filed upstream, and are kept here as design documentation rather
than as pending submissions. Their job in this repository is to answer "why
doesn't cogindex just do X", where X is blocked by something outside it: a
reader who wonders why there is no remote runtime gets 0002 instead of an
apology.

Each one is written against the commits pinned in
[`UPSTREAM_LOCK.json`](../../UPSTREAM_LOCK.json), and the audit finding it
rests on is in [`../upstream-audit/`](../upstream-audit/) with the file and
line it came from. Anyone who does want to take one upstream should re-check
the claim against current `main` first, since cognee moves quickly; the ledger
is what makes that cheap.
