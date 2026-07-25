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

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any

from cocoindex.connectorkits.target import ManagedBy

from . import _compat
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
    llm_model: str | None = None
    embedding_model: str | None = None
    # Configurable independently of the model (cognee reads EMBEDDING_DIMENSIONS
    # from the environment), and changing it invalidates every stored vector.
    embedding_dimensions: int | None = None
    extras: tuple[tuple[str, str], ...] = ()

    def fingerprint(self) -> str:
        return fingerprint_json(dataclasses.asdict(self))


def processing_config_from_profile(
    profile: CognifyProfile, *, include_runtime_models: bool = True
) -> ProcessingConfig:
    """Derive the declarative :class:`ProcessingConfig` from a profile.

    Every ``None`` in the profile is resolved to the value cognify would
    actually use, read out of the installed version's signature, so that
    upgrading cognee's own defaults shows up as a fingerprint change instead
    of passing unnoticed. The globally configured LLM and embedding settings
    are folded in for the same reason: they live in cognee's env config rather
    than in cognify() arguments, yet they shape every derivative.

    Set ``include_runtime_models=False`` when one flow must produce identical
    fingerprints across machines with different model configuration, accepting
    that model changes then no longer invalidate anything.
    """
    compat_info = _compat.load()
    graph_model = profile.graph_model or compat_info.default_graph_model
    chunker = profile.chunker or compat_info.default_chunker
    chunk_size = (
        profile.chunk_size if profile.chunk_size is not None else (compat_info.default_chunk_size)
    )
    llm_model: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    if include_runtime_models:
        llm_model, embedding_model, embedding_dimensions = _compat.configured_models()
    return ProcessingConfig(
        graph_model_id=_qualified_id(graph_model),
        graph_model_schema_fingerprint=_model_schema_fingerprint(graph_model),
        chunker_id=_qualified_id(chunker),
        chunk_size=chunk_size,
        custom_prompt_fingerprint=(
            fingerprint_content(profile.custom_prompt)
            if profile.custom_prompt is not None
            else None
        ),
        temporal_cognify=profile.temporal_cognify,
        llm_model=llm_model,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
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
    metadata_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_fingerprint", fingerprint_content(self.content))
        # node_set order is presentation detail; sorted for the fingerprint,
        # preserved as declared for the actual add() call.
        object.__setattr__(
            self,
            "annotations_fingerprint",
            fingerprint_json(
                {
                    "node_set": (sorted(self.node_set) if self.node_set is not None else None),
                    "importance_weight": self.importance_weight,
                }
            ),
        )
        object.__setattr__(
            self,
            "metadata_fingerprint",
            fingerprint_json({"label": self.label, "external_metadata": self.external_metadata}),
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
        schema_version=RECORD_SCHEMA_VERSION,
    )


def _qualified_id(cls: type[Any] | None) -> str | None:
    if cls is None:
        return None
    return f"{cls.__module__}.{cls.__qualname__}"


def _model_schema_fingerprint(model: type[Any] | None) -> str | None:
    """Fingerprint a pydantic model's JSON schema, best-effort.

    Captures structural changes to a graph model even when its qualified name
    stays the same. A silent None (schema not derivable) still leaves
    ``graph_model_id`` as a coarser invalidation signal.
    """
    if model is None:
        return None
    schema_fn = getattr(model, "model_json_schema", None)
    if schema_fn is None:
        return None
    try:
        return fingerprint_json(schema_fn())
    except Exception:
        return None
