# Proposal: accept caller-provided `data_id` in the REST add endpoint

**Target:** cognee (audited at `90b4acaa`, applies to released 1.4.0)
**Status:** draft (not yet filed upstream)

## Problem

The Python library supports stable external identity via
`DataItem(data_id=...)`, but the REST `add` endpoint accepts only file
uploads/URLs with no `data_id` field (routers under
`cognee/api/v1/add/routers/`). Confirmed against both the router code and
the published API documentation.

Consequences for anyone integrating over HTTP:

- No idempotent re-ingestion: re-sending updated content creates a **new**
  document (content-derived default id) instead of updating the old one.
- No reliable replace/delete protocol: the caller cannot compute which
  `data_id` to `forget()` without scraping list endpoints and heuristics.
- Library and REST deployments are not feature-equivalent, so integrations
  that work locally cannot be lifted to a served Cognee.

## Proposed change

Accept an optional `data_id` (UUID) per item in the REST add payload and
thread it through to the existing `DataItem` path. For multipart uploads, an
optional parallel `data_ids` array (positionally matched) or a JSON-mode
endpoint would both work; the JSON body variant is the cleaner one.

Validation: reject malformed UUIDs with 422; behavior with a provided
`data_id` must match the library path (upsert row, reset pipeline status).

## What cogindex does today

`RemoteCogneeRuntime` is deliberately read/verify-only: its write path fails
fast at configuration time (ADR-0007) rather than silently producing
duplicate documents. This proposal is what would unlock remote writes.
