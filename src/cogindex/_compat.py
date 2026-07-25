"""Single import point for version-sensitive Cognee APIs (ADR-0007).

Everything cogindex needs from cognee that is not a stable top-level export
is imported here, guarded, with actionable errors. No other module imports
cognee internals directly (AGENTS.md hard rule #5). All imports happen
lazily on first use: importing cognee initializes its logging/telemetry, and
``import cogindex`` must not pay that cost.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import importlib
import importlib.metadata
import inspect
import uuid
import warnings
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from types import ModuleType
from typing import Any

from ._errors import CompatibilityError

__all__ = [
    "COGNIFY_COMPLETE_STATUS",
    "COGNIFY_PIPELINE_NAME",
    "CogneeCompat",
    "configure_storage",
    "configured_models",
    "credentials_present",
    "dataset_database_context",
    "default_user_id",
    "ensure_databases_ready",
    "load",
    "storage_roots",
]

# The per-item incremental gate (audited: modules/pipelines/models/
# DataItemStatus.py and the forget() status-reset path). The literal is the
# enum's value, stable across the supported range.
COGNIFY_PIPELINE_NAME = "cognify_pipeline"
COGNIFY_COMPLETE_STATUS = "DATA_ITEM_PROCESSING_COMPLETED"

_SUPPORTED_MINOR = (1, 4)

_DATA_ID_PROPOSAL = (
    "cogindex relies on DataItem(data_id=...) to control document identity; "
    "see docs/upstream-proposals/ for the proposal to export it publicly."
)


@dataclasses.dataclass(frozen=True)
class CogneeCompat:
    """Resolved, capability-checked handles into the installed cognee."""

    cognee: ModuleType
    version: str
    data_item_cls: type[Any]
    # Exceptions that mean "the dataset is gone / not visible" — treated as
    # success by idempotent delete paths (ADR-0004).
    dataset_missing_errors: tuple[type[BaseException], ...]
    # Effective defaults cognify() would use if we pass nothing, captured
    # from its signature so processing fingerprints track the *actual*
    # behavior of the installed version.
    default_graph_model: type[Any] | None
    default_chunker: type[Any] | None
    default_chunk_size: int | None


@functools.cache
def load() -> CogneeCompat:
    """Import cognee and verify every capability cogindex depends on.

    Raises CompatibilityError listing all problems at once.
    """
    try:
        cognee = importlib.import_module("cognee")
    except ImportError as exc:
        raise CompatibilityError(
            "cognee is not installed; install cogindex with its dependencies"
        ) from exc

    try:
        version = importlib.metadata.version("cognee")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    _warn_if_untested_version(version)

    problems: list[str] = []

    for name in ("add", "cognify", "forget", "datasets"):
        if not hasattr(cognee, name):
            problems.append(f"cognee.{name} is missing")

    data_item_cls: type[Any] | None = None
    try:
        data_item_module = importlib.import_module("cognee.tasks.ingestion.data_item")
        data_item_cls = data_item_module.DataItem
    except (ImportError, AttributeError):
        problems.append(
            "cognee.tasks.ingestion.data_item.DataItem is unavailable. " + _DATA_ID_PROPOSAL
        )
    if data_item_cls is not None and "data_id" not in getattr(data_item_cls, "__annotations__", {}):
        problems.append(
            "DataItem does not accept data_id, so stable external identity "
            "is impossible with this cognee version. " + _DATA_ID_PROPOSAL
        )

    if hasattr(cognee, "forget"):
        forget_params: Mapping[str, inspect.Parameter]
        try:
            forget_params = inspect.signature(cognee.forget).parameters
        except (TypeError, ValueError):
            forget_params = {}
        for param in ("data_id", "dataset_id", "memory_only"):
            if param not in forget_params:
                problems.append(f"cognee.forget() lacks the {param!r} parameter")

    if problems:
        raise CompatibilityError(
            f"installed cognee {version} is incompatible with cogindex:\n- " + "\n- ".join(problems)
        )
    if data_item_cls is None:  # pragma: no cover - unreachable, narrows type
        raise CompatibilityError("DataItem unavailable")

    return CogneeCompat(
        cognee=cognee,
        version=version,
        data_item_cls=data_item_cls,
        dataset_missing_errors=_dataset_missing_errors(),
        default_graph_model=_signature_default_type(cognee.cognify, "graph_model"),
        default_chunker=_signature_default_type(cognee.cognify, "chunker"),
        default_chunk_size=_signature_default_int(cognee.cognify, "chunk_size"),
    )


def configure_storage(data_root: str | None, system_root: str | None) -> None:
    """Point cognee's data/system storage at explicit directories.

    Without this, cognee defaults to directories inside its own installed
    package, which is unsuitable for anything but throwaway experiments.
    """
    cognee = load().cognee
    if data_root is not None:
        cognee.config.data_root_directory(data_root)
    if system_root is not None:
        cognee.config.system_root_directory(system_root)


def configured_models() -> tuple[str | None, str | None, int | None]:
    """Best-effort read of the globally configured models and vector width.

    Returns ``(llm_model, embedding_model, embedding_dimensions)``. These live
    in cognee's env config rather than in cognify() arguments, but they shape
    every derivative, so they belong in processing fingerprints (ADR-0005).
    Dimensions are read separately because they are settable independently of
    the model: the same embedding model at a different width invalidates every
    stored vector. Entries are None when the config layout moves.
    """
    llm_model: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    try:
        llm_config_module = importlib.import_module("cognee.infrastructure.llm.config")
        llm_model = llm_config_module.get_llm_config().llm_model
    except Exception:
        llm_model = None
    try:
        embedding_config_module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings.config"
        )
        embedding_config = embedding_config_module.get_embedding_config()
        embedding_model = embedding_config.embedding_model
        raw_dimensions = getattr(embedding_config, "embedding_dimensions", None)
        embedding_dimensions = int(raw_dimensions) if raw_dimensions is not None else None
    except Exception:
        embedding_model = None
        embedding_dimensions = None
    return llm_model, embedding_model, embedding_dimensions


def dataset_database_context(
    dataset_id: uuid.UUID, user_id: uuid.UUID | None
) -> AbstractAsyncContextManager[Any]:
    """Bind cognee's per-dataset database context for the duration of a block.

    A pure optimisation with a large payoff. Cognee scopes its graph and vector
    engines per dataset, and every public call that needs them opens this
    context and closes it again on the way out. Closing it shuts down the graph
    worker, which blocks on a thread join: measured at roughly 2.7 s, against
    about 0.07 s of actual work for a single-document ``forget``. Deleting a
    batch of documents therefore pays that teardown once per document unless
    something holds the context open across the whole batch, which is what this
    is for. Nesting is what makes it work: the inner contexts cognee opens for
    itself become no-ops while an outer one is live.

    Returns a null context when the module has moved, so the caller degrades to
    the slower behaviour rather than failing.
    """
    if user_id is None:
        return contextlib.nullcontext()
    try:
        module = importlib.import_module("cognee.context_global_variables")
        set_context = module.set_database_global_context_variables
    except (ImportError, AttributeError):
        return contextlib.nullcontext()
    context: AbstractAsyncContextManager[Any] = set_context(dataset_id, user_id)
    return context


async def default_user_id() -> uuid.UUID | None:
    """The id of the user cognee acts as when none was configured."""
    try:
        module = importlib.import_module("cognee.modules.users.methods")
        user = await module.get_default_user()
    except (ImportError, AttributeError):
        return None
    user_id = getattr(user, "id", None)
    return user_id if isinstance(user_id, uuid.UUID) else None


async def ensure_databases_ready() -> None:
    """Create cognee's relational/graph/vector structures if absent.

    Idempotent; upstream's own forget() runs the same setup defensively on
    every call ("In case there is no database...").
    """
    low_level = importlib.import_module("cognee.low_level")
    await low_level.setup()


def storage_roots() -> tuple[str | None, str | None]:
    """Best-effort read of cognee's configured (data, system) root paths."""
    try:
        base_config_module = importlib.import_module("cognee.base_config")
        base = base_config_module.get_base_config()
        return str(base.data_root_directory), str(base.system_root_directory)
    except Exception:
        return None, None


