"""The upstream surface ``_compat`` depends on, pinned against installed cognee.

Every capability asserted here is one cogindex would silently mis-handle if it
disappeared, so this is the file that should fail first when an upstream bump
breaks us (CONTRIBUTING documents re-running it on every version bump, and the
nightly workflow runs it against the newest releases).

Two kinds of assertion live here and they are worth keeping apart:

- structural checks against the real installed cognee, which are what the
  nightly job exists to break;
- behavior of ``_compat``'s own degradation paths under a moved config layout,
  driven by monkeypatching, since the point is that a moved module yields
  ``None`` rather than an exception.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from cogindex import CompatibilityError, _compat

# ---------------------------------------------------------------------------
# The capability set _compat.load() gates on
# ---------------------------------------------------------------------------


def test_load_succeeds_and_reports_a_version() -> None:
    info = _compat.load()
    assert info.version != "unknown"
    assert info.version.startswith("1.4."), (
        f"tested against cognee 1.4.x, found {info.version}; "
        "review the findings ledger before widening the supported range"
    )


def test_top_level_api_cogindex_calls_is_present() -> None:
    cognee = _compat.load().cognee
    for name in ("add", "cognify", "forget", "datasets"):
        assert hasattr(cognee, name), f"cognee.{name} disappeared"


def test_data_item_accepts_a_caller_supplied_data_id() -> None:
    # The single capability the whole connector rests on: without it, identity
    # is content-derived and in-place replacement is impossible (ADR-0002).
    data_item_cls = _compat.load().data_item_cls
    assert "data_id" in data_item_cls.__annotations__
    item = data_item_cls(data="text", data_id=None)
    assert hasattr(item, "data_id")


def test_data_item_is_still_absent_from_the_public_namespace() -> None:
    # Inverted assertion on purpose. upstream-proposals/0001 asks for this
    # export; when it lands, this test fails and tells us to delete the shim.
    cognee = _compat.load().cognee
    assert not hasattr(cognee, "DataItem"), (
        "cognee now exports DataItem: drop the deep import in _compat and "
        "close docs/upstream-proposals/0001"
    )


def test_forget_keeps_the_keyword_signature_the_delete_protocol_needs() -> None:
    cognee = _compat.load().cognee
    params = inspect.signature(cognee.forget).parameters
    for name in ("data_id", "dataset_id", "memory_only"):
        assert name in params
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_add_still_defaults_its_skip_gate_on() -> None:
    # ADR-0004's first amendment: both default True, and either one routes the
    # add through the incremental path that skips an already-COMPLETED data_id
    # entirely. LocalCogneeRuntime passes both as False for exactly this
    # reason. If upstream ever flips these defaults the override becomes
    # redundant rather than wrong, but we want to know.
    cognee = _compat.load().cognee
    params = inspect.signature(cognee.add).parameters
    assert params["incremental_loading"].default is True
    assert params["data_cache"].default is True


def test_cognify_pipeline_name_and_completion_status_literals_match_upstream() -> None:
    # _compat hardcodes these two strings; list_documents compares against
    # them to decide whether a document is fully processed.
    status_module = importlib.import_module("cognee.modules.pipelines.models.DataItemStatus")
    values = {member.value for member in status_module.DataItemStatus}
    assert _compat.COGNIFY_COMPLETE_STATUS in values

    pipeline_module = importlib.import_module("cognee.api.v1.cognify.cognify")
    source = inspect.getsource(pipeline_module)
    assert _compat.COGNIFY_PIPELINE_NAME in source


def test_only_dataset_not_found_is_classified_as_missing() -> None:
    # UnauthorizedDataAccessError and ValueError may mean a permission,
    # argument, or configuration failure. Treating them as absence would turn
    # a failed delete into false success.
    errors = _compat.load().dataset_missing_errors
    resolved = {error.__name__ for error in errors}
    assert resolved == {"DatasetNotFoundError"}


# ---------------------------------------------------------------------------
# Effective defaults folded into processing fingerprints (ADR-0005)
# ---------------------------------------------------------------------------


def test_cognify_defaults_are_readable_from_its_signature() -> None:
    info = _compat.load()
    # A None here is tolerated by the fingerprint (it just means coarser
    # invalidation), so assert on the mechanism, not on specific values.
    assert info.default_graph_model is None or isinstance(info.default_graph_model, type)
    assert info.default_chunker is None or isinstance(info.default_chunker, type)
    assert info.default_chunk_size is None or isinstance(info.default_chunk_size, int)


def test_configured_models_reports_model_and_vector_width() -> None:
    llm_model, embedding_model, dimensions = _compat.configured_models()
    assert llm_model
    assert embedding_model
    assert dimensions is None or dimensions > 0


def test_storage_roots_are_readable() -> None:
    data_root, system_root = _compat.storage_roots()
    assert data_root is not None
    assert system_root is not None


# ---------------------------------------------------------------------------
# Degradation: a moved config layout must yield None, never raise
# ---------------------------------------------------------------------------


def _break_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_on_import(name: str, *args: object, **kwargs: object) -> object:
        raise ImportError(f"simulated relocation of {name}")

    monkeypatch.setattr(importlib, "import_module", raise_on_import)


def test_configured_models_degrades_to_none_when_config_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_import(monkeypatch)
    assert _compat.configured_models() == (None, None, None)


def test_storage_roots_degrade_to_none_when_config_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_import(monkeypatch)
    assert _compat.storage_roots() == (None, None)


def test_credentials_present_degrades_to_unknown_when_config_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_import(monkeypatch)
    assert _compat.credentials_present() == (None, None)


def test_missing_cognee_raises_an_actionable_compatibility_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _compat.load.cache_clear()
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name)),
    )
    try:
        with pytest.raises(CompatibilityError, match="cognee is not installed"):
            _compat.load()
    finally:
        _compat.load.cache_clear()


def test_untested_cognee_version_warns_without_failing() -> None:
    with pytest.warns(UserWarning, match="tested against cognee"):
        _compat._warn_if_untested_version("9.9.9")


def test_unparseable_version_is_not_warned_about() -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _compat._warn_if_untested_version("not-a-version")
