# Cognee audit findings (ingestion / cognify / deletion / provenance / locking)

Audited commit: `90b4acaac937dc1c0aeffaead8b707c896ebf3db` (main; release
v1.4.0 is 192 commits behind but — verified via `git show v1.4.0:<path>` —
already contains every capability cogindex depends on: `DataItem.data_id`,
keyword-only `forget()`, the provenance delete planner, the `ladybug` default,
per-item incremental status, and the asyncio dataset lock).

## Identity and ingestion

- `cognee.add()` (`cognee/api/v1/add/add.py`) accepts `DataItem` /
  `list[DataItem]`; `DataItem` (`cognee/tasks/ingestion/data_item.py`) has
  fields `data, label, external_metadata, data_id`. **A provided `data_id`
  overrides the default id** in `ingest_data`.
- Default identity is content-derived:
  `uuid5(NAMESPACE_OID, f"{md5(content)}{user.id}{tenant_id}")`
  (`cognee/modules/data/methods/get_unique_data_id.py`) — an edited document
  becomes a *new* row. Stable identity therefore requires supplying `data_id`.
- **`DataItem` is not exported top-level** — absent from `cognee/__init__.py`
  and every `api` `__init__`. Only importable from
  `cognee.tasks.ingestion.data_item`. (Upstream proposal filed; cogindex
  isolates the import in `_compat.py`.)
- Re-`add` of an existing `data_id`: metadata upserted in place;
  `content_changed` → `pipeline_status = {}` (statuses reset). **Old
  graph/vector derivatives are NOT removed** (`ingest_data.py`); chunk/entity
  ids are content-derived, so the old ones orphan. Clean replacement requires
  `forget(memory_only=True)` first — the core fact behind ADR-0004.
- **CORRECTION (found by the integration tier, missed by the initial code
  audit):** the `ingest_data` upsert branch above is only reachable when the
  ADD pipeline's per-item skip gate does not fire first.
  `run_tasks_data_item()` routes through the incremental path whenever
  `data_cache or incremental_loading` — and **both default to True** on
  `add()`. That path skips any data_id whose `add_pipeline` status is
  COMPLETED before ingestion runs, so replacement content is silently never
  written (memory-only forget deliberately resets only `cognify_pipeline`,
  keeping `add_pipeline` intact). A connector that re-adds under stable
  data_ids MUST call `add(..., incremental_loading=False, data_cache=False)`;
  same-content idempotency is still guaranteed by ingestion's content-hash
  comparison. Empirically verified in
  `tests/integration/test_local_cognee.py` (the replace tests fail against
  the defaults).
- Concurrent identical inserts race on the PK; handled by a single
  `IntegrityError` retry that re-reads and takes the update path. File writes
  are content-addressed (idempotent).

## Cognify and incrementality

- `cognify(datasets=…)` (`cognee/api/v1/cognify/cognify.py`) runs per dataset,
  sequentially, pipeline `"cognify_pipeline"`, with a rollback handler wired.
- Incremental gate is per item
  (`run_tasks_data_item.py::run_tasks_data_item_incremental`):
  skip iff `Data.pipeline_status[pipeline][str(dataset_id)] ==
  "DATA_ITEM_PROCESSING_COMPLETED"`. The status enum has that single value.
- **No public API to cognify specific data ids** — selection is
  dataset-granular; item granularity exists only via the status skip.
- **No configuration fingerprint anywhere in the gate**: changing
  `graph_model`, `chunker`, `custom_prompt`, LLM, or embedding model
  re-processes nothing by itself. Config invalidation must live in the caller
  (cogindex ADR-0005).

## Deletion and provenance

- `cognee.forget()` (`cognee/api/v1/forget/forget.py`, keyword-only, exported
  top-level) is the unified deletion API; `cognee.delete()` is
  `@deprecated` (and `delete/delete.py` is an empty file — the function lives
  in the package `__init__`).
  - `forget(data_id, dataset_id, memory_only=True)` → `_forget_data_memory`:
    deletes the item's graph/vector derivatives, resets only
    `cognify_pipeline[dataset_id]` status, keeps the raw row. The replacement
    primitive.
  - `forget(data_id, dataset_id)` → hard delete via `datasets.delete_data`.
  - `forget(dataset=…, memory_only=True)` → dataset-wide derivative purge,
    framed upstream as "re-cognify with different settings".