def credentials_present() -> tuple[bool | None, bool | None]:
    """Whether (llm, embedding) credentials look usable. None = unknown.

    "Usable" means an API key is set, or the provider is a local one that
    needs none (ollama / custom endpoints)."""
    keyless_providers = {"ollama", "custom"}
    llm_ok: bool | None
    embedding_ok: bool | None
    try:
        llm_config_module = importlib.import_module("cognee.infrastructure.llm.config")
        llm = llm_config_module.get_llm_config()
        llm_ok = bool(llm.llm_api_key) or llm.llm_provider in keyless_providers
    except Exception:
        llm_ok = None
    try:
        embedding_config_module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings.config"
        )
        embedding = embedding_config_module.get_embedding_config()
        embedding_ok = (
            bool(embedding.embedding_api_key)
            or embedding.embedding_provider in keyless_providers
            # Many providers fall back to the LLM key for embeddings.
            or (llm_ok is True and embedding.embedding_provider == "openai")
        )
    except Exception:
        embedding_ok = None
    return llm_ok, embedding_ok


def _warn_if_untested_version(version: str) -> None:
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return
    if (major, minor) != _SUPPORTED_MINOR:
        warnings.warn(
            f"cogindex is tested against cognee {'.'.join(map(str, _SUPPORTED_MINOR))}.x; "
            f"found {version}. Capability checks passed, but behavior may differ.",
            stacklevel=3,
        )


def _dataset_missing_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = []
    try:
        exceptions_module = importlib.import_module("cognee.modules.data.exceptions")
    except ImportError:
        return ()
    for name in ("DatasetNotFoundError", "UnauthorizedDataAccessError"):
        error_cls = getattr(exceptions_module, name, None)
        if isinstance(error_cls, type) and issubclass(error_cls, BaseException):
            errors.append(error_cls)
    return tuple(errors)


def _signature_default_type(fn: Any, param: str) -> type[Any] | None:
    default = _signature_default(fn, param)
    return default if isinstance(default, type) else None


def _signature_default_int(fn: Any, param: str) -> int | None:
    default = _signature_default(fn, param)
    return default if isinstance(default, int) and not isinstance(default, bool) else None


def _signature_default(fn: Any, param: str) -> Any:
    try:
        return inspect.signature(fn).parameters[param].default
    except (KeyError, TypeError, ValueError):
        return None
