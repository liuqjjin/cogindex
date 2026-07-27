"""Unit tests for tracking records (_records) and desired-state specs (_spec).

Covers ADR-0003/0005 contracts: msgspec roundtrip stability of tracking
records, eager fingerprint computation on CogneeDocumentSpec, per-field
fingerprint sensitivity of ProcessingConfig, and record derivation via
document_record_for.
"""

from __future__ import annotations

import dataclasses
import importlib
import uuid
from collections.abc import Callable
from pathlib import Path
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


def test_processing_config_rejects_non_positive_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        ProcessingConfig(chunk_size=0)


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
        runtime_config_fingerprint="runtime-fp-1",
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
    (
        "runtime_config_fingerprint",
        lambda c: dataclasses.replace(c, runtime_config_fingerprint="runtime-fp-2"),
    ),
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
        # api_key-ish names are the dangerous ones; fingerprint/digest is fine.
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
    assert derived.runtime_config_fingerprint
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


def test_empty_custom_prompt_is_equivalent_to_no_custom_prompt() -> None:
    empty = processing_config_from_profile(CognifyProfile(custom_prompt=""))
    unset = processing_config_from_profile(CognifyProfile(custom_prompt=None))

    assert empty.custom_prompt_fingerprint is None
    assert empty == unset
    assert empty.fingerprint() == unset.fingerprint()


def test_excluding_runtime_models_is_an_explicit_runtime_invalidation_escape_hatch() -> None:
    # The documented escape hatch for reproducing identical fingerprints across
    # machines whose model configuration differs.
    with_models = processing_config_from_profile(CognifyProfile())
    without = processing_config_from_profile(CognifyProfile(), include_runtime_models=False)
    assert without.runtime_config_fingerprint is None
    assert without.graph_model_id == with_models.graph_model_id
    assert without.chunker_id == with_models.chunker_id
    assert without.fingerprint() != with_models.fingerprint()


def _automatic_runtime_fingerprint(profile: CognifyProfile | None = None) -> str:
    derived = processing_config_from_profile(profile or CognifyProfile())
    assert derived.runtime_config_fingerprint is not None
    return derived.runtime_config_fingerprint


@pytest.mark.parametrize(
    ("field_name", "new_value"),
    [
        ("llm_model", "openai/cogindex-base-regression"),
        ("llm_provider", "anthropic"),
        ("llm_api_version", "base-v2"),
        ("llm_extraction_model", "openai/cogindex-extraction-regression"),
        ("llm_extraction_provider", "custom"),
        ("llm_extraction_api_version", "extraction-v2"),
        ("llm_summarization_model", "openai/cogindex-summary-regression"),
        ("llm_summarization_provider", "gemini"),
        ("llm_summarization_api_version", "summary-v2"),
        ("llm_temperature", 0.37),
        ("structured_output_framework", "litellm_native"),
        ("llm_max_completion_tokens", 12_345),
        ("llm_args", {"temperature": 0.61, "top_p": 0.83}),
    ],
)
def test_runtime_fingerprint_tracks_llm_derivative_configuration(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    new_value: object,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    before = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, field_name, new_value)

    assert _automatic_runtime_fingerprint() != before


@pytest.mark.parametrize(
    ("first_args", "second_args"),
    [
        ({"parallel_tool_calls": False}, {"parallel_tool_calls": True}),
        ({"mirostat_tau": 4.0}, {"mirostat_tau": 5.0}),
        ({"extra_body": {"top_k": 20}}, {"extra_body": {"top_k": 40}}),
        (
            {
                "response_format": {
                    "json_schema": {
                        "schema": {"$id": "https://schemas.invalid/first", "type": "object"}
                    }
                }
            },
            {
                "response_format": {
                    "json_schema": {
                        "schema": {"$id": "https://schemas.invalid/second", "type": "object"}
                    }
                }
            },
        ),
    ],
)
def test_runtime_fingerprint_tracks_generation_model_arguments(
    monkeypatch: pytest.MonkeyPatch,
    first_args: dict[str, object],
    second_args: dict[str, object],
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(config, "llm_args", first_args)
    first = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, "llm_args", second_args)

    assert _automatic_runtime_fingerprint() != first


