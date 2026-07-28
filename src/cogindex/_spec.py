"""Desired-state specifications for datasets and documents.

Two views of processing configuration exist on purpose (ADR-0005):

- :class:`CognifyProfile` holds the *actual objects* passed to
  ``cognee.cognify()`` (model classes, chunker, prompt text).
- :class:`ProcessingConfig` is its *declarative twin*: plain, fingerprintable
  data whose fingerprint is persisted per document and drives config
  invalidation.

Derive the twin with :func:`processing_config_from_profile`. Hand-maintaining
both is how they drift apart.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from cocoindex.connectorkits.target import ManagedBy

from . import _compat
from ._errors import CompatibilityError
from ._identity import fingerprint_content, fingerprint_json
from ._records import RECORD_SCHEMA_VERSION, DocumentRecord

__all__ = [
    "CogneeDatasetSpec",
    "CogneeDocumentSpec",
    "CognifyProfile",
    "ProcessingConfig",
    "document_record_for",
    "processing_config_from_profile",
]


@dataclass(frozen=True)
class CognifyProfile:
    """The subset of ``cognee.cognify()`` parameters cogindex manages.

    ``None`` means "use cognee's default". The derived
    :class:`ProcessingConfig` records the *effective* value instead, so that a
    cognee upgrade that changes a default still invalidates correctly.
    """

    graph_model: type[Any] | None = None
    chunker: type[Any] | None = None
    chunk_size: int | None = None
    custom_prompt: str | None = None
    temporal_cognify: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("graph_model", self.graph_model),
            ("chunker", self.chunker),
        ):
            if value is not None and not isinstance(value, type):
                raise TypeError(f"{field_name} must be a type or None")
        chunk_size: Any = self.chunk_size
        if chunk_size is not None and (
            isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer or None")
        if self.custom_prompt is not None and not isinstance(self.custom_prompt, str):
            raise TypeError("custom_prompt must be str or None")
        if not isinstance(self.temporal_cognify, bool):
            raise TypeError("temporal_cognify must be bool")
        if self.temporal_cognify and self.graph_model is not None:
            raise ValueError("graph_model is not used by Cognee's temporal cognify pipeline")
        if self.temporal_cognify and self.custom_prompt:
            raise ValueError("custom_prompt is not used by Cognee's temporal cognify pipeline")


@dataclass(frozen=True)
class ProcessingConfig:
    """Declarative description of everything that shapes cognify derivatives.

    The fingerprint of this config is stored per document; changing any field
    purges and re-cognifies every document in the dataset (ADR-0005). Keep
    out anything that does NOT change derivatives (batch sizes, concurrency,
    telemetry). Putting it here causes needless full rebuilds.
    """

    graph_model_id: str | None = None
    graph_model_schema_fingerprint: str | None = None
    chunker_id: str | None = None
    chunk_size: int | None = None
    custom_prompt_fingerprint: str | None = None
    temporal_cognify: bool = False
    # One digest covers Cognee's derivative-affecting runtime configuration.
    # Raw prompts, ontology paths/content, model settings and credentials never
    # enter ProcessingConfig or tracking records.
    runtime_config_fingerprint: str | None = None
    extras: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        chunk_size: Any = self.chunk_size
        if chunk_size is not None and (
            isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer or None")
        extras: Any = self.extras
        try:
            normalized_extras = tuple(tuple(item) for item in extras)
        except TypeError as exc:
            raise TypeError("extras must be key/value string pairs") from exc
        if any(len(item) != 2 for item in normalized_extras):
            raise ValueError("extras must contain exactly two values per entry")
        if any(not isinstance(part, str) for item in normalized_extras for part in item):
            raise TypeError("extras keys and values must be strings")
        typed_extras = tuple((item[0], item[1]) for item in normalized_extras)
        if any(not key.strip() or "\x00" in key for key, _ in typed_extras):
            raise ValueError("extras keys must be non-blank and contain no NUL")
        if any("\x00" in value for _, value in typed_extras):
            raise ValueError("extras values must not contain NUL")
        if len({key for key, _ in typed_extras}) != len(typed_extras):
            raise ValueError("extras keys must be unique")
        object.__setattr__(self, "extras", tuple(sorted(typed_extras)))

    def fingerprint(self) -> str:
        return fingerprint_json(dataclasses.asdict(self))


def processing_config_from_profile(
    profile: CognifyProfile, *, include_runtime_models: bool = True
) -> ProcessingConfig:
    """Derive the declarative :class:`ProcessingConfig` from a profile.

    Every ``None`` in the profile is resolved to the value cognify would
    actually use, read out of the installed version's signature, so that
    upgrading cognee's own defaults shows up as a fingerprint change instead
    of passing unnoticed. Cognee's globally configured LLM, embedding, prompt,
    ontology and cognify settings are folded into one secret-free digest for
    the same reason: they live outside cognify() arguments, yet shape its
    derivatives.

    Set ``include_runtime_models=False`` only as an explicit escape hatch when
    one flow must produce identical fingerprints across machines. It disables
    all automatic invalidation for Cognee runtime configuration, not just model
    identifiers; the caller then owns supplying an explicit
    :class:`ProcessingConfig` when any such input changes.
    """
    compat_info = _compat.load()
    graph_model = None
    if not profile.temporal_cognify:
        graph_model = (
            profile.graph_model
            if profile.graph_model is not None
            else compat_info.default_graph_model
        )
    chunker = profile.chunker if profile.chunker is not None else compat_info.default_chunker
    if not profile.temporal_cognify and graph_model is None:
        raise CompatibilityError(
            "could not resolve Cognee's default graph model; pass graph_model explicitly"
        )
    if chunker is None:
        raise CompatibilityError(
            "could not resolve Cognee's default chunker; pass chunker explicitly"
        )
    chunk_size = (
        profile.chunk_size if profile.chunk_size is not None else (compat_info.default_chunk_size)
    )
    runtime_config_fingerprint: str | None = None
    if include_runtime_models:
        runtime_inputs = _compat.configured_processing_inputs(
            # Cognee selects the custom prompt with ``if custom_prompt``;
            # an empty string therefore still uses the configured default.
            uses_custom_graph_prompt=bool(profile.custom_prompt),
            temporal_cognify=profile.temporal_cognify,
        )
        runtime_config_fingerprint = fingerprint_json(runtime_inputs)
    return ProcessingConfig(
        graph_model_id=_qualified_id(graph_model) if graph_model is not None else None,
        graph_model_schema_fingerprint=(
            _model_schema_fingerprint(graph_model) if graph_model is not None else None
        ),
        chunker_id=_qualified_id(chunker),
        chunk_size=chunk_size,
        custom_prompt_fingerprint=(
            fingerprint_content(profile.custom_prompt) if profile.custom_prompt else None
        ),
        temporal_cognify=profile.temporal_cognify,
        runtime_config_fingerprint=runtime_config_fingerprint,
    )


@dataclass(frozen=True)
class CogneeDatasetSpec:
    """Desired state of a dataset container target."""

    profile: CognifyProfile
    processing: ProcessingConfig
    managed_by: ManagedBy = ManagedBy.SYSTEM


@dataclass(frozen=True)
class CogneeDocumentSpec:
    """Desired state of one document within a dataset.

    Fingerprints are computed eagerly at construction so invalid input (e.g.
    non-JSON-serializable ``external_metadata``) fails at declaration time
    with a useful stack, not inside the engine's reconcile loop.
    """

    content: str | bytes
    label: str | None = None
    external_metadata: dict[str, Any] | None = None
    node_set: tuple[str, ...] | None = None
    importance_weight: float | None = None
    content_fingerprint: str = field(init=False)
    annotations_fingerprint: str = field(init=False)
    importance_weight_fingerprint: str = field(init=False)
    metadata_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, (str, bytes)):
            raise TypeError(f"content must be str or bytes, got {type(self.content).__name__}")
        if self.label is not None and not isinstance(self.label, str):
            raise TypeError("label must be str or None")
        if self.external_metadata is not None and not isinstance(self.external_metadata, dict):
            raise TypeError("external_metadata must be dict or None")
        metadata = copy.deepcopy(self.external_metadata)
        # Validate before the spec reaches the engine and detach it from the
        # caller's mutable object.
        fingerprint_json(metadata)
        object.__setattr__(self, "external_metadata", metadata)
        node_set: Any = self.node_set
        if node_set is None:
            nodes: tuple[str, ...] | None = None
        else:
            if isinstance(node_set, (str, bytes)):
                raise TypeError("node_set must be a sequence of strings, not str or bytes")
            nodes = tuple(node_set)
            if any(not isinstance(node, str) for node in nodes):
                raise TypeError("every node_set entry must be str")
            if any(not node.strip() or "\x00" in node for node in nodes):
                raise ValueError("node_set entries must be non-blank and contain no NUL")
            if len(set(nodes)) != len(nodes):
                raise ValueError("node_set entries must be unique")
        object.__setattr__(self, "node_set", nodes)
        weight = self.importance_weight
        if weight is not None:
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise TypeError("importance_weight must be a finite number or None")
            weight = float(weight)
            if not math.isfinite(weight):
                raise ValueError("importance_weight must be a finite number or None")
            object.__setattr__(self, "importance_weight", weight)
        object.__setattr__(self, "content_fingerprint", fingerprint_content(self.content))
        # node_set order is presentation detail; sorted for the fingerprint,
        # preserved as declared for the actual add() call.
        object.__setattr__(
            self,
            "annotations_fingerprint",
            fingerprint_json(
                {
                    "external_metadata": metadata,
                    "node_set": (sorted(nodes) if nodes is not None else None),
                }
            ),
        )
        object.__setattr__(
            self,
            "importance_weight_fingerprint",
            fingerprint_json(weight),
        )
        object.__setattr__(
            self,
            "metadata_fingerprint",
            fingerprint_json({"label": self.label}),
        )


def document_record_for(
    spec: CogneeDocumentSpec, *, data_id: uuid.UUID, processing_fingerprint: str
) -> DocumentRecord:
    """Build the tracking record a converged document would have."""
    return DocumentRecord(
        data_id=data_id,
        content_fingerprint=spec.content_fingerprint,
        annotations_fingerprint=spec.annotations_fingerprint,
        metadata_fingerprint=spec.metadata_fingerprint,
        processing_fingerprint=processing_fingerprint,
        importance_weight_fingerprint=spec.importance_weight_fingerprint,
        schema_version=RECORD_SCHEMA_VERSION,
    )


def _qualified_id(cls: type[Any] | None) -> str | None:
    if cls is None:
        return None
    return f"{cls.__module__}.{cls.__qualname__}"


def _model_schema_fingerprint(model: type[Any] | None) -> str | None:
    """Fingerprint a pydantic model's JSON schema when it exposes one.

    Captures structural changes to a graph model even when its qualified name
    stays the same. A model without ``model_json_schema`` falls back to its
    qualified name. A declared schema method that fails is an error: silently
    dropping the schema would make later model edits invisible.
    """
    if model is None:
        return None
    schema_fn = getattr(model, "model_json_schema", None)
    if schema_fn is None:
        return None
    try:
        return fingerprint_json(schema_fn())
    except Exception as exc:
        raise ValueError(
            f"could not derive JSON schema for graph model {_qualified_id(model)!r}"
        ) from exc
