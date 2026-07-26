"""Runtime abstraction over a Cognee deployment (ADR-0007)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ._spec import CognifyProfile

__all__ = ["CogneeRuntime", "DatasetHandle", "DocumentPayload", "StoredDocument"]


@dataclass(frozen=True)
class DatasetHandle:
    """Reference to a (possibly not-yet-materialized) Cognee dataset.

    ``dataset_id`` is None until the dataset exists: Cognee creates datasets
    implicitly on first ``add()``. There is no public create API.
    """

    name: str
    tenant: str
    dataset_id: uuid.UUID | None = None


@dataclass(frozen=True)
class DocumentPayload:
    """Everything needed to ingest one document into Cognee."""

    data_id: uuid.UUID
    content: str | bytes
    label: str | None = None
    external_metadata: dict[str, Any] | None = None
    node_set: tuple[str, ...] | None = None
    importance_weight: float | None = None


@dataclass(frozen=True)
class StoredDocument:
    """Read-side view of one stored document, as drift verification needs it."""

    data_id: uuid.UUID
    label: str | None
    external_metadata: dict[str, Any] | None
    cognify_complete: bool


@runtime_checkable
class CogneeRuntime(Protocol):
    """What cogindex needs from a Cognee deployment.

    Implementations: :class:`cogindex.LocalCogneeRuntime` (in-process
    library) and :class:`cogindex.testing.FakeCogneeRuntime` (tests).

    Every method is idempotent as observed by the caller (ADR-0003/0004):
    data reported unambiguously as missing is a successful no-op, re-adding
    the same payload converges, and cognify skips already-processed items.
    Authorization and validation errors propagate. The write protocol's
    convergence argument depends on these properties. Dataset teardown and
    document batches must additionally share the lock described in ADR-0006.
    """

    async def resolve_dataset(self, name: str, tenant: str) -> DatasetHandle:
        """Look up the dataset; ``dataset_id`` is None if it does not exist."""
        ...

    async def add_documents(
        self, handle: DatasetHandle, payloads: Sequence[DocumentPayload]
    ) -> DatasetHandle:
        """Ingest documents under their stable data_ids.

        Returns a handle with ``dataset_id`` set (the dataset materializes on
        first add if it did not exist).
        """
        ...

    async def purge_document_memory(
        self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]
    ) -> None:
        """Remove graph/vector derivatives and reset cognify status, keeping
        raw data. Unambiguously missing documents or datasets are a no-op;
        authorization and validation failures propagate."""
        ...

    async def delete_documents(self, handle: DatasetHandle, data_ids: Sequence[uuid.UUID]) -> None:
        """Hard-delete documents (raw data + derivatives). Unambiguously
        missing documents or datasets are a no-op; authorization and validation
        failures propagate."""
        ...

    async def cognify_dataset(self, handle: DatasetHandle, profile: CognifyProfile) -> None:
        """Run incremental cognify over the dataset: items whose pipeline
        status is already complete are skipped by Cognee. Config invalidation
        is cogindex's job; the caller must have purged anything stale."""
        ...

    async def teardown_dataset(self, handle: DatasetHandle) -> None:
        """Remove the dataset and all managed content.

        A successful teardown invalidates ``handle``; resolve the dataset by
        name before any later operation. An unambiguously missing dataset is a
        no-op; authorization and validation failures propagate.
        """
        ...

    async def list_documents(self, handle: DatasetHandle) -> Sequence[StoredDocument]:
        """Read-only view of the dataset's documents for drift verification.
        A freshly resolved missing dataset yields an empty sequence; a stale
        non-empty handle may fail authorization after teardown."""
        ...

    def dataset_lock(self, handle: DatasetHandle) -> AbstractAsyncContextManager[None]:
        """Serialize document batches and teardown per dataset (ADR-0006)."""
        ...
