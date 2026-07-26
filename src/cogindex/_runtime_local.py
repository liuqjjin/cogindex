"""In-process Cognee runtime (ADR-0007).

Wraps the cognee library installed in this process. All version-sensitive
imports go through :mod:`cogindex._compat`; every method upholds the
idempotency contract documented on :class:`cogindex.CogneeRuntime`.
"""

from __future__ import annotations

import logging
import threading
import uuid
import weakref
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
_SUPPORTED_TENANT = "default"


class LocalCogneeRuntime:
    """Cognee running in this process (default local stack).

    Args:
        data_root / system_root: where cognee stores raw data and databases.
            Both are required because Cognee's defaults point inside its own
            installed package directory.
        lock_provider: dataset lock provider; defaults to in-process locks
            (matching cognee's own process-local locking). Use
            PostgresAdvisoryLockProvider for multi-process updaters.
        user: cognee User to act as; None means cognee's default user.

    This runtime accepts only the ``"default"`` connector tenant. Physical
    Cognee tenancy comes from ``user``; accepting additional logical tenant
    names would let two connector identities address the same physical
    dataset without sharing ownership or lock scope.
    """

    __slots__ = (
        "__weakref__",
        "_data_root",
        "_default_user_id",
        "_lock_provider",
        "_setup_done",
        "_system_root",
        "_user",
    )

    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        system_root: str | Path | None = None,
        lock_provider: LockProvider | None = None,
        user: Any | None = None,
    ) -> None:
        normalized_roots = _normalize_storage_roots(data_root, system_root)
        _compat.load()
        self._data_root, self._system_root = normalized_roots
        self._lock_provider: LockProvider = (
            lock_provider if lock_provider is not None else InProcessLockProvider()
        )
        self._user = user
        self._setup_done = False
        self._default_user_id: uuid.UUID | None = None
        with _LOCAL_RUNTIME_CONFIG_LOCK:
            live_runtimes = list(_LIVE_LOCAL_RUNTIMES)
            conflict = next(
                (
                    runtime
                    for runtime in live_runtimes
                    if runtime._storage_roots != normalized_roots
                ),
                None,
            )
            if conflict is not None:
                raise RuntimeError(
                    "Cognee storage configuration is process-global; cannot create a "
                    f"LocalCogneeRuntime for {normalized_roots!r} while another live "
                    f"runtime uses {conflict._storage_roots!r}"
                )
            # Do not reconfigure when a same-root runtime is already alive:
            # doing so would mask an external mutation that every live runtime
            # must detect before its next Cognee operation.
            if not live_runtimes:
                _compat.configure_storage(*normalized_roots)
            _LIVE_LOCAL_RUNTIMES.add(self)

    async def _ensure_ready(self) -> None:
        """Lazily create cognee's database structures (idempotent)."""
        self._assert_storage_roots()
        if not self._setup_done:
            await _compat.ensure_databases_ready()
            self._setup_done = True

    async def resolve_dataset(self, name: str, tenant: str) -> DatasetHandle:
        _validate_dataset_name(name)
        _validate_tenant(tenant)
        await self._ensure_ready()
        compat_info = _compat.load()
        user_id = await self._resolve_user_id()
        if user_id is None:
            raise RuntimeError("cannot resolve the acting Cognee user's id")
        datasets = await compat_info.cognee.datasets.list_datasets(user=self._user)
        owned_matches = [
            dataset
            for dataset in datasets
            if dataset.name == name and getattr(dataset, "owner_id", None) == user_id
        ]
        if len(owned_matches) > 1:
            match_ids = sorted(str(dataset.id) for dataset in owned_matches)
            raise RuntimeError(
                f"multiple Cognee datasets named {name!r} are owned by the acting user: {match_ids}"
            )
        if owned_matches:
            return DatasetHandle(name=name, tenant=tenant, dataset_id=owned_matches[0].id)
        return DatasetHandle(name=name, tenant=tenant, dataset_id=None)

    async def add_documents(
        self, handle: DatasetHandle, payloads: Sequence[DocumentPayload]
    ) -> DatasetHandle:
        self._validate_handle(handle)
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
        self._validate_handle(handle)
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
        self._validate_handle(handle)
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            return
        compat_info = _compat.load()
        try:
            # Hard dataset forget removes raw data, graph, vectors and the
            # dataset row itself. A stale handle is therefore invalid after
            # this call; callers must resolve by name again if they need it.
            await compat_info.cognee.forget(dataset_id=handle.dataset_id, user=self._user)
        except compat_info.dataset_missing_errors as exc:
            logger.info(
                "teardown_dataset: dataset %s already absent (%s)",
                handle.name,
                type(exc).__name__,
            )

    async def list_documents(self, handle: DatasetHandle) -> list[StoredDocument]:
        self._validate_handle(handle)
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
        self._validate_handle(handle)
        return self._lock_provider.lock(
            canonical_join(_LOCK_SCOPE_PREFIX, handle.tenant, handle.name)
        )

    async def _forget_documents(
        self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID], *, memory_only: bool
    ) -> None:
        self._validate_handle(handle)
        if not data_ids:
            return
        await self._ensure_ready()
        handle = await self._ensure_resolved(handle)
        if handle.dataset_id is None:
            # Dataset never materialized: nothing to purge or delete.
            return
        compat_info = _compat.load()
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
                except compat_info.dataset_missing_errors as exc:
                    # forget() on a missing data_id in an existing dataset
                    # already succeeds upstream. Only an explicit
                    # DatasetNotFoundError unambiguously indicates that a
                    # concurrently removed dataset is absent. Pinned cognee
                    # also uses ValueError for
                    # the ambiguous "not found or not accessible" case;
                    # swallowing it (or UnauthorizedDataAccessError) would turn
                    # authorization/configuration failures into false success.
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
        self._validate_handle(handle)
        if handle.dataset_id is not None:
            return handle
        return await self.resolve_dataset(handle.name, handle.tenant)

    @property
    def _storage_roots(self) -> tuple[str, str]:
        return self._data_root, self._system_root

    def _assert_storage_roots(self) -> None:
        effective_roots = _compat.storage_roots()
        if effective_roots != self._storage_roots:
            raise RuntimeError(
                "Cognee's process-global storage roots changed after runtime "
                f"construction: expected {self._storage_roots!r}, found "
                f"{effective_roots!r}"
            )

    def _validate_handle(self, handle: DatasetHandle) -> None:
        _validate_dataset_name(handle.name)
        _validate_tenant(handle.tenant)
        self._assert_storage_roots()