def test_runtime_inputs_use_registry_capped_llm_ceiling_for_every_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_module = importlib.import_module("cognee.infrastructure.llm.config")
    utils_module = importlib.import_module("cognee.infrastructure.llm.utils")
    config = config_module.get_llm_config()
    monkeypatch.setattr(config, "llm_model", "cogindex/base")
    monkeypatch.setattr(config, "llm_extraction_model", "cogindex/extraction")
    monkeypatch.setattr(config, "llm_summarization_model", "cogindex/summarization")
    monkeypatch.setattr(config, "llm_max_completion_tokens", 10_000)
    registry = {
        "cogindex/base": 8_000,
        "cogindex/extraction": 7_000,
        "cogindex/summarization": 6_000,
    }
    monkeypatch.setattr(
        utils_module,
        "get_model_max_completion_tokens",
        registry.get,
    )

    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert inputs["llm"]["base"]["max_completion_tokens"] == 8_000
    assert inputs["llm"]["extraction"]["max_completion_tokens"] == 7_000
    assert inputs["llm"]["summarization"]["max_completion_tokens"] == 6_000
    assert inputs["llm"]["dynamic_chunk"]["max_completion_tokens"] == 8_000


def test_runtime_fingerprint_tracks_effective_not_unreached_llm_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_module = importlib.import_module("cognee.infrastructure.llm.config")
    utils_module = importlib.import_module("cognee.infrastructure.llm.utils")
    config = config_module.get_llm_config()
    registry_limit = {"value": 8_000}
    monkeypatch.setattr(
        utils_module,
        "get_model_max_completion_tokens",
        lambda model: registry_limit["value"],
    )
    monkeypatch.setattr(config, "llm_max_completion_tokens", 10_000)
    capped = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, "llm_max_completion_tokens", 9_000)
    assert _automatic_runtime_fingerprint() == capped

    registry_limit["value"] = 7_500
    registry_changed = _automatic_runtime_fingerprint()
    assert registry_changed != capped

    monkeypatch.setattr(config, "llm_max_completion_tokens", 7_000)
    assert _automatic_runtime_fingerprint() != registry_changed


@pytest.mark.parametrize("invalid_limit", [0, -1, 1.5, "8192", True])
def test_invalid_registry_llm_ceiling_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_limit: object,
) -> None:
    utils_module = importlib.import_module("cognee.infrastructure.llm.utils")
    monkeypatch.setattr(
        utils_module,
        "get_model_max_completion_tokens",
        lambda model: invalid_limit,
    )

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_runtime_fingerprint_tracks_baml_configuration_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(config, "structured_output_framework", "baml")
    monkeypatch.setattr(config, "baml_llm_provider", "anthropic")
    monkeypatch.setattr(config, "baml_llm_model", "claude-first")
    monkeypatch.setattr(config, "baml_llm_temperature", 0.2)
    monkeypatch.setattr(config, "baml_llm_api_version", "v1")
    monkeypatch.setattr(config, "baml_llm_api_key", "first-secret")
    monkeypatch.setattr(config, "baml_llm_endpoint", "https://first.invalid")
    first = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, "baml_llm_api_key", "second-secret")
    monkeypatch.setattr(config, "baml_llm_endpoint", "https://second.invalid")
    assert _automatic_runtime_fingerprint() == first

    monkeypatch.setattr(config, "baml_llm_model", "claude-second")
    assert _automatic_runtime_fingerprint() != first


def test_missing_stage_config_contract_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(type(config), "stage_config", None)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_llama_cpp_model_path_is_hashed_and_changes_runtime_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(config, "llm_provider", "llama_cpp")
    monkeypatch.setattr(config, "llm_model", "local-model")
    monkeypatch.setattr(config, "llama_cpp_model_path", None)
    without_path = _automatic_runtime_fingerprint()

    model_path = tmp_path / "weights.gguf"
    monkeypatch.setattr(config, "llama_cpp_model_path", str(model_path))
    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert str(model_path) not in repr(inputs)
    assert inputs["llm"]["base"]["llama_cpp"]["model_path"]
    assert _automatic_runtime_fingerprint() != without_path

    monkeypatch.setattr(config, "llama_cpp_model_path", object())
    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


