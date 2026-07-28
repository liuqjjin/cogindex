"""Unit tests for ``DatasetHandler.reconcile`` and ``DocumentHandler._apply``.

Part A exercises the dataset container's pure reconcile (ADR-0003/0005):
container outputs even when converged, config-change replace with lossy child
invalidation, ownership-aware deletion, and key validation.

Part B drives ``DocumentHandler._apply`` directly over a
:class:`FakeCogneeRuntime`, with actions produced by real ``reconcile()``
calls, asserting the ADR-0004 batch protocol: hard deletes and recreations,
then purges, then one batched add, then a single incremental cognify, all
under the dataset lock, which is released even on failure.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import cocoindex as coco
import pytest
from cocoindex.connectorkits import statediff
from cocoindex.connectorkits.target import ManagedBy

from cogindex._identity import document_data_id
from cogindex._records import DatasetConfigRecord, DocumentRecord
from cogindex._runtime import CogneeRuntime, DatasetHandle, DocumentPayload
from cogindex._spec import (
    CogneeDatasetSpec,
    CogneeDocumentSpec,
    CognifyProfile,
    ProcessingConfig,
    document_record_for,
)
from cogindex._target import (
    DatasetHandler,
    DocumentHandler,
    _apply_dataset_actions,
    _DocumentAction,
    dataset_target,
)
from cogindex.testing import FakeCogneeRuntime, InjectedFault

# =============================================================================
# Part A: DatasetHandler.reconcile (pure, no I/O)
# =============================================================================

_KEY = ("rt", "default", "ds")
_PUBLIC_RUNTIME_KEY = coco.ContextKey[CogneeRuntime]("dataset-target-validation-runtime")


def _dataset_spec(
    *,
    graph_model_id: str = "model-a",
    managed_by: ManagedBy = ManagedBy.SYSTEM,
) -> CogneeDatasetSpec:
    return CogneeDatasetSpec(
        profile=CognifyProfile(),
        processing=ProcessingConfig(graph_model_id=graph_model_id),
        managed_by=managed_by,
    )


def _prev_dataset_record(
    fingerprint: str, managed_by: ManagedBy = ManagedBy.SYSTEM
) -> statediff.MutualTrackingRecord[DatasetConfigRecord]:
    return statediff.MutualTrackingRecord(
        tracking_record=DatasetConfigRecord(processing_fingerprint=fingerprint),
        managed_by=managed_by,
    )


def test_converged_dataset_still_returns_container_output() -> None:
    handler = DatasetHandler()
    spec = _dataset_spec()
    fingerprint = spec.processing.fingerprint()
    output = handler.reconcile(_KEY, spec, [_prev_dataset_record(fingerprint)], False)
    # Container pattern: the sink must always run to hand out a child handler.
    assert output is not None
    assert output.action.main_action is None
    assert output.child_invalidation is None
    assert output.tracking_record == _prev_dataset_record(fingerprint)


def test_processing_fingerprint_change_replaces_with_lossy_child_invalidation() -> None:
    handler = DatasetHandler()
    spec = _dataset_spec(graph_model_id="model-a")
    other_fingerprint = ProcessingConfig(graph_model_id="model-b").fingerprint()
    assert other_fingerprint != spec.processing.fingerprint()
    output = handler.reconcile(_KEY, spec, [_prev_dataset_record(other_fingerprint)], False)
    assert output is not None
    assert output.action.main_action == "replace"
    assert output.child_invalidation == "lossy"


def test_non_existence_with_system_managed_prev_deletes() -> None:
    handler = DatasetHandler()
    prev = [_prev_dataset_record("fp-old", ManagedBy.SYSTEM)]
    output = handler.reconcile(_KEY, coco.NON_EXISTENCE, prev, False)
    assert output is not None
    assert output.action.main_action == "delete"
    assert coco.is_non_existence(output.tracking_record)


async def test_non_existence_with_user_managed_prev_is_hands_off() -> None:
    handler = DatasetHandler()
    prev_variants: list[list[statediff.MutualTrackingRecord[DatasetConfigRecord]]] = [
        [_prev_dataset_record("fp-old", ManagedBy.USER)],
        [
            _prev_dataset_record("fp-old", ManagedBy.SYSTEM),
            _prev_dataset_record("fp-other", ManagedBy.USER),
        ],
    ]
    for prev in prev_variants:
        output = handler.reconcile(_KEY, coco.NON_EXISTENCE, prev, False)
        assert output is not None
        assert output.action.main_action is None

        class RemovedProvider:
            def get(self, key: str) -> CogneeRuntime:
                raise AssertionError(f"runtime binding {key!r} must not be read")

        outputs = await _apply_dataset_actions(
            cast(coco.ContextProvider, RemovedProvider()),
            [output.action],
        )
        assert outputs == [None]


def test_non_existence_with_empty_prev_is_noop() -> None:
    handler = DatasetHandler()
    for prev_may_be_missing in (False, True):
        output = handler.reconcile(_KEY, coco.NON_EXISTENCE, [], prev_may_be_missing)
        assert output is not None
        assert output.action.main_action is None


def test_user_managed_desired_is_hands_off_but_records_ownership() -> None:
    handler = DatasetHandler()
    spec = _dataset_spec(managed_by=ManagedBy.USER)
    output = handler.reconcile(_KEY, spec, [_prev_dataset_record("fp-old", ManagedBy.SYSTEM)], True)
    assert output is not None
    assert output.action.main_action is None
    record = output.tracking_record
    assert isinstance(record, statediff.MutualTrackingRecord)
    assert record.managed_by == ManagedBy.USER


def test_bad_dataset_keys_raise_type_error() -> None:
    handler = DatasetHandler()
    spec = _dataset_spec()
    with pytest.raises(TypeError):
        handler.reconcile(("a", "b"), spec, [], False)
    with pytest.raises(TypeError):
        handler.reconcile(("a", 1, "c"), spec, [], False)
    with pytest.raises(TypeError):
        handler.reconcile("just-a-string", spec, [], False)


@pytest.mark.parametrize(
    "key",
    [
        ("", "default", "ds"),
        ("rt", "", "ds"),
        ("rt", "default", ""),
        ("rt\x00other", "default", "ds"),
    ],
)
def test_empty_or_nul_dataset_coordinates_raise_value_error(
    key: tuple[str, str, str],
) -> None:
    with pytest.raises(ValueError):
        DatasetHandler().reconcile(key, _dataset_spec(), [], False)


def test_dataset_target_rejects_secret_runtime_keys_without_echoing_them() -> None:
    secret_runtime_key: Any = "postgresql://user:password@db/example"
    with pytest.raises(TypeError) as raw_exc:
        dataset_target(
            secret_runtime_key,
            "docs",
            processing=ProcessingConfig(),
        )

    assert "password" not in str(raw_exc.value)

    wrapped_secret = coco.ContextKey[CogneeRuntime](secret_runtime_key)
    with pytest.raises(ValueError) as wrapped_exc:
        dataset_target(
            wrapped_secret,
            "docs",
            processing=ProcessingConfig(),
        )

    assert "password" not in str(wrapped_exc.value)

    with pytest.raises(ValueError) as handler_exc:
        DatasetHandler().reconcile(
            (secret_runtime_key, "default", "docs"),
            _dataset_spec(),
            [],
            False,
        )

    assert "password" not in str(handler_exc.value)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("profile", object()),
        ("processing", object()),
    ],
)
def test_dataset_target_rejects_invalid_config_types_immediately(
    argument: str,
    value: object,
) -> None:
    kwargs: dict[str, Any] = {argument: value}

    with pytest.raises(TypeError, match=argument):
        dataset_target(_PUBLIC_RUNTIME_KEY, "docs", **kwargs)


# =============================================================================
# Part B: DocumentHandler._apply batching over FakeCogneeRuntime
# =============================================================================

_RUNTIME_KEY = "rt"
_TENANT = "default"
_DATASET = "ds"
_IDENTITY_SCOPE = "fake-default"
_PROCESSING_FP = "processing-fp-1"


class _ControlledLockRuntime(FakeCogneeRuntime):
    """Expose exactly when teardown contends with a document batch."""

    def __init__(self) -> None:
        super().__init__()
        self.add_entered = asyncio.Event()
        self.release_add = asyncio.Event()
        self.teardown_lock_attempted = asyncio.Event()
        self.teardown_lock_acquired = asyncio.Event()
        self.teardown_entered = asyncio.Event()
        self.timeline: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self._gate = asyncio.Lock()
        self._lock_attempts = 0

    async def resolve_dataset(self, name: str, tenant: str) -> DatasetHandle:
        self.resolve_calls.append((name, tenant))
        return await super().resolve_dataset(name, tenant)

    async def add_documents(
        self,
        handle: DatasetHandle,
        payloads: Sequence[DocumentPayload],
    ) -> DatasetHandle:
        self.add_entered.set()
        await self.release_add.wait()
        return await super().add_documents(handle, payloads)

    async def teardown_dataset(self, handle: DatasetHandle) -> None:
        self.timeline.append("teardown_enter")
        self.teardown_entered.set()
        await super().teardown_dataset(handle)

    def dataset_lock(self, handle: DatasetHandle) -> AbstractAsyncContextManager[None]:
        return self._controlled_lock(handle)

    @asynccontextmanager
    async def _controlled_lock(self, handle: DatasetHandle) -> AsyncIterator[None]:
        del handle
        self._lock_attempts += 1
        attempt = self._lock_attempts
        self.timeline.append(f"lock_attempt:{attempt}")
        if attempt == 2:
            self.teardown_lock_attempted.set()
        async with self._gate:
            self.timeline.append(f"lock_acquire:{attempt}")
            if attempt == 2:
                self.teardown_lock_acquired.set()
            try:
                yield
            finally:
                self.timeline.append(f"lock_release:{attempt}")


class _ContextProviderStub:
    def __init__(self, runtime: CogneeRuntime) -> None:
        self._runtime = runtime

    def get(self, key: str) -> CogneeRuntime:
        assert key == _RUNTIME_KEY
        return self._runtime


def _doc_handler(fake: FakeCogneeRuntime) -> DocumentHandler:
    return DocumentHandler(
        runtime=fake,
        runtime_key=_RUNTIME_KEY,
        handle=DatasetHandle(
            name=_DATASET,
            tenant=_TENANT,
            identity_scope=_IDENTITY_SCOPE,
        ),
        profile=CognifyProfile(),
        processing_fingerprint=_PROCESSING_FP,
    )


def _data_id(external_key: str) -> uuid.UUID:
    return document_data_id(
        _RUNTIME_KEY,
        _IDENTITY_SCOPE,
        _TENANT,
        _DATASET,
        external_key,
    )


def _prev_doc_record(external_key: str, spec: CogneeDocumentSpec) -> DocumentRecord:
    return document_record_for(
        spec, data_id=_data_id(external_key), processing_fingerprint=_PROCESSING_FP
    )


def _action_for(
    handler: DocumentHandler,
    external_key: str,
    desired: CogneeDocumentSpec | coco.NonExistenceType,
    prev: list[DocumentRecord],
    prev_may_be_missing: bool,
) -> _DocumentAction:
    output = handler.reconcile(external_key, desired, prev, prev_may_be_missing)
    assert output is not None
    return output.action


async def _run_mixed_batch(fake: FakeCogneeRuntime) -> dict[str, uuid.UUID]:
    """Seed pre-existing docs, then apply a realistic mixed batch.

    Batch: two upserts (u1, u2), one replace (r1: content changed), one
    update_metadata (m1: label changed only), one delete (d1: undeclared).
    Returns the data_ids by external key. ``fake.calls`` contains only the
    batch's calls (seeding calls are cleared).
    """
    handler = _doc_handler(fake)
    handle = DatasetHandle(
        name=_DATASET,
        tenant=_TENANT,
        identity_scope=_IDENTITY_SCOPE,
    )
    old_r1 = CogneeDocumentSpec(content="r1 old content")
    old_m1 = CogneeDocumentSpec(content="m1 content", label="old-label")
    old_d1 = CogneeDocumentSpec(content="d1 content")
    await fake.add_documents(
        handle,
        [
            DocumentPayload(data_id=_data_id("r1"), content="r1 old content"),
            DocumentPayload(data_id=_data_id("m1"), content="m1 content", label="old-label"),
            DocumentPayload(data_id=_data_id("d1"), content="d1 content"),
        ],
    )
    await fake.cognify_dataset(handle, CognifyProfile())
    fake.calls.clear()

    actions = [
        _action_for(handler, "u1", CogneeDocumentSpec(content="u1 content"), [], True),
        _action_for(handler, "u2", CogneeDocumentSpec(content="u2 content"), [], True),
        _action_for(
            handler,
            "r1",
            CogneeDocumentSpec(content="r1 new content"),
            [_prev_doc_record("r1", old_r1)],
            False,
        ),
        _action_for(
            handler,
            "m1",
            CogneeDocumentSpec(content="m1 content", label="new-label"),
            [_prev_doc_record("m1", old_m1)],
            False,
        ),
        _action_for(handler, "d1", coco.NON_EXISTENCE, [_prev_doc_record("d1", old_d1)], False),
    ]
    assert [action.op for action in actions] == [
        "upsert",
        "upsert",
        "replace",
        "update_metadata",
        "delete",
    ]
    await handler._apply(cast(Any, None), actions)
    return {key: _data_id(key) for key in ("u1", "u2", "r1", "m1", "d1")}


async def test_mixed_batch_ordering_and_single_add_and_cognify() -> None:
    fake = FakeCogneeRuntime()
    ids = await _run_mixed_batch(fake)
    ops = [call[0] for call in fake.calls]

    assert ops.count("add_documents") == 1
    assert ops.count("cognify_dataset") == 1
    # ADR-0004 order, bracketed by the dataset lock.
    assert ops.index("lock_acquire") == 0
    assert (
        ops.index("delete_documents")
        < ops.index("purge_document_memory")
        < ops.index("add_documents")
        < ops.index("cognify_dataset")
        < ops.index("lock_release")
    )
    assert ops.index("lock_release") == len(ops) - 1

    delete_call = next(call for call in fake.calls if call[0] == "delete_documents")
    assert delete_call[2] == (str(ids["d1"]),)
    purge_call = next(call for call in fake.calls if call[0] == "purge_document_memory")
    assert purge_call[2] == (str(ids["r1"]),)
    add_call = next(call for call in fake.calls if call[0] == "add_documents")
    expected_add = tuple(sorted((str(ids["u1"]), str(ids["u2"]), str(ids["r1"]), str(ids["m1"]))))
    assert add_call[2] == expected_add


async def test_update_metadata_only_batch_adds_without_cognify_or_purge() -> None:
    fake = FakeCogneeRuntime()
    handler = _doc_handler(fake)
    old_spec = CogneeDocumentSpec(content="m1 content", label="old-label")
    action = _action_for(
        handler,
        "m1",
        CogneeDocumentSpec(content="m1 content", label="new-label"),
        [_prev_doc_record("m1", old_spec)],
        False,
    )
    assert action.op == "update_metadata"
    await handler._apply(cast(Any, None), [action])
    ops = [call[0] for call in fake.calls]
    assert "add_documents" in ops
    assert "cognify_dataset" not in ops
    assert "purge_document_memory" not in ops


async def test_importance_weight_change_hard_deletes_then_recreates() -> None:
    fake = FakeCogneeRuntime()
    handler = _doc_handler(fake)
    data_id = _data_id("weighted")
    old_spec = CogneeDocumentSpec(content="same content", importance_weight=0.25)
    handle = await fake.add_documents(
        DatasetHandle(
            name=_DATASET,
            tenant=_TENANT,
            identity_scope=_IDENTITY_SCOPE,
        ),
        [
            DocumentPayload(
                data_id=data_id,
                content=old_spec.content,
                importance_weight=old_spec.importance_weight,
            )
        ],
    )
    await fake.cognify_dataset(handle, CognifyProfile())
    fake.calls.clear()
    action = _action_for(
        handler,
        "weighted",
        CogneeDocumentSpec(content="same content", importance_weight=0.9),
        [_prev_doc_record("weighted", old_spec)],
        False,
    )
    assert action.op == "recreate"

    await handler._apply(cast(Any, None), [action])

    ops = [call[0] for call in fake.calls]
    assert (
        ops.index("lock_acquire")
        < ops.index("delete_documents")
        < ops.index("add_documents")
        < ops.index("cognify_dataset")
        < ops.index("lock_release")
    )
    assert "purge_document_memory" not in ops
    document = fake.document(_TENANT, _DATASET, data_id)
    assert document is not None
    assert document.payload.importance_weight == 0.9
    assert document.cognify_complete is True


async def test_dataset_teardown_waits_for_document_batch_lock() -> None:
    runtime = _ControlledLockRuntime()
    handler = _doc_handler(runtime)
    document_action = _action_for(
        handler,
        "held",
        CogneeDocumentSpec(content="document batch holds the lock"),
        [],
        True,
    )
    document_task = asyncio.create_task(handler._apply(cast(Any, None), [document_action]))
    await asyncio.wait_for(runtime.add_entered.wait(), timeout=1)

    dataset_output = DatasetHandler().reconcile(
        (_RUNTIME_KEY, _TENANT, _DATASET),
        coco.NON_EXISTENCE,
        [_prev_dataset_record(_PROCESSING_FP, ManagedBy.SYSTEM)],
        False,
    )
    assert dataset_output is not None
    assert dataset_output.action.main_action == "delete"
    provider = cast(coco.ContextProvider, _ContextProviderStub(runtime))
    teardown_task = asyncio.create_task(_apply_dataset_actions(provider, [dataset_output.action]))
    await asyncio.wait_for(runtime.teardown_lock_attempted.wait(), timeout=1)

    acquired_while_document_held_lock = runtime.teardown_lock_acquired.is_set()
    entered_while_document_held_lock = runtime.teardown_entered.is_set()
    runtime.release_add.set()
    document_outputs, teardown_outputs = await asyncio.gather(
        document_task,
        teardown_task,
    )

    assert document_outputs is None
    assert teardown_outputs == [None]
    assert acquired_while_document_held_lock is False
    assert entered_while_document_held_lock is False
    assert runtime.teardown_entered.is_set()
    assert runtime.timeline.index("lock_release:1") < runtime.timeline.index("teardown_enter")
    assert runtime.resolve_calls == [(_DATASET, _TENANT)]


async def test_dataset_teardown_lock_failure_propagates_before_runtime_call() -> None:
    runtime = FakeCogneeRuntime()
    runtime.inject_fault("dataset_lock")
    dataset_output = DatasetHandler().reconcile(
        (_RUNTIME_KEY, _TENANT, _DATASET),
        coco.NON_EXISTENCE,
        [_prev_dataset_record(_PROCESSING_FP, ManagedBy.SYSTEM)],
        False,
    )
    assert dataset_output is not None
    provider = cast(coco.ContextProvider, _ContextProviderStub(runtime))

    with pytest.raises(InjectedFault):
        await _apply_dataset_actions(provider, [dataset_output.action])

    assert all(call[0] != "teardown_dataset" for call in runtime.calls)


async def test_delete_only_batch_only_deletes() -> None:
    fake = FakeCogneeRuntime()
    handler = _doc_handler(fake)
    old_spec = CogneeDocumentSpec(content="d1 content")
    action = _action_for(
        handler, "d1", coco.NON_EXISTENCE, [_prev_doc_record("d1", old_spec)], False
    )
    assert action.op == "delete"
    await handler._apply(cast(Any, None), [action])
    ops = [call[0] for call in fake.calls]
    assert "delete_documents" in ops
    assert "add_documents" not in ops
    assert "cognify_dataset" not in ops
    assert "purge_document_memory" not in ops


async def test_cognify_fault_propagates_but_releases_lock() -> None:
    fake = FakeCogneeRuntime()
    handler = _doc_handler(fake)
    action = _action_for(handler, "u1", CogneeDocumentSpec(content="u1 content"), [], True)
    fake.inject_fault("cognify_dataset")
    with pytest.raises(InjectedFault):
        await handler._apply(cast(Any, None), [action])
    ops = [call[0] for call in fake.calls]
    assert "lock_release" in ops
    assert ops.index("lock_acquire") < ops.index("lock_release")


async def test_mixed_batch_converges_fake_state() -> None:
    fake = FakeCogneeRuntime()
    ids = await _run_mixed_batch(fake)
    assert fake.unconverged_documents(_TENANT, _DATASET) == []
    assert fake.document(_TENANT, _DATASET, ids["d1"]) is None
    for key in ("u1", "u2", "r1", "m1"):
        assert fake.document(_TENANT, _DATASET, ids[key]) is not None
