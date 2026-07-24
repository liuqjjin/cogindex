"""Two-level CocoIndex target: dataset containers with document children.

This is the heart of cogindex (ADR-0003/0004/0005):

- The root :class:`DatasetHandler` reconciles dataset-level configuration and
  ownership. Its sink resolves the runtime connection and hands the engine a
  :class:`DocumentHandler` for the dataset's documents.
- :class:`DocumentHandler` reconciles individual documents into idempotent
  write ops (upsert / replace / update_metadata / delete) and applies them in
  batches: hard deletes, then derivative purges, then one batched add, then a
  single incremental cognify — under the dataset lock.

``reconcile()`` implementations are synchronous and perform no I/O (the
engine calls them under a lock); every external call lives in action sinks.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Collection, Sequence
from typing import (
    Any,
    Generic,
    Literal,
    NamedTuple,
    TypeAlias,
)

import cocoindex as coco
from cocoindex.connectorkits import statediff
from cocoindex.connectorkits.target import ManagedBy

from ._identity import document_data_id, normalize_external_key
from ._records import DatasetConfigRecord, DocumentRecord
from ._runtime import CogneeRuntime, DatasetHandle, DocumentPayload
from ._spec import (
    CogneeDatasetSpec,
    CogneeDocumentSpec,
    CognifyProfile,
    ProcessingConfig,
    document_record_for,
    processing_config_from_profile,
)

__all__ = [
    "DatasetHandler",
    "DatasetTarget",
    "DocumentHandler",
    "dataset_target",
    "declare_dataset_target",
    "mount_dataset_target",
]

logger = logging.getLogger("cogindex.target")


# =============================================================================
# Document level (children of a dataset)
# =============================================================================

_DocumentOp: TypeAlias = Literal["upsert", "replace", "update_metadata", "delete"]


class _DocumentAction(NamedTuple):
    op: _DocumentOp
    external_key: str
    data_id: uuid.UUID
    # Previously recorded data_ids that no longer match the derived identity
    # (identity schema evolution): hard-deleted so they cannot orphan.
    stale_data_ids: tuple[uuid.UUID, ...]
    payload: DocumentPayload | None  # None only for op == "delete"


def _classify_write(
    diff_action: statediff.DiffAction,
    desired: DocumentRecord,
    prev_records: Collection[DocumentRecord],
    prev_may_be_missing: bool,
) -> _DocumentOp:
    """Map a statediff action onto a Cognee write op (ADR-0004).

    "update_metadata" (re-add without purging derivatives) is only safe when
    every possible previous record matches the desired record on all
    derivative-affecting fields AND no record may be missing — a missing
    record could mean the last cognify never completed, so the ensure path
    (upsert: add + cognify) must run instead.
    """
    if diff_action in ("insert", "upsert"):
        return "upsert"
    derivative_safe = not prev_may_be_missing and all(
        record.data_id == desired.data_id
        and record.content_fingerprint == desired.content_fingerprint
        and record.annotations_fingerprint == desired.annotations_fingerprint
        and record.processing_fingerprint == desired.processing_fingerprint
        and record.schema_version == desired.schema_version
        for record in prev_records
    )
    return "update_metadata" if derivative_safe else "replace"


class DocumentHandler(coco.TargetHandler[CogneeDocumentSpec, DocumentRecord, None]):
    """Handler for one dataset's documents.

    Instances are created by the dataset sink with the connection already
    resolved: they hold the runtime object, never a context key — child
    reconciles and sinks do not touch the ContextProvider. The action sink is
    a bound method, so the engine batches actions per handler instance, i.e.
    per dataset — which is exactly the batching unit cognify wants.
    """

    __slots__ = (
        "_handle",
        "_processing_fingerprint",
        "_profile",
        "_runtime",
        "_runtime_key",
        "_sink",
    )

    def __init__(
        self,
        *,
        runtime: CogneeRuntime,
        runtime_key: str,
        handle: DatasetHandle,
        profile: CognifyProfile,
        processing_fingerprint: str,
    ) -> None:
        self._runtime = runtime
        self._runtime_key = runtime_key
        self._handle = handle
        self._profile = profile
        self._processing_fingerprint = processing_fingerprint
        self._sink: coco.TargetActionSink[_DocumentAction, None] = (
            coco.TargetActionSink.from_async_fn(self._apply)
        )

    def reconcile(
        self,
        key: coco.StableKey,
        desired_state: CogneeDocumentSpec | coco.NonExistenceType,
        prev_possible_records: Collection[DocumentRecord],
        prev_may_be_missing: bool,
        /,
    ) -> coco.TargetReconcileOutput[_DocumentAction, DocumentRecord, None] | None:
        if not isinstance(key, str):
            raise TypeError(f"document key must be str, got {type(key).__name__}")
        external_key = normalize_external_key(key)
        data_id = document_data_id(
            self._runtime_key, self._handle.tenant, self._handle.name, external_key
        )
        stale_data_ids = tuple(
            sorted(
                {r.data_id for r in prev_possible_records if r.data_id != data_id},
                key=str,
            )
        )

        if coco.is_non_existence(desired_state):
            # statediff.diff() would return None for empty prev even when
            # records may be missing. We deviate deliberately: a pending
            # marker without any committed record means an external write may
            # have happened that we cannot rule out. Deletes are idempotent
            # and data_id is derivable from the key alone, so the
            # conservative delete costs at most one no-op call (ADR-0004).
            if not prev_possible_records and not prev_may_be_missing:
                return None
            return coco.TargetReconcileOutput(
                action=_DocumentAction("delete", external_key, data_id, stale_data_ids, None),
                sink=self._sink,
                tracking_record=coco.NON_EXISTENCE,
            )

        desired_record = document_record_for(
            desired_state,
            data_id=data_id,
            processing_fingerprint=self._processing_fingerprint,
        )
        diff_action = statediff.diff(
            statediff.TrackingRecordTransition(
                desired_record, prev_possible_records, prev_may_be_missing
            )
        )
        if diff_action is None:
            return None

        op = _classify_write(
            diff_action, desired_record, prev_possible_records, prev_may_be_missing
        )
        payload = DocumentPayload(
            data_id=data_id,
            content=desired_state.content,
            label=desired_state.label,
            external_metadata=desired_state.external_metadata,
            node_set=desired_state.node_set,
            importance_weight=desired_state.importance_weight,
        )
        return coco.TargetReconcileOutput(
            action=_DocumentAction(op, external_key, data_id, stale_data_ids, payload),
            sink=self._sink,
            tracking_record=desired_record,
        )

    async def _apply(
        self,
        context_provider: coco.ContextProvider,
        actions: Sequence[_DocumentAction],
        /,
    ) -> None:
        del context_provider  # connection was resolved by the dataset sink
        runtime = self._runtime
        handle = self._handle

        delete_ids: set[uuid.UUID] = set()
        purge_ids: set[uuid.UUID] = set()
        payloads: dict[uuid.UUID, DocumentPayload] = {}
        needs_cognify = False
        for action in actions:
            delete_ids.update(action.stale_data_ids)
            if action.op == "delete":
                delete_ids.add(action.data_id)
                continue
            if action.payload is None:
                raise RuntimeError(f"invariant violation: {action.op} action without payload")
            payloads[action.data_id] = action.payload
            if action.op == "replace":
                purge_ids.add(action.data_id)
            if action.op in ("upsert", "replace"):
                needs_cognify = True
        # Never delete an identity we are about to write in the same batch.
        delete_ids -= payloads.keys()

        started = time.monotonic()
        async with runtime.dataset_lock(handle):
            # Order (ADR-0004): removals first so replaced/stale identities
            # cannot linger if a later phase fails; then derivative purges;
            # then one batched add; then a single incremental cognify.
            if delete_ids:
                await runtime.delete_documents(handle, _ordered(delete_ids))
            if purge_ids:
                await runtime.purge_document_memory(handle, _ordered(purge_ids))
            if payloads:
                ordered_payloads = [payloads[data_id] for data_id in _ordered(payloads.keys())]
                handle = await runtime.add_documents(handle, ordered_payloads)
            if needs_cognify:
                await runtime.cognify_dataset(handle, self._profile)
        logger.info(
            "apply dataset=%s tenant=%s deletes=%d purges=%d adds=%d cognify=%s duration_ms=%.0f",
            handle.name,
            handle.tenant,
            len(delete_ids),
            len(purge_ids),
            len(payloads),
            needs_cognify,
            (time.monotonic() - started) * 1000,
        )


def _ordered(data_ids: Collection[uuid.UUID]) -> list[uuid.UUID]:
    """Deterministic application order, for reproducible logs and tests."""
    return sorted(data_ids, key=str)


# =============================================================================
# Dataset level (root container)
# =============================================================================

_DatasetTrackingRecord: TypeAlias = statediff.MutualTrackingRecord[DatasetConfigRecord]


class _DatasetKey(NamedTuple):
    """Root target key. ``runtime_key`` is the ContextKey string — part of
    every document's identity, so renaming it renames every document."""

    runtime_key: str
    tenant: str
    dataset_name: str


