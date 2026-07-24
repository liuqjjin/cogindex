"""Test doubles: an in-memory Cognee emulator with fault injection.

:class:`FakeCogneeRuntime` emulates exactly the Cognee semantics cogindex
depends on — implicit dataset creation, data_id-keyed rows, the derivative
lifecycle (including the upstream behavior that re-adding changed content
resets pipeline status but KEEPS stale derivatives, and that the incremental
cognify gate checks completion only, never configuration) — so unit and
property tests can assert convergence without a real Cognee stack.

It is not a Cognee replacement, and tests built on it are never presented as
integration tests (AGENTS.md hard rule #7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from ._identity import canonical_join, fingerprint_content
from ._locks import InProcessLockProvider, LockProvider
from ._runtime import DatasetHandle, DocumentPayload, StoredDocument
from ._spec import CognifyProfile

__all__ = [
    "FakeCogneeRuntime",
    "FakeDataset",
    "FakeDocument",
    "InjectedFault",
]

_FAULT_OPS = frozenset(
    {
        "resolve_dataset",
        "add_documents",
        "purge_document_memory",
        "delete_documents",
        "cognify_dataset",
        "teardown_dataset",
        "dataset_lock",
    }
)


async def _yield_point() -> None:
    """Cooperative yield inside every fake operation.

    Real Cognee calls suspend on I/O; without an explicit yield the fake's
    operations would run atomically on the event loop and concurrency tests
    could never observe interleaving from a broken lock discipline.
    """
    await asyncio.sleep(0)


class InjectedFault(RuntimeError):
    """Raised by FakeCogneeRuntime when a scripted fault fires."""


@dataclasses.dataclass
class FakeDocument:
    """One stored document plus the state of its emulated derivatives.

    ``derived_fragments`` holds the content fingerprints whose graph/vector
    derivatives currently exist. Upstream-faithfully, cognify *adds* the
    current content's derivatives and nothing removes old ones except an
    explicit memory purge — re-adding changed content and cognifying without
    purging leaves orphaned derivatives behind. That accumulation is exactly
    the hazard ADR-0004's replace protocol closes, so the emulator must make
    it observable.
    """

    payload: DocumentPayload
    cognify_complete: bool = False
    derived_fragments: set[str] = dataclasses.field(default_factory=set)
    derived_profile: CognifyProfile | None = None

    @property
    def derivatives_stale(self) -> bool:
        """True if any existing derivative was built from other content."""
        current = fingerprint_content(self.payload.content)
        return bool(self.derived_fragments - {current})


@dataclasses.dataclass
class FakeDataset:
    dataset_id: uuid.UUID
    name: str
    tenant: str
    documents: dict[uuid.UUID, FakeDocument] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _Fault:
    remaining: int
    after_items: int | None
    exc_factory: Callable[[], BaseException]


class FakeCogneeRuntime:
    """In-memory :class:`cogindex.CogneeRuntime` implementation.

    Fault injection::

        runtime.inject_fault("cognify_dataset")                # next call raises
        runtime.inject_fault("add_documents", after_items=2)   # partial batch
        runtime.inject_fault("dataset_lock", times=3)          # next 3 raise

    Every mutating call is appended to ``calls`` (op, dataset name, detail)
    so tests can assert ordering — e.g. purges before adds, one cognify per
    batch. Entries record *attempts*: a scripted fault fires after the call
    is logged but before any state changes — except add_documents, whose
    entry lists exactly the payloads that were applied before the fault.
    """

    def __init__(self, *, lock_provider: LockProvider | None = None) -> None:
        self.datasets: dict[tuple[str, str], FakeDataset] = {}
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self._lock_provider: LockProvider = lock_provider or InProcessLockProvider()
        self._faults: dict[str, list[_Fault]] = {}

    # -- fault scripting ----------------------------------------------------

    def inject_fault(
        self,
        op: str,
        *,
        times: int = 1,
        after_items: int | None = None,
        exc: BaseException | type[BaseException] | None = None,
    ) -> None:
        if op not in _FAULT_OPS:
            raise ValueError(f"unknown op {op!r}; valid: {sorted(_FAULT_OPS)}")
        if after_items is not None and op != "add_documents":
            raise ValueError("after_items only applies to add_documents")
        exc_factory: Callable[[], BaseException]
        if exc is None:

            def exc_factory() -> BaseException:
                return InjectedFault(f"injected fault in {op}")

        elif isinstance(exc, BaseException):
            fixed_exc = exc

            def exc_factory() -> BaseException:
                return fixed_exc

        else:
            exc_factory = exc
        self._faults.setdefault(op, []).append(
            _Fault(remaining=times, after_items=after_items, exc_factory=exc_factory)
        )

    def clear_faults(self) -> None:
        """Drop all queued fault scripts (e.g. one-sync-scoped injection)."""
        self._faults.clear()

    def _next_fault(self, op: str) -> _Fault | None:
        scripts = self._faults.get(op)
        if not scripts:
            return None
        fault = scripts[0]
        fault.remaining -= 1
        if fault.remaining <= 0:
            scripts.pop(0)
        return fault

    def _fire(self, op: str) -> None:
        fault = self._next_fault(op)
        if fault is not None:
            raise fault.exc_factory()

    # -- CogneeRuntime protocol ---------------------------------------------

    async def resolve_dataset(self, name: str, tenant: str) -> DatasetHandle:
        await _yield_point()
        self._fire("resolve_dataset")
        dataset = self.datasets.get((tenant, name))
        return DatasetHandle(
            name=name,
            tenant=tenant,
            dataset_id=dataset.dataset_id if dataset is not None else None,
        )

    async def add_documents(
        self, handle: DatasetHandle, payloads: Sequence[DocumentPayload]
    ) -> DatasetHandle:
        if not payloads:
            return handle
        await _yield_point()
        fault = self._next_fault("add_documents")
        applied: list[str] = []
        dataset = self._ensure_dataset(handle)
        try:
            for index, payload in enumerate(payloads):
                if fault is not None and fault.after_items is not None:
                    if index >= fault.after_items:
                        raise fault.exc_factory()
                elif fault is not None:
                    raise fault.exc_factory()
                self._add_one(dataset, payload)
                applied.append(str(payload.data_id))
        finally:
            self.calls.append(("add_documents", handle.name, tuple(applied)))
        return DatasetHandle(name=handle.name, tenant=handle.tenant, dataset_id=dataset.dataset_id)

    async def purge_document_memory(
        self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]
    ) -> None:
        await _yield_point()
        self.calls.append(("purge_document_memory", handle.name, tuple(str(d) for d in data_ids)))
        self._fire("purge_document_memory")
        dataset = self.datasets.get((handle.tenant, handle.name))
        if dataset is None:
            return
        for data_id in data_ids:
            document = dataset.documents.get(data_id)
            if document is None:
                continue
            document.derived_fragments.clear()
            document.derived_profile = None
            document.cognify_complete = False

    async def delete_documents(self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]) -> None:
        await _yield_point()
        self.calls.append(("delete_documents", handle.name, tuple(str(d) for d in data_ids)))
        self._fire("delete_documents")
        dataset = self.datasets.get((handle.tenant, handle.name))
        if dataset is None:
            return
        for data_id in data_ids:
            dataset.documents.pop(data_id, None)

    async def cognify_dataset(self, handle: DatasetHandle, profile: CognifyProfile) -> None:
        await _yield_point()
        self.calls.append(("cognify_dataset", handle.name, ()))
        self._fire("cognify_dataset")
        dataset = self.datasets.get((handle.tenant, handle.name))
        if dataset is None:
            return
        for data_id in sorted(dataset.documents, key=str):
            document = dataset.documents[data_id]
            # Upstream-faithful incremental gate: completion only. No config
            # comparison — config invalidation is cogindex's job, and this
            # emulation must be able to expose it when cogindex gets it wrong.
            if document.cognify_complete:
                continue
            document.derived_fragments.add(fingerprint_content(document.payload.content))
            document.derived_profile = profile
            document.cognify_complete = True

    async def teardown_dataset(self, handle: DatasetHandle) -> None:
        await _yield_point()
        self.calls.append(("teardown_dataset", handle.name, ()))
        self._fire("teardown_dataset")
        dataset = self.datasets.get((handle.tenant, handle.name))
        if dataset is None:
            return
        # Mirrors upstream empty_dataset: contents removed, dataset row kept.
        dataset.documents.clear()

    async def list_documents(self, handle: DatasetHandle) -> list[StoredDocument]:
        await _yield_point()
        dataset = self.datasets.get((handle.tenant, handle.name))
        if dataset is None:
            return []
        return [
            StoredDocument(
                data_id=data_id,
                label=document.payload.label,
                external_metadata=document.payload.external_metadata,
                cognify_complete=document.cognify_complete,
            )
            for data_id, document in sorted(
                dataset.documents.items(), key=lambda item: str(item[0])
            )
        ]

    def dataset_lock(self, handle: DatasetHandle) -> AbstractAsyncContextManager[None]:
        return self._locked(handle)

    # -- inspection helpers for tests ---------------------------------------

    def dataset(self, tenant: str, name: str) -> FakeDataset | None:
        return self.datasets.get((tenant, name))

    def document(self, tenant: str, name: str, data_id: uuid.UUID) -> FakeDocument | None:
        dataset = self.datasets.get((tenant, name))
        if dataset is None:
            return None
        return dataset.documents.get(data_id)

    def unconverged_documents(
        self, tenant: str, name: str, *, profile: CognifyProfile | None = None
    ) -> list[uuid.UUID]:
        """Documents whose derivatives are absent, stale, or (when ``profile``
        is given) built with a different configuration."""
        dataset = self.datasets.get((tenant, name))
        if dataset is None:
            return []
        result: list[uuid.UUID] = []
        for data_id, document in dataset.documents.items():
            if (
                not document.cognify_complete
                or document.derived_fragments != {fingerprint_content(document.payload.content)}
                or (profile is not None and document.derived_profile != profile)
            ):
                result.append(data_id)
        return sorted(result, key=str)

    # -- internals ----------------------------------------------------------

    def _ensure_dataset(self, handle: DatasetHandle) -> FakeDataset:
        key = (handle.tenant, handle.name)
        dataset = self.datasets.get(key)
        if dataset is None:
            dataset = FakeDataset(
                # Deterministic id: stable across test runs, unique per key.
                dataset_id=uuid.uuid5(
                    uuid.NAMESPACE_OID,
                    canonical_join("fake-dataset", handle.tenant, handle.name),
                ),
                name=handle.name,
                tenant=handle.tenant,
            )
            self.datasets[key] = dataset
        return dataset

    def _add_one(self, dataset: FakeDataset, payload: DocumentPayload) -> None:
        existing = dataset.documents.get(payload.data_id)
        if existing is None:
            dataset.documents[payload.data_id] = FakeDocument(payload=payload)
            return
        content_changed = fingerprint_content(existing.payload.content) != fingerprint_content(
            payload.content
        )
        existing.payload = payload
        if content_changed:
            # Upstream-faithful: status resets so cognify reprocesses, but
            # old derivatives are NOT removed (they go stale/orphaned).
            existing.cognify_complete = False

    @asynccontextmanager
    async def _locked(self, handle: DatasetHandle) -> AsyncIterator[None]:
        self._fire("dataset_lock")
        scope = canonical_join("cogindex", handle.tenant, handle.name)
        async with self._lock_provider.lock(scope):
            self.calls.append(("lock_acquire", handle.name, ()))
            try:
                yield
            finally:
                self.calls.append(("lock_release", handle.name, ()))