- Provenance: every node/edge carries owning source refs
  `sourceref:v1:{dataset_id}:{data_id}`
  (`cognee/infrastructure/databases/provenance/source_refs.py`). On the
  default stack (Ladybug graph), refs are **folded into the graph write
  atomically — no write-then-attach window**
  (`cognee/tasks/storage/add_data_points.py` docstring and code). A
  write-then-attach window exists only on hybrid-write backends (today:
  Neptune Analytics); non-provenance backends (Neo4j, NetworkX, …) use a
  relational rollback ledger written before graph writes.
- Delete planner
  (`cognee/infrastructure/databases/unified/provenance_delete_planner.py`):
  removing ref R from an artifact whose ref set becomes empty → hard delete;
  otherwise the artifact survives with R detached. Ordering is retry-safe:
  delete unowned vectors → detach refs on survivors only → delete unowned
  nodes/edge triples → orphaned EdgeType/NodeSet cleanup. Missing collections
  are no-ops. Unowned artifacts keep their refs until actually deleted, so a
  failed hard-delete is rediscoverable on retry.
- Shared-entity semantics are pinned by upstream tests
  (`cognee/tests/test_shared_node_preservation.py`,
  `cognee/tests/integration/tasks/test_graph_provenance_delete_part2.py`):
  an entity referenced by two documents survives deleting one (ref detached),
  a sole-owned entity and its edges are removed.
- Rollback and recovery: `cognify_rollback_handler`
  (`cognee/modules/cognify/rollback.py`) unwinds a failed run's refs;
  `recovery.py` rolls back stale runs older than
  `COGNEE_STALE_RUN_RECOVERY_MIN_AGE_SECONDS` (default 3600 s) — an explicit
  acknowledgment that multi-process runs share a DB without a lease.
- **Open question tracked**: top-level `forget(data_id=…)` behavior when the
  row is already absent (planner level is idempotent; top level not fully
  traced). cogindex's runtime wraps hard deletes to treat missing-data as
  success and pins that with a contract test against the real SDK.

## Locking (the gap cogindex fills)

`cognee/modules/pipelines/operations/pipeline.py:38-44`:
`_dataset_locks: dict[UUID, asyncio.Lock]` — process-local by declared intent
("does NOT protect against multiple processes/workers … to be replaced by a
cross-process mechanism (e.g. DB-backed lock) later"). No cross-process lock
exists anywhere in the tree. See ADR-0006.

## Multi-tenancy and defaults

- Datasets are owned (`owner_id`, `tenant_id`); ACL types
  read/write/delete/share; dataset-by-name resolves only within the caller's
  authorized set (owner), so cogindex prefers explicit `dataset_id` after
  first resolution. Default user `default_user@example.com` auto-created.
- As of v1.4.0, multi-user backend access control is **enabled by default**
  (startup warning; `ENABLE_BACKEND_ACCESS_CONTROL=false` to disable). With it
  on, each dataset routes to its own database context.
- Zero-config local stack: SQLite (`db_provider="sqlite"`), LanceDB
  (`vector_db_provider="lancedb"`), graph `GRAPH_DATABASE_PROVIDER="ladybug"`
  (Kuzu-lineage embedded engine; NetworkX is an alternative, not the default).
- Default storage paths land inside the installed package directory
  (`…/site-packages/cognee/.cognee_system/databases`) unless configured —
  tests and examples must always set explicit data directories.

## LLM/embedding configuration and how upstream tests avoid real calls

- Env-driven settings, no prefix: `LLM_PROVIDER` (default `openai`),
  `LLM_MODEL` (default `openai/gpt-5-mini`), `LLM_API_KEY`, `LLM_ENDPOINT`,
  per-stage `LLM_EXTRACTION_*`/`LLM_SUMMARIZATION_*`; `EMBEDDING_PROVIDER`
  (default `openai`), `EMBEDDING_MODEL` (default
  `openai/text-embedding-3-large`), `EMBEDDING_DIMENSIONS`.
- There is **no built-in mock LLM provider**. Upstream tests patch
  `LLMGateway.acreate_structured_output` with a deterministic `AsyncMock` and
  use `MockEmbeddingEngine`
  (`cognee/tests/unit/infrastructure/mock_embedding_engine.py`). cogindex's
  no-key integration tier replicates exactly this pattern and labels itself
  "deterministic-LLM integration", never "real E2E".

## Metadata discrepancies

- `pyproject.toml` says `requires-python >=3.10,<3.15`; `AGENTS.md` says
  "< 3.14". Packaging metadata is treated as authoritative.
- Search: no standalone "entity retriever" SearchType exists; entity-level
  retrieval lives inside `HybridRetriever` (BM25 + dense + summary fused with
  RRF in `cognee/modules/retrieval/hybrid/ranking.py`).
