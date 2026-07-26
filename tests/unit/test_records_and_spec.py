"""Unit tests for tracking records (_records) and desired-state specs (_spec).

Covers ADR-0003/0005 contracts: msgspec roundtrip stability of tracking
records, eager fingerprint computation on CogneeDocumentSpec, per-field
fingerprint sensitivity of ProcessingConfig, and record derivation via
document_record_for.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import msgspec
import pytest
from cocoindex.connectorkits.statediff import MutualTrackingRecord
from cocoindex.connectorkits.target import ManagedBy

from cogindex import CompatibilityError, _compat
from cogindex._records import RECORD_SCHEMA_VERSION, DatasetConfigRecord, DocumentRecord
from cogindex._spec import (
    CogneeDocumentSpec,
    CognifyProfile,
    ProcessingConfig,
    document_record_for,
    processing_config_from_profile,
)

# ---------------------------------------------------------------------------
# Contract 1: msgspec.json roundtrip preserves equality
# ---------------------------------------------------------------------------


def test_document_record_json_roundtrip() -> None:
    record = DocumentRecord(
        data_id=uuid.uuid5(uuid.NAMESPACE_DNS, "cogindex-test-doc"),
        content_fingerprint="cf",
        annotations_fingerprint="af",
        metadata_fingerprint="mf",
        processing_fingerprint="pf",
        importance_weight_fingerprint="wf",
        schema_version=RECORD_SCHEMA_VERSION,
    )
    decoded = msgspec.json.decode(msgspec.json.encode(record), type=DocumentRecord)
    assert decoded == record
    assert isinstance(decoded.data_id, uuid.UUID)
    assert decoded.data_id == record.data_id


def test_document_record_without_weight_fingerprint_uses_unknown_default() -> None:
    """Records written before the weight split remain decodable.

    The empty sentinel cannot equal a real BLAKE2 fingerprint, so reconcile
    will conservatively recreate the row once.
    """
    encoded = msgspec.json.encode(
        {
            "data_id": uuid.uuid5(uuid.NAMESPACE_DNS, "legacy-cogindex-record"),
            "content_fingerprint": "cf",
            "annotations_fingerprint": "af",
            "metadata_fingerprint": "mf",
            "processing_fingerprint": "pf",
            "schema_version": RECORD_SCHEMA_VERSION,
        }
    )

    decoded = msgspec.json.decode(encoded, type=DocumentRecord)

    assert decoded.importance_weight_fingerprint == ""


def test_dataset_config_record_json_roundtrip() -> None:
    record = DatasetConfigRecord(processing_fingerprint="pf", schema_version=RECORD_SCHEMA_VERSION)
    decoded = msgspec.json.decode(msgspec.json.encode(record), type=DatasetConfigRecord)
    assert decoded == record


@pytest.mark.parametrize("managed_by", [ManagedBy.SYSTEM, ManagedBy.USER])
def test_mutual_tracking_record_json_roundtrip(managed_by: ManagedBy) -> None:
    record = MutualTrackingRecord(
        tracking_record=DatasetConfigRecord(processing_fingerprint="pf"),
        managed_by=managed_by,
    )
    decoded = msgspec.json.decode(
        msgspec.json.encode(record), type=MutualTrackingRecord[DatasetConfigRecord]
    )
    assert decoded == record
    assert decoded.tracking_record == record.tracking_record
    assert decoded.managed_by is managed_by
    assert isinstance(decoded.managed_by, ManagedBy)


# ---------------------------------------------------------------------------
# Contract 2: CogneeDocumentSpec computes fingerprints eagerly at construction
# ---------------------------------------------------------------------------


def test_label_change_affects_only_metadata_fingerprint() -> None:
    spec_a = CogneeDocumentSpec(content="same content", label="label-a")
    spec_b = CogneeDocumentSpec(content="same content", label="label-b")
    assert spec_a.content_fingerprint == spec_b.content_fingerprint
    assert spec_a.annotations_fingerprint == spec_b.annotations_fingerprint
    assert spec_a.metadata_fingerprint != spec_b.metadata_fingerprint


def test_external_metadata_change_affects_annotations_fingerprint() -> None:
    spec_a = CogneeDocumentSpec(
        content="body",
        label="lbl",
        external_metadata={"source": "s1"},
        node_set=("n",),
        importance_weight=1.0,
    )
    spec_b = CogneeDocumentSpec(
        content="body",
        label="lbl",
        external_metadata={"source": "s2"},
        node_set=("n",),
        importance_weight=1.0,
    )
    assert spec_a.content_fingerprint == spec_b.content_fingerprint
    assert spec_a.annotations_fingerprint != spec_b.annotations_fingerprint
    assert spec_a.metadata_fingerprint == spec_b.metadata_fingerprint


def test_external_metadata_is_copied_at_construction() -> None:
    metadata: dict[str, Any] = {
        "source": "s1",
        "nested": {"revision": 1},
        "tags": ["a"],
    }
    spec = CogneeDocumentSpec(content="body", external_metadata=metadata)
    fingerprint = spec.annotations_fingerprint

    metadata["source"] = "s2"
    metadata["nested"]["revision"] = 2
    metadata["tags"].append("b")

    assert spec.external_metadata == {
        "source": "s1",
        "nested": {"revision": 1},
        "tags": ["a"],
    }
    assert spec.annotations_fingerprint == fingerprint


def test_node_set_fingerprint_is_order_insensitive_but_field_preserves_order() -> None:
    spec_ab = CogneeDocumentSpec(content="body", node_set=("a", "b"))
    spec_ba = CogneeDocumentSpec(content="body", node_set=("b", "a"))
    assert spec_ab.annotations_fingerprint == spec_ba.annotations_fingerprint
    assert spec_ab.node_set == ("a", "b")
    assert spec_ba.node_set == ("b", "a")


def test_importance_weight_changes_only_weight_fingerprint() -> None:
    spec_a = CogneeDocumentSpec(content="body", label="lbl", importance_weight=0.5)
    spec_b = CogneeDocumentSpec(content="body", label="lbl", importance_weight=0.75)
    assert spec_a.importance_weight_fingerprint != spec_b.importance_weight_fingerprint
    assert spec_a.annotations_fingerprint == spec_b.annotations_fingerprint
    assert spec_a.metadata_fingerprint == spec_b.metadata_fingerprint
    assert spec_a.content_fingerprint == spec_b.content_fingerprint


def test_str_and_bytes_content_never_collide() -> None:
    spec_str = CogneeDocumentSpec(content="x")
    spec_bytes = CogneeDocumentSpec(content=b"x")
    assert spec_str.content_fingerprint != spec_bytes.content_fingerprint


def test_non_json_external_metadata_raises_type_error_at_construction() -> None:
    with pytest.raises(TypeError):
        CogneeDocumentSpec(content="body", external_metadata={"x": object()})


@pytest.mark.parametrize("content", [bytearray(b"mutable"), memoryview(b"view"), 3])
def test_document_spec_rejects_content_outside_public_contract(content: Any) -> None:
    with pytest.raises(TypeError, match="content must be str or bytes"):
        CogneeDocumentSpec(content=content)


def test_document_spec_copies_and_freezes_runtime_node_sequence() -> None:
    nodes: Any = ["first", "second"]
    spec = CogneeDocumentSpec(content="body", node_set=nodes)
    nodes.append("third")

    assert spec.node_set == ("first", "second")


@pytest.mark.parametrize(
    "nodes",
    [
        "not-a-sequence-of-node-names",
        ("",),
        ("   ",),
        ("\t\n",),
        ("a\x00b",),
        ("same", "same"),
        ("valid", 3),
    ],
)
def test_document_spec_rejects_invalid_node_set(nodes: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="node_set"):
        CogneeDocumentSpec(content="body", node_set=nodes)


@pytest.mark.parametrize("weight", [True, "heavy", float("nan"), float("inf")])
def test_document_spec_rejects_invalid_importance_weight(weight: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="importance_weight"):
        CogneeDocumentSpec(content="body", importance_weight=weight)


def test_document_spec_normalizes_integer_weight_to_float() -> None:
    spec = CogneeDocumentSpec(content="body", importance_weight=1)
    assert spec.importance_weight == 1.0
    assert isinstance(spec.importance_weight, float)


@pytest.mark.parametrize("chunk_size", [0, -1, True])
def test_cognify_profile_rejects_invalid_chunk_size(chunk_size: Any) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        CognifyProfile(chunk_size=chunk_size)


@pytest.mark.parametrize("field_name", ["chunk_size", "embedding_dimensions"])
def test_processing_config_rejects_non_positive_dimensions(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        if field_name == "chunk_size":
            ProcessingConfig(chunk_size=0)
        else:
            ProcessingConfig(embedding_dimensions=0)


def test_processing_config_copies_normalizes_and_sorts_extras() -> None:
    extras: Any = [["second", "2"], ["first", "1"]]
    config = ProcessingConfig(extras=extras)
    fingerprint = config.fingerprint()

    extras[0][1] = "changed"

    assert config.extras == (("first", "1"), ("second", "2"))
    assert config.fingerprint() == fingerprint
    assert (
        config.fingerprint()
        == ProcessingConfig(extras=(("second", "2"), ("first", "1"))).fingerprint()
    )


@pytest.mark.parametrize(
    "extras",
    [
        (("only-key",),),
        (("", "value"),),
        (("   ", "value"),),
        (("key\x00bad", "value"),),
        (("key", "value\x00bad"),),
        (("same", "1"), ("same", "2")),
        (("key", 1),),
    ],
)
def test_processing_config_rejects_invalid_extras(extras: Any) -> None:
    with pytest.raises((TypeError, ValueError), match="extras"):
        ProcessingConfig(extras=extras)


# ---------------------------------------------------------------------------
# Contract 3: ProcessingConfig.fingerprint()
# ---------------------------------------------------------------------------


def _base_processing_config() -> ProcessingConfig:
    return ProcessingConfig(
        graph_model_id="models.KnowledgeGraph",
        graph_model_schema_fingerprint="schema-fp-1",
        chunker_id="chunkers.TextChunker",
        chunk_size=512,
        custom_prompt_fingerprint="prompt-fp-1",
        temporal_cognify=False,
        llm_model="llm-1",
        embedding_model="emb-1",
        embedding_dimensions=1536,
        extras=(("k", "v"),),
    )


def test_equal_processing_configs_have_equal_fingerprints() -> None:
    config_a = _base_processing_config()
    config_b = _base_processing_config()
    assert config_a == config_b
    assert config_a.fingerprint() == config_b.fingerprint()


_FIELD_CHANGES: list[tuple[str, Callable[[ProcessingConfig], ProcessingConfig]]] = [
    ("graph_model_id", lambda c: dataclasses.replace(c, graph_model_id="models.OtherGraph")),
    (
        "graph_model_schema_fingerprint",
        lambda c: dataclasses.replace(c, graph_model_schema_fingerprint="schema-fp-2"),
    ),
    ("chunker_id", lambda c: dataclasses.replace(c, chunker_id="chunkers.OtherChunker")),
    ("chunk_size", lambda c: dataclasses.replace(c, chunk_size=1024)),
    (
        "custom_prompt_fingerprint",
        lambda c: dataclasses.replace(c, custom_prompt_fingerprint="prompt-fp-2"),
    ),
    ("temporal_cognify", lambda c: dataclasses.replace(c, temporal_cognify=True)),
    ("llm_model", lambda c: dataclasses.replace(c, llm_model="llm-2")),
    ("embedding_model", lambda c: dataclasses.replace(c, embedding_model="emb-2")),
    # Same model at a different width: every stored vector is invalidated, and
    # cognee takes this from the environment independently of the model name.
    ("embedding_dimensions", lambda c: dataclasses.replace(c, embedding_dimensions=3072)),
    ("extras", lambda c: dataclasses.replace(c, extras=(("k", "v2"),))),
]


def test_field_change_parametrization_covers_every_field() -> None:
    covered = {name for name, _ in _FIELD_CHANGES}
    all_fields = {f.name for f in dataclasses.fields(ProcessingConfig)}
    assert covered == all_fields


def test_processing_config_holds_nothing_that_cannot_change_derivatives() -> None:
    """ADR-0005's exclusion list, enforced structurally.

    Every field here triggers a purge and re-cognify of an entire dataset when
    it changes, so a connection string or a batch size landing in this struct
    would mean rotating a credential rebuilds the whole graph. Deployment and
    performance knobs belong on the runtime, which never reaches a fingerprint.
    """
    forbidden = (
        "url",
        "dsn",
        "host",
        "port",
        "password",
        "token",
        "key",
        "secret",
        "credential",
        "batch",
        "concurrency",
        "parallel",
        "worker",
        "timeout",
        "retry",
        "log",
        "telemetry",
        "lock",
        "path",
        "root",
        "dir",
    )
    for field in dataclasses.fields(ProcessingConfig):
        # api_key-ish names are the dangerous ones; embedding_model is fine.
        hits = [word for word in forbidden if word in field.name.lower()]
        assert not hits, (
            f"ProcessingConfig.{field.name} looks like a deployment knob ({hits}); "
            "fields here force a full dataset rebuild when they change"
        )


@pytest.mark.parametrize(
    ("field_name", "change"), _FIELD_CHANGES, ids=[n for n, _ in _FIELD_CHANGES]
)
def test_single_field_change_changes_processing_fingerprint(
    field_name: str, change: Callable[[ProcessingConfig], ProcessingConfig]
) -> None:
    base = _base_processing_config()
    changed = change(base)
    assert getattr(changed, field_name) != getattr(base, field_name), (
        "test setup: value must actually change"
    )
    assert changed.fingerprint() != base.fingerprint()


# ---------------------------------------------------------------------------
# Contract 4: processing_config_from_profile resolves effective defaults
# ---------------------------------------------------------------------------


def test_derived_config_resolves_cognee_defaults_for_unset_fields() -> None:
    # An all-None profile must not fingerprint as all-None: the point is to
    # record what cognify would actually do, so that a cognee upgrade which
    # moves a default registers as a config change.
    derived = processing_config_from_profile(CognifyProfile())
    assert derived.graph_model_id is not None
    assert derived.chunker_id is not None
    assert derived.llm_model
    assert derived.embedding_model
    # A schema fingerprint means the graph model's shape is covered too, not
    # only its name.
    assert derived.graph_model_schema_fingerprint


def test_derived_config_prefers_explicit_profile_values_over_defaults() -> None:
    default_chunk_size = processing_config_from_profile(CognifyProfile()).chunk_size
    explicit = processing_config_from_profile(CognifyProfile(chunk_size=17))
    assert explicit.chunk_size == 17
    assert explicit.chunk_size != default_chunk_size


def test_falsey_explicit_types_do_not_fall_back_to_defaults() -> None:
    class FalseyType(type):
        def __bool__(cls) -> bool:
            return False

    class Graph(metaclass=FalseyType):
        pass

    class Chunker(metaclass=FalseyType):
        pass

    derived = processing_config_from_profile(CognifyProfile(graph_model=Graph, chunker=Chunker))

    assert derived.graph_model_id == f"{Graph.__module__}.{Graph.__qualname__}"
    assert derived.chunker_id == f"{Chunker.__module__}.{Chunker.__qualname__}"


def test_custom_prompt_is_fingerprinted_not_stored() -> None:
    # The prompt itself may be long and is not needed for change detection; a
    # fingerprint is enough and keeps the tracking record small.
    prompt = "extract only organizations"
    derived = processing_config_from_profile(CognifyProfile(custom_prompt=prompt))
    assert derived.custom_prompt_fingerprint
    assert prompt not in str(dataclasses.asdict(derived))
    other = processing_config_from_profile(CognifyProfile(custom_prompt=prompt + "!"))
    assert other.custom_prompt_fingerprint != derived.custom_prompt_fingerprint


def test_excluding_runtime_models_drops_only_the_environment_derived_fields() -> None:
    # The documented escape hatch for reproducing identical fingerprints across
    # machines whose model configuration differs.
    with_models = processing_config_from_profile(CognifyProfile())
    without = processing_config_from_profile(CognifyProfile(), include_runtime_models=False)
    assert (without.llm_model, without.embedding_model, without.embedding_dimensions) == (
        None,
        None,
        None,
    )
    assert without.graph_model_id == with_models.graph_model_id
    assert without.chunker_id == with_models.chunker_id
    assert without.fingerprint() != with_models.fingerprint()


def test_graph_model_schema_fingerprint_tracks_structure_not_name() -> None:
    import msgspec as _msgspec  # noqa: F401  (kept local; pydantic is the shape under test)
    import pydantic

    class Shape(pydantic.BaseModel):
        name: str

    class SameNameDifferentShape(pydantic.BaseModel):
        name: str
        weight: float

    # Deliberately give both models the same qualified name, which is what
    # happens when someone edits a graph model in place.
    SameNameDifferentShape.__qualname__ = Shape.__qualname__
    SameNameDifferentShape.__module__ = Shape.__module__

    first = processing_config_from_profile(CognifyProfile(graph_model=Shape))
    second = processing_config_from_profile(CognifyProfile(graph_model=SameNameDifferentShape))
    assert first.graph_model_id == second.graph_model_id
    assert first.graph_model_schema_fingerprint != second.graph_model_schema_fingerprint
    assert first.fingerprint() != second.fingerprint()


def test_graph_model_without_a_json_schema_degrades_to_name_only() -> None:
    class NotAPydanticModel:
        pass

    derived = processing_config_from_profile(CognifyProfile(graph_model=NotAPydanticModel))
    assert derived.graph_model_id is not None
    # No schema derivable, so the qualified name is the coarser fallback signal.
    assert derived.graph_model_schema_fingerprint is None


def test_graph_model_with_broken_schema_fails_closed() -> None:
    class BrokenSchema:
        @classmethod
        def model_json_schema(cls) -> dict[str, object]:
            raise RuntimeError("broken")

    with pytest.raises(ValueError, match="could not derive JSON schema"):
        processing_config_from_profile(CognifyProfile(graph_model=BrokenSchema))


def test_missing_runtime_model_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_compat, "configured_models", lambda: (None, None, None))

    with pytest.raises(CompatibilityError, match="processing invalidation"):
        processing_config_from_profile(CognifyProfile())


def test_missing_embedding_dimensions_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _compat,
        "configured_models",
        lambda: ("configured-llm", "configured-embedding", None),
    )

    with pytest.raises(CompatibilityError, match="dimensions"):
        processing_config_from_profile(CognifyProfile())


@pytest.mark.parametrize("missing_default", ["graph", "chunker"])
def test_missing_profile_type_default_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    missing_default: str,
) -> None:
    compat_info = _compat.load()
    replacement = (
        dataclasses.replace(compat_info, default_graph_model=None)
        if missing_default == "graph"
        else dataclasses.replace(compat_info, default_chunker=None)
    )
    monkeypatch.setattr(_compat, "load", lambda: replacement)

    with pytest.raises(CompatibilityError, match=missing_default):
        processing_config_from_profile(CognifyProfile())


# ---------------------------------------------------------------------------
# Contract 5: document_record_for
# ---------------------------------------------------------------------------


def test_document_record_for_copies_fingerprints_and_identity() -> None:
    spec = CogneeDocumentSpec(
        content="body",
        label="lbl",
        external_metadata={"source": "s"},
        node_set=("n1", "n2"),
        importance_weight=2.0,
    )
    data_id = uuid.uuid5(uuid.NAMESPACE_DNS, "cogindex-test-record-for")
    record = document_record_for(spec, data_id=data_id, processing_fingerprint="proc-fp")
    assert record == DocumentRecord(
        data_id=data_id,
        content_fingerprint=spec.content_fingerprint,
        annotations_fingerprint=spec.annotations_fingerprint,
        metadata_fingerprint=spec.metadata_fingerprint,
        processing_fingerprint="proc-fp",
        importance_weight_fingerprint=spec.importance_weight_fingerprint,
        schema_version=RECORD_SCHEMA_VERSION,
    )
    assert record.schema_version == RECORD_SCHEMA_VERSION
