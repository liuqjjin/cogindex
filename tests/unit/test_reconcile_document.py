"""DocumentHandler.reconcile decision matrix (pure, no engine).

Covers the ADR-0003/0004/0005 write-op classification: upsert, replace,
recreate, update_metadata and delete, plus the deliberate statediff
deviations, stale identity collection, key validation, and payload/tracking
record contents.
"""

from __future__ import annotations

import uuid
from typing import Any

import cocoindex as coco
import pytest

import cogindex
from cogindex import CognifyProfile, DatasetHandle, DocumentPayload
from cogindex._records import DocumentRecord
from cogindex._spec import CogneeDocumentSpec, document_record_for
from cogindex._target import DocumentHandler, _DocumentAction
from cogindex.testing import FakeCogneeRuntime

RUNTIME_KEY = "rt"
TENANT = "default"
DATASET = "ds"
PFP = "pfp"
KEY = "docs/guide.md"


def make_handler(processing_fingerprint: str = PFP) -> DocumentHandler:
    return DocumentHandler(
        runtime=FakeCogneeRuntime(),
        runtime_key=RUNTIME_KEY,
        handle=DatasetHandle(name=DATASET, tenant=TENANT),
        profile=CognifyProfile(),
        processing_fingerprint=processing_fingerprint,
    )


def make_spec(
    content: str = "hello",
    *,
    label: str | None = None,
    external_metadata: dict[str, Any] | None = None,
    node_set: tuple[str, ...] | None = None,
    importance_weight: float | None = None,
) -> CogneeDocumentSpec:
    return CogneeDocumentSpec(
        content=content,
        label=label,
        external_metadata=external_metadata,
        node_set=node_set,
        importance_weight=importance_weight,
    )


def data_id_for(key: str) -> uuid.UUID:
    return cogindex.document_data_id(RUNTIME_KEY, TENANT, DATASET, key)


def record_for(
    spec: CogneeDocumentSpec, *, key: str = KEY, processing_fingerprint: str = PFP
) -> DocumentRecord:
    return document_record_for(
        spec, data_id=data_id_for(key), processing_fingerprint=processing_fingerprint
    )


def payload_for(spec: CogneeDocumentSpec, *, key: str = KEY) -> DocumentPayload:
    return DocumentPayload(
        data_id=data_id_for(key),
        content=spec.content,
        label=spec.label,
        external_metadata=spec.external_metadata,
        node_set=spec.node_set,
        importance_weight=spec.importance_weight,
    )


def reconcile(
    handler: DocumentHandler,
    desired: CogneeDocumentSpec | coco.NonExistenceType,
    prev: list[DocumentRecord],
    missing: bool,
    *,
    key: str = KEY,
) -> coco.TargetReconcileOutput[_DocumentAction, DocumentRecord, None] | None:
    return handler.reconcile(key, desired, prev, missing)


# -- write-op classification --------------------------------------------------


def test_fresh_document_upserts_with_converged_tracking_record() -> None:
    # No previous record means nothing is recorded that a torn delete could
    # have left half-removed, so the create path skips the purge, which on
    # real Cognee costs one forget() round trip per document.
    spec = make_spec()
    output = reconcile(make_handler(), spec, [], True)
    assert output is not None
    assert output.action.op == "upsert"
    assert output.tracking_record == document_record_for(
        spec, data_id=data_id_for(KEY), processing_fingerprint=PFP
    )


def test_no_prev_and_not_missing_is_noop() -> None:
    # statediff semantics; the engine never produces this combination.
    assert reconcile(make_handler(), make_spec(), [], False) is None


def test_converged_record_is_noop() -> None:
    spec = make_spec()
    assert reconcile(make_handler(), spec, [record_for(spec)], False) is None


def test_same_record_but_possibly_missing_replaces() -> None:
    # A recorded document whose state we cannot confirm is ADR-0004's Replace
    # trigger, not a create. A torn hard delete removes derivatives before the
    # row and its COMPLETED cognify status, so add+cognify alone would commit
    # this record over a document that has no derivatives and that no later
    # reconcile would revisit. Pinned end to end by
    # test_fault_matrix.py::test_torn_delete_then_redeclare_rebuilds_derivatives.
    spec = make_spec()
    output = reconcile(make_handler(), spec, [record_for(spec)], True)
    assert output is not None
    assert output.action.op == "replace"