_LIVE_LOCAL_RUNTIMES: weakref.WeakSet[LocalCogneeRuntime] = weakref.WeakSet()
_LOCAL_RUNTIME_CONFIG_LOCK = threading.Lock()


def _normalize_storage_roots(
    data_root: str | Path | None,
    system_root: str | Path | None,
) -> tuple[str, str]:
    if data_root is None or system_root is None:
        raise ValueError("data_root and system_root must both be explicitly supplied")

    raw_roots = (str(data_root), str(system_root))
    labels = ("data_root", "system_root")
    for label, raw_root in zip(labels, raw_roots, strict=True):
        if not raw_root.strip():
            raise ValueError(f"{label} must not be empty or whitespace")
        if "\x00" in raw_root:
            raise ValueError(f"{label} must not contain NUL characters")

    # Cognee's file-URI storage layer rejects relative roots deep in ingestion
    # with a misleading error, so normalize both only after both raw values
    # have passed validation.
    return (
        str(Path(raw_roots[0]).expanduser().resolve()),
        str(Path(raw_roots[1]).expanduser().resolve()),
    )


def _validate_tenant(tenant: str) -> None:
    if tenant != _SUPPORTED_TENANT:
        raise ValueError(
            f"LocalCogneeRuntime supports only tenant {_SUPPORTED_TENANT!r}, got {tenant!r}"
        )


def _validate_dataset_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"dataset name must be str, got {type(name).__name__}")
    if not name:
        raise ValueError("dataset name must be non-empty")
    if "\x00" in name:
        raise ValueError("dataset name must not contain NUL characters")


class CogneePipelineError(RuntimeError):
    """A cognee pipeline reported errored runs instead of raising."""


def _raise_on_errored_runs(result: Any, *, op: str, dataset: str) -> None:
    """Fail hard when cognee swallows task errors into its result payload.

    cognee's non-incremental pipeline path collects per-item failures as
    PipelineRunErrored entries in the *return value* and does not raise.
    Treating that as success would commit tracking records for writes that
    never happened, which is exactly the false success ADR-0003 forbids.
    """
    errored_count = 0

    def collect(run_info: Any) -> None:
        nonlocal errored_count
        if run_info is not None and type(run_info).__name__ == "PipelineRunErrored":
            errored_count += 1

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
    if errored_count:
        raise CogneePipelineError(
            f"cognee {op} on dataset {dataset!r} reported "
            f"{errored_count} errored pipeline run(s); inspect Cognee's own logs for details"
        )
