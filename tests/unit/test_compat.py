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

import enum
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


def test_runtime_api_surface_is_callable_and_load_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cognee = _compat.load().cognee
    surfaces = (
        (
            cognee.add,
            ("data",),
            (
                "dataset_name",
                "user",
                "node_set",
                "dataset_id",
                "incremental_loading",
                "importance_weight",
                "data_cache",
            ),
        ),
        (
            cognee.cognify,
            (),
            (
                "datasets",
                "user",
                "graph_model",
                "chunker",
                "chunk_size",
                "custom_prompt",
                "temporal_cognify",
            ),
        ),
        (cognee.forget, (), ("data_id", "dataset_id", "memory_only", "user")),
        (cognee.datasets.list_datasets, (), ("user",)),
        (cognee.datasets.list_data, ("dataset_id",), ("user",)),
        (cognee.config.data_root_directory, ("data_root_directory",), ()),
        (cognee.config.system_root_directory, ("system_root_directory",), ()),
    )
    for callable_api, positional, keyword in surfaces:
        assert callable(callable_api)
        parameters = inspect.signature(callable_api).parameters
        assert set(positional) <= parameters.keys()
        assert set(keyword) <= parameters.keys()

    # Prove load() owns the gate rather than merely mirroring the currently
    # installed API in this test. A moved nested dataset method must fail before
    # LocalCogneeRuntime reaches its first sync.
    monkeypatch.setattr(cognee.datasets, "list_data", None)
    _compat.load.cache_clear()
    try:
        with pytest.raises(CompatibilityError, match=r"datasets\.list_data is not callable"):
            _compat.load()
    finally:
        _compat.load.cache_clear()


def test_remote_mode_check_reads_current_upstream_state() -> None:
    state = importlib.import_module("cognee.api.v1.serve.state")
    previous_client = state.get_remote_client()
    try:
        state.set_remote_client(None)
        _compat.ensure_local_sdk_mode()

        state.set_remote_client(object())
        with pytest.raises(CompatibilityError, match=r"await cognee\.disconnect"):
            _compat.ensure_local_sdk_mode()
    finally:
        state.set_remote_client(previous_client)


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


def test_configured_processing_inputs_cover_real_cognee_surfaces() -> None:
    inputs = _compat.configured_processing_inputs(
        uses_custom_graph_prompt=False,
        temporal_cognify=False,
    )

    assert inputs["cognee_version"].startswith("1.4.")
    assert inputs["llm"]["base"]["provider"]
    assert inputs["llm"]["base"]["model"]
    assert inputs["llm"]["extraction"]["model"]
    assert inputs["llm"]["summarization"]["model"]
    assert inputs["embedding"]["provider"]
    assert inputs["embedding"]["model"]
    assert inputs["embedding"]["dimensions"] > 0
    assert inputs["embedding"]["max_completion_tokens"] > 0
    assert inputs["cognify"]["classification_model"]["schema"]
    assert inputs["cognify"]["summarization_model"]["schema"]
    assert set(inputs["prompts"]) == {"classification", "graph", "summary"}
    assert all(inputs["prompts"].values())


def test_model_arg_sanitizer_recursively_excludes_secrets_and_execution_knobs() -> None:
    first = _compat._canonical_model_args(
        {
            "temperature": 0.2,
            "top_p": 0.9,
            "seed": 7,
            "logit_bias": {"42": -1},
            "nested": {
                "auth": "first-auth",
                "api_key": "first-secret",
                "accessToken": "first-access-token",
                "bearerToken": "first-bearer-token",
                "clientSecret": "first-client-secret",
                "endpoint": "https://first.invalid",
                "headers": {"Authorization": "Bearer first-secret"},
                "privateKey": "first-private-key",
                "timeout": 10,
                "retry_count": 2,
                "batch_size": 8,
                "rate_limit": 60,
                "embedding_rate_limit_enabled": True,
                "logger_name": "first",
                "max_retry_count": 2,
                "request_batch_size": 8,
            },
        }
    )
    second = _compat._canonical_model_args(
        {
            "temperature": 0.2,
            "top_p": 0.9,
            "seed": 7,
            "logit_bias": {"42": -1},
            "nested": {
                "auth": "second-auth",
                "api_key": "second-secret",
                "accessToken": "second-access-token",
                "bearerToken": "second-bearer-token",
                "clientSecret": "second-client-secret",
                "endpoint": "https://second.invalid",
                "headers": {"Authorization": "Bearer second-secret"},
                "privateKey": "second-private-key",
                "timeout": 90,
                "retry_count": 9,
                "batch_size": 64,
                "rate_limit": 1,
                "embedding_rate_limit_enabled": False,
                "logger_name": "second",
                "max_retry_count": 9,
                "request_batch_size": 64,
            },
        }
    )

    assert (
        first
        == second
        == {
            "temperature": 0.2,
            "top_p": 0.9,
            "seed": 7,
            "logit_bias": {"42": -1},
        }
    )