def test_content_change_replaces() -> None:
    spec = make_spec("new content")
    prev = record_for(make_spec("old content"))
    output = reconcile(make_handler(), spec, [prev], False)
    assert output is not None
    assert output.action.op == "replace"
    assert output.action.payload == payload_for(spec)


def test_metadata_only_change_updates_metadata() -> None:
    spec = make_spec(label="new label")
    prev = record_for(make_spec(label="old label"))
    assert prev.metadata_fingerprint != spec.metadata_fingerprint
    assert prev.content_fingerprint == spec.content_fingerprint
    assert prev.annotations_fingerprint == spec.annotations_fingerprint
    output = reconcile(make_handler(), spec, [prev], False)
    assert output is not None
    assert output.action.op == "update_metadata"
    assert output.action.payload == payload_for(spec)


def test_external_metadata_change_replaces() -> None:
    spec = make_spec(external_metadata={"node_set": ["new"]})
    prev = record_for(make_spec(external_metadata={"node_set": ["old"]}))
    assert prev.annotations_fingerprint != spec.annotations_fingerprint
    assert prev.metadata_fingerprint == spec.metadata_fingerprint

    output = reconcile(make_handler(), spec, [prev], False)

    assert output is not None
    assert output.action.op == "replace"
    assert output.action.payload == payload_for(spec)


def test_importance_weight_change_recreates() -> None:
    spec = make_spec(importance_weight=0.9)
    prev = record_for(make_spec(importance_weight=0.25))
    assert prev.importance_weight_fingerprint != spec.importance_weight_fingerprint
    assert prev.annotations_fingerprint == spec.annotations_fingerprint

    output = reconcile(make_handler(), spec, [prev], False)

    assert output is not None
    assert output.action.op == "recreate"
    assert output.action.payload == payload_for(spec)


def test_record_without_weight_fingerprint_recreates_once() -> None:
    spec = make_spec(importance_weight=0.9)
    prev = DocumentRecord(
        data_id=data_id_for(KEY),
        content_fingerprint=spec.content_fingerprint,
        annotations_fingerprint=spec.annotations_fingerprint,
        metadata_fingerprint=spec.metadata_fingerprint,
        processing_fingerprint=PFP,
    )
    assert prev.importance_weight_fingerprint == ""

    output = reconcile(make_handler(), spec, [prev], False)

    assert output is not None
    assert output.action.op == "recreate"


@pytest.mark.parametrize("missing", [False, True])
def test_failed_weight_recreate_retries_as_recreate(missing: bool) -> None:
    old = record_for(make_spec(importance_weight=0.25))
    spec = make_spec(importance_weight=0.9)
    attempted = record_for(spec)

    output = reconcile(make_handler(), spec, [old, attempted], missing)

    assert output is not None
    assert output.action.op == "recreate"


def test_metadata_only_retry_after_failed_update_still_updates_metadata() -> None:
    # The engine transition upstream pins in
    # test_prev_may_be_missing_after_failed_update: a failed metadata update
    # leaves both the committed and the attempted record as possible states,
    # with prev_may_be_missing=False. Both agree on every
    # derivative-affecting field, so the retry must stay metadata-only,
    # escalating to replace here would rebuild the whole graph for a label.
    old = record_for(make_spec(label="old label"))
    spec = make_spec(label="new label")
    attempted = record_for(spec)
    assert old.content_fingerprint == attempted.content_fingerprint
    output = reconcile(make_handler(), spec, [old, attempted], False)
    assert output is not None
    assert output.action.op == "update_metadata"


def test_metadata_only_change_with_missing_prev_escalates_to_replace() -> None:
    # Conservative escalation: a missing record may mean cognify never completed.
    spec = make_spec(label="new label")
    prev = record_for(make_spec(label="old label"))
    output = reconcile(make_handler(), spec, [prev], True)
    assert output is not None
    assert output.action.op == "replace"