@pytest.mark.parametrize(
    ("field_name", "new_value"),
    [
        ("embedding_provider", "fastembed"),
        ("embedding_model", "sentence-transformers/cogindex-regression"),
        ("embedding_dimensions", 768),
        ("embedding_max_completion_tokens", 4_096),
        ("huggingface_tokenizer", "sentence-transformers/all-MiniLM-L6-v2"),
        ("embedding_api_version", "embedding-v2"),
        ("embedding_api_version", 2.0),
    ],
)
def test_runtime_fingerprint_tracks_embedding_derivative_configuration(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    new_value: object,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(config.embedding_dimensions))
    before = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, field_name, new_value)
    if field_name == "embedding_dimensions":
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(new_value))

    assert _automatic_runtime_fingerprint() != before


def test_unregistered_embedding_model_without_explicit_dimensions_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(config, "embedding_model", "cogindex/unregistered-embedding")
    monkeypatch.setattr(module, "_resolve_embedding_dimensions", lambda provider, model: None)

    with pytest.raises(CompatibilityError, match="EMBEDDING_DIMENSIONS"):
        _compat.validate_embedding_dimensions()
    with pytest.raises(CompatibilityError, match="EMBEDDING_DIMENSIONS"):
        processing_config_from_profile(CognifyProfile())


def test_explicit_embedding_dimensions_accept_an_unregistered_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setattr(config, "embedding_model", "cogindex/unregistered-embedding")
    monkeypatch.setattr(config, "embedding_dimensions", 768)
    monkeypatch.setattr(module, "_resolve_embedding_dimensions", lambda provider, model: None)

    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert inputs["embedding"]["dimensions"] == 768


def test_dotenv_only_embedding_dimensions_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    (tmp_path / ".env").write_text("EMBEDDING_DIMENSIONS=768\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "embedding_model", "cogindex/unregistered-embedding")
    monkeypatch.setattr(config, "embedding_dimensions", 768)
    monkeypatch.setattr(module, "_resolve_embedding_dimensions", lambda provider, model: None)

    assert _compat.validate_embedding_dimensions() == 768


def test_embedding_probe_drift_from_explicit_dimensions_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setattr(config, "embedding_dimensions", 384)

    with pytest.raises(CompatibilityError, match="EMBEDDING_DIMENSIONS"):
        _compat.validate_embedding_dimensions()


def test_registry_known_embedding_dimensions_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(config, "embedding_model", "cogindex/known-embedding")
    monkeypatch.setattr(config, "embedding_dimensions", 384)
    monkeypatch.setattr(module, "_resolve_embedding_dimensions", lambda provider, model: 384)

    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert inputs["embedding"]["dimensions"] == 384


def test_unexplicit_embedding_dimension_registry_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.databases.vector.embeddings.config")
    config = module.get_embedding_config()
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    monkeypatch.setattr(config, "embedding_dimensions", 768)
    monkeypatch.setattr(module, "_resolve_embedding_dimensions", lambda provider, model: 384)

    with pytest.raises(CompatibilityError, match="EMBEDDING_DIMENSIONS"):
        _compat.validate_embedding_dimensions()
    with pytest.raises(CompatibilityError, match="EMBEDDING_DIMENSIONS"):
        processing_config_from_profile(CognifyProfile())


def test_runtime_fingerprint_tracks_configured_graph_prompt_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    llm_module = importlib.import_module("cognee.infrastructure.llm.config")
    config = llm_module.get_llm_config()
    prompt = tmp_path / "graph.txt"
    prompt.write_text("extract organizations", encoding="utf-8")
    monkeypatch.setattr(config, "graph_prompt_path", str(prompt))
    before = _automatic_runtime_fingerprint()

    prompt.write_text("extract organizations and people", encoding="utf-8")

    assert _automatic_runtime_fingerprint() != before


@pytest.mark.parametrize("filename", ["classify_content.txt", "summarize_content.txt"])
def test_runtime_fingerprint_tracks_builtin_prompt_content(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    prompt_module = importlib.import_module("cognee.infrastructure.llm.prompts")
    read_prompt = prompt_module.read_query_prompt
    before = _automatic_runtime_fingerprint()

    def changed_prompt(requested: str, base_directory: str | None = None) -> str | None:
        content = read_prompt(requested, base_directory)
        return f"{content}\nchanged" if requested == filename and content is not None else content

    monkeypatch.setattr(prompt_module, "read_query_prompt", changed_prompt)

    assert _automatic_runtime_fingerprint() != before


@pytest.mark.parametrize(
    ("loader_name", "failure_kind"),
    [
        ("render_prompt", "not-callable"),
        ("render_prompt", "invalid-result"),
        ("read_query_prompt", "not-callable"),
        ("read_query_prompt", "invalid-result"),
    ],
)
def test_invalid_prompt_loader_result_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    loader_name: str,
    failure_kind: str,
) -> None:
    prompt_module = importlib.import_module("cognee.infrastructure.llm.prompts")

    if failure_kind == "not-callable":
        monkeypatch.setattr(prompt_module, loader_name, None)
    elif loader_name == "render_prompt":

        def invalid_render(
            filename: str,
            context: dict[str, object],
            base_directory: str | None = None,
        ) -> object:
            return object()

        monkeypatch.setattr(prompt_module, loader_name, invalid_render)
    else:

        def missing_prompt(
            filename: str,
            base_directory: str | None = None,
        ) -> None:
            return None

        monkeypatch.setattr(prompt_module, loader_name, missing_prompt)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


@pytest.mark.parametrize(
    "field_name",
    ["temporal_graph_prompt_path", "event_entity_prompt_path"],
)
def test_runtime_fingerprint_tracks_temporal_prompt_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field_name: str,
) -> None:
    llm_module = importlib.import_module("cognee.infrastructure.llm.config")
    config = llm_module.get_llm_config()
    prompt = tmp_path / f"{field_name}.txt"
    prompt.write_text("temporal prompt one", encoding="utf-8")
    monkeypatch.setattr(config, field_name, str(prompt))
    profile = CognifyProfile(temporal_cognify=True)
    before = _automatic_runtime_fingerprint(profile)

    prompt.write_text("temporal prompt two", encoding="utf-8")

    assert _automatic_runtime_fingerprint(profile) != before