class _DatasetAction(NamedTuple):
    key: _DatasetKey
    spec: CogneeDatasetSpec | coco.NonExistenceType
    main_action: statediff.DiffAction | None
    processing_fingerprint: str | None  # None only when spec is NON_EXISTENCE


def _check_dataset_key(key: coco.StableKey) -> _DatasetKey:
    if isinstance(key, tuple) and len(key) == 3:
        runtime_key, tenant, dataset_name = key
        if (
            isinstance(runtime_key, str)
            and isinstance(tenant, str)
            and isinstance(dataset_name, str)
        ):
            return _DatasetKey(runtime_key, tenant, dataset_name)
    raise TypeError(f"dataset key must be (runtime_key, tenant, name) strs, got {key!r}")


class DatasetHandler(
    coco.TargetHandler[CogneeDatasetSpec, _DatasetTrackingRecord, DocumentHandler]
):
    """Root handler for dataset container target states."""

    def reconcile(
        self,
        key: coco.StableKey,
        desired_state: CogneeDatasetSpec | coco.NonExistenceType,
        prev_possible_records: Collection[_DatasetTrackingRecord],
        prev_may_be_missing: bool,
        /,
    ) -> coco.TargetReconcileOutput[_DatasetAction, _DatasetTrackingRecord, DocumentHandler] | None:
        dataset_key = _check_dataset_key(key)

        tracking_record: _DatasetTrackingRecord | coco.NonExistenceType
        processing_fingerprint: str | None = None
        if coco.is_non_existence(desired_state):
            tracking_record = coco.NON_EXISTENCE
        else:
            processing_fingerprint = desired_state.processing.fingerprint()
            tracking_record = statediff.MutualTrackingRecord(
                tracking_record=DatasetConfigRecord(processing_fingerprint=processing_fingerprint),
                managed_by=desired_state.managed_by,
            )

        resolved = statediff.resolve_system_transition(
            statediff.TrackingRecordTransition(
                tracking_record, prev_possible_records, prev_may_be_missing
            )
        )
        main_action = statediff.diff(resolved)

        # Config change: every document's derivatives are stale, but raw data
        # survives — "lossy", not "destructive". Children additionally carry
        # the processing fingerprint in their own records (ADR-0005's dual
        # mechanism), so either signal alone forces the rebuild.
        child_invalidation: Literal["destructive", "lossy"] | None = (
            "lossy" if main_action == "replace" else None
        )

        # Container targets always return an output, even when converged: the
        # sink must run to hand the engine a child handler.
        return coco.TargetReconcileOutput(
            action=_DatasetAction(dataset_key, desired_state, main_action, processing_fingerprint),
            sink=_dataset_action_sink,
            tracking_record=tracking_record,
            child_invalidation=child_invalidation,
        )


