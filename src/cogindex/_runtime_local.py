"""In-process Cognee runtime (ADR-0007).

Wraps the cognee library installed in this process. All version-sensitive
imports go through :mod:`cogindex._compat`; every method upholds the
idempotency contract documented on :class:`cogindex.CogneeRuntime`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from . import _compat
from ._identity import canonical_join
from ._locks import InProcessLockProvider, LockProvider
from ._runtime import DatasetHandle, DocumentPayload, StoredDocument
from ._spec import CognifyProfile

__all__ = ["CogneePipelineError", "LocalCogneeRuntime"]

logger = logging.getLogger("cogindex.runtime")

_LOCK_SCOPE_PREFIX = "cogindex"


class LocalCogneeRuntime:
    """Cognee running in this process (default local stack).

    Args:
        data_root / system_root: where cognee stores raw data and databases.
            Strongly recommended. Cognee's defaults point inside its own
            installed package directory.
        lock_provider: dataset lock provider; defaults to in-process locks
            (matching cognee's own process-local locking). Use
            PostgresAdvisoryLockProvider for multi-process updaters.
        user: cognee User to act as; None means cognee's default user.

    ``tenant`` on handles is an identity namespace component (ADR-0002), not
    an access-control mechanism: this runtime acts as one cognee user.
    """

    __slots__ = ("_default_user_id", "_lock_provider", "_setup_done", "_user")

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        system_root: str | Path | None = None,
        lock_provider: LockProvider | None = None,
        user: Any | None = None,
    ) -> None:
        _compat.load()
        # Absolutize: cognee's file-URI storage layer rejects relative roots
        # (deep in ingestion, with a misleading error), so normalize here.
        _compat.configure_storage(
            str(Path(data_root).expanduser().resolve()) if data_root is not None else None,
            str(Path(system_root).expanduser().resolve()) if system_root is not None else None,
        )
        self._lock_provider: LockProvider = lock_provider or InProcessLockProvider()
        self._user = user
        self._setup_done = False
        self._default_user_id: uuid.UUID | None = None

    async def _ensure_ready(self) -> None:
        """Lazily create cognee's database structures (idempotent)."""
        if not self._setup_done:
            await _compat.ensure_databases_ready()
            self._setup_done = True

    async def resolve_dataset(self, name: str, tenant: str) -> DatasetHandle:
        await self._ensure_ready()
        compat_info = _compat.load()
        datasets = await compat_info.cognee.datasets.list_datasets(user=self._user)
        for dataset in datasets:
            if dataset.name == name:
                return DatasetHandle(name=name, tenant=tenant, dataset_id=dataset.id)
        return DatasetHandle(name=name, tenant=tenant, dataset_id=None)

    async def add_documents(
        self, handle: DatasetHandle, payloads: Sequence[DocumentPayload]
    ) -> DatasetHandle:
        if not payloads:
            return handle
        await self._ensure_ready()
        compat_info = _compat.load()
        # node_set and importance_weight are add()-call-level parameters in
        # cognee (not DataItem fields), so payloads are grouped by them.
        groups: dict[tuple[tuple[str, ...] | None, float | None], list[DocumentPayload]] = {}
        for payload in payloads:
            groups.setdefault((payload.node_set, payload.importance_weight), []).append(payload)
        for (node_set, importance_weight), group in sorted(
            groups.items(), key=lambda item: repr(item[0])
        ):
            items = [
                compat_info.data_item_cls(
                    data=payload.content,
                    label=payload.label,
                    external_metadata=(
                        dict(payload.external_metadata)
                        if payload.external_metadata is not None
                        else None
                    ),
                    data_id=payload.data_id,
                )
                for payload in group
            ]
            kwargs: dict[str, Any] = {
                # Never let the ADD pipeline's per-item skip gate swallow our
                # payloads: with either incremental_loading=True or
                # data_cache=True (both upstream defaults), a data_id whose
                # add_pipeline status is COMPLETED is skipped entirely,
                # replacement content would silently never be ingested
                # (memory-only purge resets only cognify_pipeline, by
                # upstream design). Idempotency for unchanged content is
                # preserved by ingestion's own content-hash comparison, and
                # cognify keeps its own incremental gate. Verified by the
                # integration replace tests (ADR-0004).
                "incremental_loading": False,
                "data_cache": False,
            }
            if node_set is not None:
                kwargs["node_set"] = list(node_set)
            if importance_weight is not None:
                kwargs["importance_weight"] = importance_weight
            if handle.dataset_id is not None:
                kwargs["dataset_id"] = handle.dataset_id
            if self._user is not None:
                kwargs["user"] = self._user
            result = await compat_info.cognee.add(items, dataset_name=handle.name, **kwargs)
            _raise_on_errored_runs(result, op="add", dataset=handle.name)
        if handle.dataset_id is None:
            # The dataset materialized on first add; learn its id.
            handle = await self.resolve_dataset(handle.name, handle.tenant)
        return handle

    async def purge_document_memory(
        self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]
    ) -> None:
        await self._forget_documents(handle, data_ids, memory_only=True)

    async def delete_documents(self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]) -> None:
        await self._forget_documents(handle, data_ids, memory_only=False)

    async def cognify_dataset(self, handle: DatasetHandle, profile: CognifyProfile) -> None:
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            # Nothing was ever ingested; cognify would fail on a missing
            # dataset and there are no derivatives to build.
            return
        compat_info = _compat.load()
        kwargs: dict[str, Any] = {}
        if profile.graph_model is not None:
            kwargs["graph_model"] = profile.graph_model
        if profile.chunker is not None:
            kwargs["chunker"] = profile.chunker
        if profile.chunk_size is not None:
            kwargs["chunk_size"] = profile.chunk_size
        if profile.custom_prompt is not None:
            kwargs["custom_prompt"] = profile.custom_prompt
        if profile.temporal_cognify:
            kwargs["temporal_cognify"] = True
        if self._user is not None:
            kwargs["user"] = self._user
        result = await compat_info.cognee.cognify(datasets=[handle.dataset_id], **kwargs)
        _raise_on_errored_runs(result, op="cognify", dataset=handle.name)

    async def teardown_dataset(self, handle: DatasetHandle) -> None:
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            return
        compat_info = _compat.load()
        try:
            # Empties the dataset: raw data + graph + vector. The dataset row
            # itself survives, an upstream limitation documented in ADR-0004.
            await compat_info.cognee.forget(dataset_id=handle.dataset_id, user=self._user)
        except compat_info.dataset_missing_errors as exc:
            logger.info(
                "teardown_dataset: dataset %s already absent (%s)",
                handle.name,
                type(exc).__name__,
            )

    async def list_documents(self, handle: DatasetHandle) -> list[StoredDocument]:
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            return []
        compat_info = _compat.load()
        rows = await compat_info.cognee.datasets.list_data(handle.dataset_id, user=self._user)
        documents: list[StoredDocument] = []
        dataset_id_str = str(handle.dataset_id)
        for row in rows:
            pipeline_status = row.pipeline_status or {}
            status = pipeline_status.get(_compat.COGNIFY_PIPELINE_NAME, {}).get(dataset_id_str)
            documents.append(
                StoredDocument(
                    data_id=row.id,
                    label=row.label,
                    external_metadata=(
                        dict(row.external_metadata)
                        if isinstance(row.external_metadata, dict)
                        else None
                    ),
                    cognify_complete=status == _compat.COGNIFY_COMPLETE_STATUS,
                )
            )
        return sorted(documents, key=lambda document: str(document.data_id))

    def dataset_lock(self, handle: DatasetHandle) -> AbstractAsyncContextManager[None]:
        return self._lock_provider.lock(
            canonical_join(_LOCK_SCOPE_PREFIX, handle.tenant, handle.name)
        )

    async def _forget_documents(
        self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID], *, memory_only: bool
    ) -> None:
        if not data_ids:
            return
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            # Dataset never materialized: nothing to purge or delete.
            return
        compat_info = _compat.load()
        already_absent_errors: tuple[type[BaseException], ...] = (
            *compat_info.dataset_missing_errors,
            ValueError,
        )
        # One dataset-database context around the whole batch. cognee opens one
        # per forget() otherwise, and closing it tears down the graph worker on
        # a blocking thread join that dominates the call (see
        # _compat.dataset_database_context). Sequentially, inside one context,
        # is deliberate: running these concurrently is measurably faster and
        # measurably wrong, because the provenance planner's shared-node
        # cleanup races and leaves orphaned type nodes behind.
        async with _compat.dataset_database_context(
            handle.dataset_id, await self._resolve_user_id()
        ):
            for data_id in data_ids:
                try:
                    await compat_info.cognee.forget(
                        data_id=data_id,
                        dataset_id=handle.dataset_id,
                        memory_only=memory_only,
                        user=self._user,
                    )
                except already_absent_errors as exc:
                    # forget() on a missing data_id in an existing dataset
                    # already succeeds upstream. What can raise is the dataset
                    # vanishing concurrently, which upstream signals as
                    # DatasetNotFound / UnauthorizedDataAccess, or as a bare
                    # ValueError out of its dataset-id resolution. All three
                    # mean "already gone", which an idempotent delete treats as
                    # success (ADR-0004), logged for observability.
                    logger.warning(
                        "forget(memory_only=%s) data_id=%s dataset=%s treated as "
                        "already-absent: %s",
                        memory_only,
                        data_id,
                        handle.name,
                        exc,
                    )

    async def _resolve_user_id(self) -> uuid.UUID | None:
        """Id of the acting user, resolved once and cached."""
        if self._user is not None:
            user_id = getattr(self._user, "id", None)
            return user_id if isinstance(user_id, uuid.UUID) else None
        if self._default_user_id is None:
            self._default_user_id = await _compat.default_user_id()
        return self._default_user_id

    async def _ensure_resolved(self, handle: DatasetHandle) -> DatasetHandle:
        if handle.dataset_id is not None:
            return handle
        return await self.resolve_dataset(handle.name, handle.tenant)


class CogneePipelineError(RuntimeError):
    """A cognee pipeline reported errored runs instead of raising."""


def _raise_on_errored_runs(result: Any, *, op: str, dataset: str) -> None:
    """Fail hard when cognee swallows task errors into its result payload.

    cognee's non-incremental pipeline path collects per-item failures as
    PipelineRunErrored entries in the *return value* and does not raise.
    Treating that as success would commit tracking records for writes that
    never happened, which is exactly the false success ADR-0003 forbids.
    """
    errored: list[Any] = []

    def collect(run_info: Any) -> None:
        if run_info is not None and type(run_info).__name__ == "PipelineRunErrored":
            errored.append(getattr(run_info, "payload", run_info))

    entries = getattr(result, "data_ingestion_info", None)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                collect(entry.get("run_info"))
            else:
                collect(entry)
    elif isinstance(result, dict):
        for value in result.values():
            collect(value)
    if errored:
        raise CogneePipelineError(
            f"cognee {op} on dataset {dataset!r} reported "
            f"{len(errored)} errored pipeline run(s): {errored[:3]!r}"
        )