def test_custom_graph_prompt_excludes_unused_default_graph_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    llm_module = importlib.import_module("cognee.infrastructure.llm.config")
    config = llm_module.get_llm_config()
    prompt = tmp_path / "unused-default-graph.txt"
    prompt.write_text("unused graph prompt one", encoding="utf-8")
    monkeypatch.setattr(config, "graph_prompt_path", str(prompt))
    profile = CognifyProfile(custom_prompt="extract only organizations")
    before = processing_config_from_profile(profile).fingerprint()

    prompt.write_text("unused graph prompt two", encoding="utf-8")

    assert processing_config_from_profile(profile).fingerprint() == before


def test_empty_custom_prompt_still_tracks_default_graph_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    llm_module = importlib.import_module("cognee.infrastructure.llm.config")
    config = llm_module.get_llm_config()
    prompt = tmp_path / "default-graph.txt"
    prompt.write_text("default graph prompt one", encoding="utf-8")
    monkeypatch.setattr(config, "graph_prompt_path", str(prompt))
    profile = CognifyProfile(custom_prompt="")
    before = processing_config_from_profile(profile).fingerprint()

    prompt.write_text("default graph prompt two", encoding="utf-8")

    assert processing_config_from_profile(profile).fingerprint() != before


def test_runtime_fingerprint_tracks_cognify_models_and_triplet_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pydantic

    class OtherClassification(pydantic.BaseModel):
        category: str

    class OtherSummary(pydantic.BaseModel):
        summary: str
        confidence: float

    module = importlib.import_module("cognee.modules.cognify.config")
    config = module.get_cognify_config()
    baseline = _automatic_runtime_fingerprint()

    monkeypatch.setattr(config, "classification_model", OtherClassification)
    classification_changed = _automatic_runtime_fingerprint()
    assert classification_changed != baseline

    monkeypatch.setattr(config, "summarization_model", OtherSummary)
    summary_changed = _automatic_runtime_fingerprint()
    assert summary_changed != classification_changed

    monkeypatch.setattr(config, "triplet_embedding", not config.triplet_embedding)
    assert _automatic_runtime_fingerprint() != summary_changed