async def _apply_dataset_actions(
    context_provider: coco.ContextProvider,
    actions: Sequence[_DatasetAction],
    /,
) -> list[coco.ChildTargetDef[DocumentHandler] | None]:
    outputs: list[coco.ChildTargetDef[DocumentHandler] | None] = []
    for action in actions:
        runtime_obj = context_provider.get(action.key.runtime_key)
        if not isinstance(runtime_obj, CogneeRuntime):
            raise TypeError(
                f"context key {action.key.runtime_key!r} must provide a "
                f"CogneeRuntime, got {type(runtime_obj).__name__}"
            )
        runtime: CogneeRuntime = runtime_obj

        if coco.is_non_existence(action.spec):
            # main_action is None here when ownership resolution said hands
            # off (user-managed data): the dataset's content is left alone.
            if action.main_action == "delete":
                await runtime.teardown_dataset(
                    DatasetHandle(name=action.key.dataset_name, tenant=action.key.tenant)
                )
                logger.info(
                    "teardown dataset=%s tenant=%s",
                    action.key.dataset_name,
                    action.key.tenant,
                )
            outputs.append(None)
            continue

        # Datasets materialize implicitly on first add; "creating" one here
        # is just resolving whether it already exists.
        handle = await runtime.resolve_dataset(action.key.dataset_name, action.key.tenant)
        if action.processing_fingerprint is None:
            raise RuntimeError(
                "invariant violation: live dataset action without processing fingerprint"
            )
        outputs.append(
            coco.ChildTargetDef(
                handler=DocumentHandler(
                    runtime=runtime,
                    runtime_key=action.key.runtime_key,
                    handle=handle,
                    profile=action.spec.profile,
                    processing_fingerprint=action.processing_fingerprint,
                )
            )
        )
    return outputs


_dataset_action_sink = coco.TargetActionSink[_DatasetAction, DocumentHandler].from_async_fn(
    _apply_dataset_actions
)


_root_provider = coco.register_root_target_states_provider(
    "cogindex/cognee/dataset", DatasetHandler()
)


# =============================================================================
# Public API
# =============================================================================


