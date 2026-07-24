"""Single import point for version-sensitive Cognee APIs (ADR-0007).

Everything cogindex needs from cognee that is not a stable top-level export
is imported here, guarded, with actionable errors. No other module imports
cognee internals directly (AGENTS.md hard rule #5). All imports happen
lazily on first use: importing cognee initializes its logging/telemetry, and
``import cogindex`` must not pay that cost.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import importlib.metadata
import inspect
import warnings
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from ._errors import CompatibilityError

__all__ = ["CogneeCompat", "configure_storage", "configured_models", "load"]

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


def configured_models() -> tuple[str | None, str | None]:
    """Best-effort read of the globally configured (llm, embedding) models.

    These live in cognee's env config, not in cognify() arguments, but they
    shape every derivative — so they belong in processing fingerprints
    (ADR-0005). Returns None entries when the config modules move.
    """
    llm_model: str | None = None
    embedding_model: str | None = None
    try:
        llm_config_module = importlib.import_module("cognee.infrastructure.llm.config")
        llm_model = llm_config_module.get_llm_config().llm_model
    except Exception:
        llm_model = None
    try:
        embedding_config_module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings.config"
        )
        embedding_model = embedding_config_module.get_embedding_config().embedding_model
    except Exception:
        embedding_model = None
    return llm_model, embedding_model


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
    try:
        default = inspect.signature(fn).parameters[param].default
    except (KeyError, TypeError, ValueError):
        return None
    return default if isinstance(default, type) else None
