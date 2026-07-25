"""Unit tests for ``DatasetHandler.reconcile`` and ``DocumentHandler._apply``.

Part A exercises the dataset container's pure reconcile (ADR-0003/0005):
container outputs even when converged, config-change replace with lossy child
invalidation, ownership-aware deletion, and key validation.

Part B drives ``DocumentHandler._apply`` directly over a
:class:`FakeCogneeRuntime`, with actions produced by real ``reconcile()``
calls, asserting the ADR-0004 batch protocol: deletes, then purges, then one
batched add, then a single incremental cognify: all under the dataset lock,
which is released even on failure.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import cocoindex as coco
import pytest
from cocoindex.connectorkits import statediff
from cocoindex.connectorkits.target import ManagedBy

from cogindex._identity import document_data_id
from cogindex._records import DatasetConfigRecord, DocumentRecord
from cogindex._runtime import DatasetHandle, DocumentPayload
from cogindex._spec import (
    CogneeDatasetSpec,
    CogneeDocumentSpec,
    CognifyProfile,
    ProcessingConfig,
    document_record_for,
)
from cogindex._target import DatasetHandler, DocumentHandler, _DocumentAction
from cogindex.testing import FakeCogneeRuntime, InjectedFault

# =============================================================================
# Part A: DatasetHandler.reconcile (pure, no I/O)
# =============================================================================

_KEY = ("rt", "default", "ds")


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


def test_non_existence_with_user_managed_prev_is_hands_off() -> None:
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


# =============================================================================
# Part B: DocumentHandler._apply batching over FakeCogneeRuntime
# =============================================================================

_RUNTIME_KEY = "rt"
_TENANT = "default"
_DATASET = "ds"
_PROCESSING_FP = "processing-fp-1"


def _doc_handler(fake: FakeCogneeRuntime) -> DocumentHandler:
    return DocumentHandler(
        runtime=fake,
        runtime_key=_RUNTIME_KEY,
        handle=DatasetHandle(name=_DATASET, tenant=_TENANT),
        profile=CognifyProfile(),
        processing_fingerprint=_PROCESSING_FP,
    )


def _data_id(external_key: str) -> uuid.UUID:
    return document_data_id(_RUNTIME_KEY, _TENANT, _DATASET, external_key)


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
    handle = DatasetHandle(name=_DATASET, tenant=_TENANT)
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