def test_broken_runtime_model_schema_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenClassification:
        @classmethod
        def model_json_schema(cls) -> dict[str, object]:
            raise RuntimeError("broken schema")

    module = importlib.import_module("cognee.modules.cognify.config")
    config = module.get_cognify_config()
    monkeypatch.setattr(config, "classification_model", BrokenClassification)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


@pytest.mark.parametrize("failure_kind", ["not-a-type", "non-callable-schema"])
def test_invalid_runtime_model_shape_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    class NonCallableSchema:
        model_json_schema = object()

    module = importlib.import_module("cognee.modules.cognify.config")
    config = module.get_cognify_config()
    invalid_model: object = object() if failure_kind == "not-a-type" else NonCallableSchema
    monkeypatch.setattr(config, "classification_model", invalid_model)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_runtime_model_without_schema_uses_qualified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClassificationWithoutSchema:
        pass

    module = importlib.import_module("cognee.modules.cognify.config")
    config = module.get_cognify_config()
    monkeypatch.setattr(config, "classification_model", ClassificationWithoutSchema)
    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert inputs["cognify"]["classification_model"] == {
        "id": (
            f"{ClassificationWithoutSchema.__module__}.{ClassificationWithoutSchema.__qualname__}"
        ),
        "schema": None,
    }


def test_runtime_fingerprint_tracks_ontology_content_and_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cognee.modules.ontology.ontology_env_config")
    config = module.get_ontology_env_config()
    ontology = tmp_path / "ontology.ttl"
    ontology.write_text("@prefix ex: <urn:one:> .", encoding="utf-8")
    monkeypatch.setattr(config, "ontology_file_path", str(ontology))
    baseline = _automatic_runtime_fingerprint()

    ontology.write_text("@prefix ex: <urn:two:> .", encoding="utf-8")
    content_changed = _automatic_runtime_fingerprint()
    assert content_changed != baseline

    monkeypatch.setattr(config, "matching_strategy", "cogindex-regression-strategy")
    strategy_changed = _automatic_runtime_fingerprint()
    assert strategy_changed != content_changed

    monkeypatch.setattr(config, "ontology_resolver", "cogindex-regression-resolver")
    assert _automatic_runtime_fingerprint() != strategy_changed


def test_ontology_fingerprint_uses_contents_not_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = importlib.import_module("cognee.modules.ontology.ontology_env_config")
    config = module.get_ontology_env_config()
    first_path = tmp_path / "first.ttl"
    second_path = tmp_path / "second.ttl"
    content = "@prefix ex: <urn:same:> ."
    first_path.write_text(content, encoding="utf-8")
    second_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(config, "ontology_file_path", str(first_path))
    first = _automatic_runtime_fingerprint()
    monkeypatch.setattr(config, "ontology_file_path", str(second_path))

    assert _automatic_runtime_fingerprint() == first