def test_processing_fingerprint_change_replaces() -> None:
    # Config invalidation (ADR-0005): same spec, different processing config.
    spec = make_spec()
    prev = record_for(spec, processing_fingerprint="old-pfp")
    output = reconcile(make_handler("new-pfp"), spec, [prev], False)
    assert output is not None
    assert output.action.op == "replace"
    assert output.tracking_record == document_record_for(
        spec, data_id=data_id_for(KEY), processing_fingerprint="new-pfp"
    )


def test_ambiguous_prev_records_replace() -> None:
    # Two possible previous records, one differing in content: not derivative-safe.
    spec = make_spec()
    prev = [record_for(spec), record_for(make_spec("other content"))]
    output = reconcile(make_handler(), spec, prev, False)
    assert output is not None
    assert output.action.op == "replace"


def test_stale_data_id_is_collected_and_replaced() -> None:
    # Identity schema evolution: a previously recorded data_id no longer
    # matches the derived one and must be hard-deleted.
    spec = make_spec()
    stale_id = data_id_for("some/other-key.md")
    assert stale_id != data_id_for(KEY)
    prev = document_record_for(spec, data_id=stale_id, processing_fingerprint=PFP)
    output = reconcile(make_handler(), spec, [prev], False)
    assert output is not None
    assert output.action.op == "replace"
    assert stale_id in output.action.stale_data_ids
    assert output.action.data_id == data_id_for(KEY)


# -- deletion -----------------------------------------------------------------


def test_delete_with_no_prev_and_not_missing_is_noop() -> None:
    assert reconcile(make_handler(), coco.NON_EXISTENCE, [], False) is None


def test_delete_with_no_prev_but_possibly_missing_deletes_conservatively() -> None:
    # Deliberate deviation from statediff.diff (which would say None): a
    # pending marker without a committed record may hide an external write.
    output = reconcile(make_handler(), coco.NON_EXISTENCE, [], True)
    assert output is not None
    assert output.action.op == "delete"
    assert output.action.payload is None
    assert coco.is_non_existence(output.tracking_record)


def test_delete_with_prev_record_deletes() -> None:
    prev = record_for(make_spec())
    output = reconcile(make_handler(), coco.NON_EXISTENCE, [prev], False)
    assert output is not None
    assert output.action.op == "delete"
    assert output.action.payload is None
    assert coco.is_non_existence(output.tracking_record)


# -- key validation -----------------------------------------------------------


def test_non_str_key_raises_type_error() -> None:
    with pytest.raises(TypeError, match="document key must be str"):
        make_handler().reconcile(42, make_spec(), [], True)


def test_nul_in_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="NUL"):
        make_handler().reconcile("bad\x00key", make_spec(), [], True)


# -- derived identity and payload ---------------------------------------------


def test_action_data_id_is_derived_from_logical_coordinates() -> None:
    for key in (KEY, "another/doc.txt", "id-42"):
        output = reconcile(make_handler(), make_spec(), [], True, key=key)
        assert output is not None
        assert output.action.data_id == cogindex.document_data_id(RUNTIME_KEY, TENANT, DATASET, key)
        assert output.action.external_key == key


def test_delete_action_data_id_is_derived_from_key() -> None:
    output = reconcile(make_handler(), coco.NON_EXISTENCE, [], True, key="gone.md")
    assert output is not None
    assert output.action.data_id == cogindex.document_data_id(
        RUNTIME_KEY, TENANT, DATASET, "gone.md"
    )


def test_payload_carries_spec_fields() -> None:
    spec = make_spec(
        "body text",
        label="a label",
        external_metadata={"source": "unit", "n": 3},
        node_set=("alpha", "beta"),
        importance_weight=0.75,
    )
    output = reconcile(make_handler(), spec, [], True)
    assert output is not None
    assert output.action.op == "upsert"
    payload = output.action.payload
    assert payload is not None
    assert payload.data_id == data_id_for(KEY)
    assert payload.content == "body text"
    assert payload.label == "a label"
    assert payload.external_metadata == {"source": "unit", "n": 3}
    assert payload.node_set == ("alpha", "beta")
    assert payload.importance_weight == 0.75