def test_model_arg_sanitizer_preserves_generation_and_provider_specific_args() -> None:
    assert _compat._canonical_model_args(
        {
            "parallel_tool_calls": True,
            "mirostat_tau": 5.0,
            "top_k": 40,
            "stop_token_ids": (1, 2),
        }
    ) == {
        "parallel_tool_calls": True,
        "mirostat_tau": 5.0,
        "top_k": 40,
        "stop_token_ids": [1, 2],
    }


def test_model_arg_sanitizer_filters_extra_body_as_parameter_namespace() -> None:
    assert _compat._canonical_model_args(
        {
            "extra_body": {
                "top_k": 40,
                "api_key": "secret",
                "endpoint": "https://provider.invalid",
            }
        }
    ) == {"extra_body": {"top_k": 40}}


def test_model_arg_sanitizer_preserves_schema_keys_and_content_urls() -> None:
    args = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "fetch",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "api_key": {"type": "string"},
                            "endpoint": {"type": "string", "format": "uri"},
                        },
                    },
                },
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "result",
                "schema": {"$id": "https://schemas.invalid/result", "type": "object"},
            },
        },
    }

    assert _compat._canonical_model_args(args) == args


def test_model_arg_sanitizer_fails_closed_for_url_under_unknown_field() -> None:
    with pytest.raises(ValueError, match="unrecognized field"):
        _compat._canonical_model_args({"resource": "https://example.invalid/model"})


def test_model_arg_sanitizer_fails_closed_for_ambiguous_secret_shaped_field() -> None:
    with pytest.raises(ValueError, match="secret-shaped"):
        _compat._canonical_model_args({"vendor_key": "could-be-secret-or-selector"})


def test_litellm_core_model_args_have_an_explicit_policy() -> None:
    from litellm.constants import OPENAI_CHAT_COMPLETION_PARAMS

    classified = (
        _compat._GENERATION_MODEL_ARG_FIELDS
        | _compat._NESTED_MODEL_ARG_FIELDS
        | _compat._EXCLUDED_MODEL_ARG_FIELDS
    )
    assert set(OPENAI_CHAT_COMPLETION_PARAMS) <= classified
    assert _compat._GENERATION_MODEL_ARG_FIELDS.isdisjoint(_compat._EXCLUDED_MODEL_ARG_FIELDS)
    assert "parallel_tool_calls" in _compat._GENERATION_MODEL_ARG_FIELDS
    assert "auth" in _compat._EXCLUDED_MODEL_ARG_FIELDS


def test_model_arg_sanitizer_preserves_enum_and_sequence_generation_values() -> None:
    class ResponseMode(enum.Enum):
        STRICT = "strict"

    assert _compat._canonical_model_args(
        {
            "response_mode": ResponseMode.STRICT,
            "stop": ("END", "STOP"),
            "logprobs": True,
        }
    ) == {
        "response_mode": "strict",
        "stop": ["END", "STOP"],
        "logprobs": True,
    }


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"temperature": float("nan")}, ValueError),
        ({1: "non-string key"}, TypeError),
        ({"response_format": object()}, TypeError),
    ],
)
def test_model_arg_sanitizer_rejects_noncanonical_values(
    args: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        _compat._canonical_model_args(args)


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


def test_configured_processing_inputs_fail_closed_when_config_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break_import(monkeypatch)
    with pytest.raises(CompatibilityError, match="explicit ProcessingConfig"):
        _compat.configured_processing_inputs(
            uses_custom_graph_prompt=False,
            temporal_cognify=False,
        )


def test_configured_processing_inputs_preserves_specific_compatibility_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = CompatibilityError("specific compatibility failure")

    def fail_load() -> _compat.CogneeCompat:
        raise expected

    monkeypatch.setattr(_compat, "load", fail_load)

    with pytest.raises(CompatibilityError) as caught:
        _compat.configured_processing_inputs(
            uses_custom_graph_prompt=False,
            temporal_cognify=False,
        )

    assert caught.value is expected


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