@pytest.mark.parametrize("configured_path", ["missing", "one.ttl,,two.ttl"])
def test_unreadable_or_malformed_ontology_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_path: str,
) -> None:
    module = importlib.import_module("cognee.modules.ontology.ontology_env_config")
    config = module.get_ontology_env_config()
    if configured_path == "missing":
        configured_path = str(tmp_path / "missing.ttl")
    monkeypatch.setattr(config, "ontology_file_path", configured_path)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_credentials_endpoints_and_execution_knobs_do_not_change_runtime_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_module = importlib.import_module("cognee.infrastructure.llm.config")
    embedding_module = importlib.import_module(
        "cognee.infrastructure.databases.vector.embeddings.config"
    )
    llm = llm_module.get_llm_config()
    embedding = embedding_module.get_embedding_config()

    def set_ignored_values(suffix: str, number: int) -> None:
        for field_name, value in (
            ("llm_api_key", f"llm-secret-{suffix}"),
            ("llm_endpoint", f"https://llm-{suffix}.invalid"),
            ("llm_extraction_api_key", f"extraction-secret-{suffix}"),
            ("llm_extraction_endpoint", f"https://extraction-{suffix}.invalid"),
            ("llm_summarization_api_key", f"summary-secret-{suffix}"),
            ("llm_summarization_endpoint", f"https://summary-{suffix}.invalid"),
            ("fallback_api_key", f"fallback-secret-{suffix}"),
            ("fallback_endpoint", f"https://fallback-{suffix}.invalid"),
            ("baml_llm_api_key", f"baml-secret-{suffix}"),
            ("baml_llm_endpoint", f"https://baml-{suffix}.invalid"),
            ("llm_rate_limit_requests", number),
            ("llm_rate_limit_interval", number),
            ("llm_rate_limit_tokens", number),
            ("llm_streaming", bool(number % 2)),
        ):
            monkeypatch.setattr(llm, field_name, value)
        monkeypatch.setattr(
            llm,
            "llm_args",
            {
                "auth": f"provider-auth-{suffix}",
                "api_key": f"provider-secret-{suffix}",
                "endpoint": f"https://provider-{suffix}.invalid",
                "parallel_tool_calls": True,
                "temperature": 0.2,
                "nested": {
                    "api_key": f"nested-secret-{suffix}",
                    "endpoint": f"https://nested-{suffix}.invalid",
                    "headers": {"Authorization": f"Bearer {suffix}"},
                    "timeout": number,
                    "retry_count": number,
                    "batch_size": number,
                    "rate_limit": number,
                },
            },
        )
        for field_name, value in (
            ("embedding_api_key", f"embedding-secret-{suffix}"),
            ("embedding_endpoint", f"https://embedding-{suffix}.invalid"),
            ("embedding_batch_size", number),
        ):
            monkeypatch.setattr(embedding, field_name, value)

    set_ignored_values("first", 3)
    before = _automatic_runtime_fingerprint()
    set_ignored_values("second", 97)

    assert _automatic_runtime_fingerprint() == before


@pytest.mark.parametrize(
    ("module_name", "getter_name", "field_name", "invalid_value"),
    [
        (
            "cognee.infrastructure.llm.config",
            "get_llm_config",
            "structured_output_framework",
            "",
        ),
        (
            "cognee.infrastructure.llm.config",
            "get_llm_config",
            "llm_temperature",
            float("nan"),
        ),
        (
            "cognee.infrastructure.llm.config",
            "get_llm_config",
            "llm_temperature",
            "cold",
        ),
        (
            "cognee.infrastructure.databases.vector.embeddings.config",
            "get_embedding_config",
            "embedding_api_version",
            object(),
        ),
        (
            "cognee.modules.cognify.config",
            "get_cognify_config",
            "triplet_embedding",
            1,
        ),
    ],
)
def test_invalid_runtime_config_type_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    getter_name: str,
    field_name: str,
    invalid_value: object,
) -> None:
    module = importlib.import_module(module_name)
    config = getattr(module, getter_name)()
    monkeypatch.setattr(config, field_name, invalid_value)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_ambiguous_url_model_argument_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(config, "llm_args", {"resource": "https://example.invalid/model"})

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_ambiguous_secret_shaped_model_argument_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("cognee.infrastructure.llm.config")
    config = module.get_llm_config()
    monkeypatch.setattr(config, "llm_args", {"vendor_key": "ambiguous"})

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


@pytest.mark.parametrize(
    ("module_name", "field_name"),
    [
        ("cognee.infrastructure.llm.config", "llm_max_completion_tokens"),
        (
            "cognee.infrastructure.databases.vector.embeddings.config",
            "embedding_max_completion_tokens",
        ),
    ],
)
def test_missing_dynamic_chunk_input_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    field_name: str,
) -> None:
    module = importlib.import_module(module_name)
    getter_name = "get_llm_config" if field_name.startswith("llm_") else "get_embedding_config"
    config = getattr(module, getter_name)()
    monkeypatch.setattr(config, field_name, None)

    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        processing_config_from_profile(CognifyProfile())


def test_graph_model_schema_fingerprint_tracks_structure_not_name() -> None:
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


def test_missing_runtime_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: object) -> dict[str, object]:
        raise CompatibilityError("automatic processing invalidation is incomplete")

    monkeypatch.setattr(_compat, "configured_processing_inputs", fail)
    with pytest.raises(CompatibilityError, match="incomplete"):
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