class DatasetTarget(Generic[coco.MaybePendingS], coco.ResolvesTo["DatasetTarget"]):
    """Declares documents into one mounted Cognee dataset."""

    __slots__ = ("_provider",)

    _provider: coco.TargetStateProvider[CogneeDocumentSpec, None, coco.MaybePendingS]

    def __init__(
        self,
        provider: coco.TargetStateProvider[CogneeDocumentSpec, None, coco.MaybePendingS],
    ) -> None:
        self._provider = provider

    def declare_document(
        self: DatasetTarget,
        external_key: str,
        content: str | bytes,
        *,
        label: str | None = None,
        external_metadata: dict[str, Any] | None = None,
        node_set: Sequence[str] | None = None,
        importance_weight: float | None = None,
    ) -> None:
        """Declare that a document with this stable key has this content.

        Same key + changed content reconciles as in-place replacement; a key
        that stops being declared reconciles as deletion. Renaming a key is
        delete + create (ADR-0002).

        Args:
            external_key: stable logical identifier (e.g. relative path or
                source record id). Never derive it from content.
            content: document text or bytes.
            label / external_metadata: benign metadata — changes re-add the
                document without rebuilding graph derivatives.
            node_set / importance_weight: derivative-affecting annotations —
                changes trigger purge + re-cognify (ADR-0005).
        """
        spec = CogneeDocumentSpec(
            content=content,
            label=label,
            external_metadata=external_metadata,
            node_set=tuple(node_set) if node_set is not None else None,
            importance_weight=importance_weight,
        )
        coco.declare_target_state(
            self._provider.target_state(normalize_external_key(external_key), spec)
        )

    def __coco_memo_key__(self) -> object:
        return self._provider.memo_key


def dataset_target(
    runtime: coco.ContextKey[CogneeRuntime] | str,
    name: str,
    *,
    profile: CognifyProfile | None = None,
    processing: ProcessingConfig | None = None,
    tenant: str = "default",
    managed_by: ManagedBy | str = ManagedBy.SYSTEM,
) -> coco.TargetState[DocumentHandler]:
    """Build the container TargetState for a Cognee dataset.

    Use with ``coco.mount_target()``, or the sugar wrappers
    :func:`declare_dataset_target` / :func:`mount_dataset_target`.

    Args:
        runtime: ContextKey (or its key string) under which a
            :class:`CogneeRuntime` is provided. The key string is part of
            every document's stable identity — renaming it renames every
            managed document (ADR-0002).
        name: Cognee dataset name.
        profile: cognify parameters for this dataset.
        processing: override the auto-derived declarative config; when given,
            the caller owns keeping it consistent with ``profile``.
        tenant: identity namespace component (ADR-0002); not an
            access-control mechanism.
        managed_by: "system" lets cogindex tear the dataset's content down
            when the target is unmounted; "user" leaves existing data alone.
    """
    resolved_profile = profile if profile is not None else CognifyProfile()
    resolved_processing = (
        processing if processing is not None else processing_config_from_profile(resolved_profile)
    )
    key = _DatasetKey(
        runtime_key=runtime.key if isinstance(runtime, coco.ContextKey) else runtime,
        tenant=tenant,
        dataset_name=name,
    )
    spec = CogneeDatasetSpec(
        profile=resolved_profile,
        processing=resolved_processing,
        managed_by=ManagedBy(managed_by),
    )
    return _root_provider.target_state(key, spec)


@coco.fn
def declare_dataset_target(
    runtime: coco.ContextKey[CogneeRuntime] | str,
    name: str,
    *,
    profile: CognifyProfile | None = None,
    processing: ProcessingConfig | None = None,
    tenant: str = "default",
    managed_by: ManagedBy | str = ManagedBy.SYSTEM,
) -> DatasetTarget[coco.PendingS]:
    """Declare a dataset target within the current component context.

    Example:
        ```python
        target = await coco.use_mount(
            cogindex.declare_dataset_target, COGNEE, "docs"
        )
        target.declare_document("guide.md", content=text)
        ```
    """
    provider = coco.declare_target_state_with_child(
        dataset_target(
            runtime,
            name,
            profile=profile,
            processing=processing,
            tenant=tenant,
            managed_by=managed_by,
        )
    )
    return DatasetTarget(provider)


async def mount_dataset_target(
    runtime: coco.ContextKey[CogneeRuntime] | str,
    name: str,
    *,
    profile: CognifyProfile | None = None,
    processing: ProcessingConfig | None = None,
    tenant: str = "default",
    managed_by: ManagedBy | str = ManagedBy.SYSTEM,
) -> DatasetTarget[coco.ResolvedS]:
    """Mount a dataset target and return a ready-to-use DatasetTarget.

    Sugar over :func:`dataset_target` + ``coco.mount_target()``.
    """
    provider = await coco.mount_target(
        dataset_target(
            runtime,
            name,
            profile=profile,
            processing=processing,
            tenant=tenant,
            managed_by=managed_by,
        )
    )
    return DatasetTarget(provider)
