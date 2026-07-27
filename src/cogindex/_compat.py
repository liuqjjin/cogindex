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
import enum
import functools
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import math
import os
import re
import uuid
import warnings
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import ModuleType
from typing import Any

from ._errors import CompatibilityError

__all__ = [
    "COGNIFY_COMPLETE_STATUS",
    "COGNIFY_PIPELINE_NAME",
    "CogneeCompat",
    "configure_storage",
    "configured_processing_inputs",
    "credentials_present",
    "dataset_database_context",
    "ensure_databases_ready",
    "ensure_local_sdk_mode",
    "load",
    "resolve_default_user",
    "resolve_user",
    "storage_roots",
    "validate_embedding_dimensions",
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

_URL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")

# LiteLLM 1.93 accepts both OpenAI-compatible arguments and arbitrary
# provider-specific kwargs. Known generation fields must win over every
# transport/execution rule: ``parallel_tool_calls`` is derivative-affecting
# despite containing a word that is usually an execution knob.
_GENERATION_MODEL_ARG_FIELDS = frozenset(
    {
        "additional_drop_params",
        "allowed_openai_params",
        "api_version",
        "audio",
        "candidate_count",
        "context_management",
        "custom_llm_provider",
        "do_sample",
        "drop_params",
        "frequency_penalty",
        "function_call",
        "functions",
        "guided_choice",
        "guided_decoding_backend",
        "guided_grammar",
        "guided_json",
        "guided_regex",
        "include_server_side_tool_invocations",
        "length_penalty",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_new_tokens",
        "max_output_tokens",
        "max_tokens",
        "min_p",
        "modalities",
        "mock_response",
        "mock_tool_calls",
        "n",
        "num_beams",
        "parallel_tool_calls",
        "prediction",
        "presence_penalty",
        "reasoning_effort",
        "repetition_penalty",
        "response_format",
        "safety_settings",
        "seed",
        "stop",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_k",
        "top_logprobs",
        "top_p",
        "typical_p",
        "verbosity",
        "web_search_options",
    }
)

# These values contain nested parameter namespaces rather than opaque
# generation payloads. Their nested credentials and endpoints still need to
# be removed while model names and provider-specific generation options remain.
_NESTED_MODEL_ARG_FIELDS = frozenset(
    {
        "context_window_fallback_dict",
        "context_window_fallbacks",
        "extra_body",
        "fallbacks",
        "model_alias_map",
        "provider_options",
    }
)

_EXCLUDED_MODEL_ARG_FIELDS = frozenset(
    {
        "acompletion",
        "api_base",
        "api_key",
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "aws_access_key_id",
        "aws_bedrock_project_id",
        "aws_bedrock_runtime_endpoint",
        "aws_external_id",
        "aws_profile_name",
        "aws_region_name",
        "aws_role_name",
        "aws_secret_access_key",
        "aws_session_name",
        "aws_session_token",
        "aws_sts_endpoint",
        "aws_web_identity_token",
        "azure_ad_token",
        "azure_password",
        "azure_scope",
        "azure_username",
        "base_url",
        "batch_size",
        "bearer_token",
        "client",
        "client_id",
        "client_secret",
        "completion_call_id",
        "cookie",
        "cookies",
        "cooldown_time",
        "default_headers",
        "deployment_id",
        "enable_json_schema_validation",
        "endpoint",
        "extra_headers",
        "force_timeout",
        "gcs_bucket_name",
        "headers",
        "http_proxy",
        "https_proxy",
        "idempotency_key",
        "input_cost_per_second",
        "input_cost_per_token",
        "itpm",
        "litellm_call_id",
        "litellm_credential_name",
        "litellm_logging_obj",
        "litellm_session_id",
        "litellm_trace_id",
        "logger_fn",
        "max_budget",
        "max_parallel_requests",
        "max_retries",
        "metadata",
        "mock_delay",
        "mock_timeout",
        "model_info",
        "model_list",
        "no_log",
        "num_retries",
        "organization",
        "otpm",
        "output_cost_per_second",
        "output_cost_per_token",
        "parallel",
        "parallelism",
        "password",
        "port",
        "preset_cache_key",
        "prompt_cache_key",
        "prompt_cache_retention",
        "provider_specific_header",
        "proxy",
        "proxy_server_request",
        "request_timeout",
        "routing_key",
        "rpm",
        "safety_identifier",
        "service_tier",
        "shared_session",
        "ssl_verify",
        "store",
        "stream",
        "stream_options",
        "stream_timeout",
        "tenant_id",
        "timeout",
        "tpm",
        "traceparent",
        "user",
        "verbose",
        "vertex_ai_credentials",
        "vertex_ai_location",
        "vertex_ai_project",
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
    }
)

_EXCLUDED_MODEL_ARG_TOKENS = frozenset(
    {
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "batch",
        "certificate",
        "concurrency",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "endpoint",
        "header",
        "headers",
        "host",
        "logger",
        "logging",
        "parallelism",
        "password",
        "port",
        "proxy",
        "retry",
        "retries",
        "secret",
        "stream",
        "streaming",
        "telemetry",
        "timeout",
        "trace",
        "tracing",
        "uri",
        "url",
        "worker",
        "workers",
    }
)
_EXCLUDED_MODEL_ARG_TOKEN_SEQUENCES = (
    ("access", "key"),
    ("access", "token"),
    ("api", "base"),
    ("api", "key"),
    ("auth", "token"),
    ("base", "url"),
    ("bearer", "token"),
    ("cache", "key"),
    ("csrf", "token"),
    ("encryption", "key"),
    ("id", "token"),
    ("idempotency", "key"),
    ("log", "file"),
    ("log", "format"),
    ("log", "handler"),
    ("log", "level"),
    ("log", "path"),
    ("parallel", "requests"),
    ("private", "key"),
    ("public", "key"),
    ("rate", "limit"),
    ("refresh", "token"),
    ("routing", "key"),
    ("secret", "key"),
    ("session", "token"),
    ("web", "identity", "token"),
)
_AMBIGUOUS_SECRET_SUFFIXES = frozenset({"key", "token"})
_OMIT_MODEL_ARG = object()


@dataclasses.dataclass(frozen=True)
class CogneeCompat:
    """Resolved, capability-checked handles into the installed cognee."""

    cognee: ModuleType
    version: str
    data_item_cls: type[Any]
    # Exceptions that unambiguously mean "the dataset is gone", treated as
    # success by idempotent delete paths. Authorization and generic validation
    # errors must not be collapsed into absence (ADR-0004).
    dataset_missing_errors: tuple[type[BaseException], ...]
    # Effective defaults cognify() would use if we pass nothing, captured
    # from its signature so processing fingerprints track the *actual*
    # behavior of the installed version.
    default_graph_model: type[Any] | None
    default_chunker: type[Any] | None
    default_chunk_size: int | None
    # Cognee's public operations silently route through its REST client after
    # cognee.serve(). This callable reads that process-global state on every
    # invocation, so a runtime also notices serve() called after construction.
    remote_mode_check: Callable[[], bool]


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

    remote_mode_check: Callable[[], bool] | None = None
    try:
        serve_state_module = importlib.import_module("cognee.api.v1.serve.state")
        candidate = serve_state_module.is_remote_mode
    except (ImportError, AttributeError):
        problems.append(
            "cognee.api.v1.serve.state.is_remote_mode is unavailable, so "
            "LocalCogneeRuntime cannot prevent accidental REST routing"
        )
    else:
        if callable(candidate):
            remote_mode_check = candidate
        else:
            problems.append("cognee.api.v1.serve.state.is_remote_mode is not callable")

    if hasattr(cognee, "forget"):
        forget_params: Mapping[str, inspect.Parameter]
        try:
            forget_params = inspect.signature(cognee.forget).parameters
        except (TypeError, ValueError):
            forget_params = {}
        for param in ("data_id", "dataset_id", "memory_only"):
            if param not in forget_params:
                problems.append(f"cognee.forget() lacks the {param!r} parameter")

    try:
        user_methods = importlib.import_module("cognee.modules.users.methods")
        user_model_module = importlib.import_module("cognee.modules.users.models.User")
    except ImportError:
        problems.append(
            "Cognee user resolution APIs are unavailable, so LocalCogneeRuntime "
            "cannot bind a stable user and tenant scope"
        )
    else:
        for name in ("get_default_user", "get_user"):
            if not callable(getattr(user_methods, name, None)):
                problems.append(f"cognee.modules.users.methods.{name} is unavailable")
        user_cls = getattr(user_model_module, "User", None)
        if user_cls is None or not hasattr(user_cls, "tenant_id"):
            problems.append(
                "Cognee User.tenant_id is unavailable, so physical tenant "
                "identity cannot be derived safely"
            )

    if problems:
        raise CompatibilityError(
            f"installed cognee {version} is incompatible with cogindex:\n- " + "\n- ".join(problems)
        )
    if data_item_cls is None:  # pragma: no cover - unreachable, narrows type
        raise CompatibilityError("DataItem unavailable")
    if remote_mode_check is None:  # pragma: no cover - unreachable, narrows type
        raise CompatibilityError("Cognee remote-mode check unavailable")

    return CogneeCompat(
        cognee=cognee,
        version=version,
        data_item_cls=data_item_cls,
        dataset_missing_errors=_dataset_missing_errors(),
        default_graph_model=_signature_default_type(cognee.cognify, "graph_model"),
        default_chunker=_signature_default_type(cognee.cognify, "chunker"),
        default_chunk_size=_signature_default_int(cognee.cognify, "chunk_size"),
        remote_mode_check=remote_mode_check,
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


def configured_processing_inputs(
    *, uses_custom_graph_prompt: bool, temporal_cognify: bool
) -> dict[str, Any]:
    """Read the Cognee settings that can change cognify derivatives.

    The returned mapping is canonical and contains content/schema digests in
    place of prompt, ontology, and model-schema bodies. Credentials, network
    locations, and execution knobs are deliberately absent. Callers hash this
    mapping once more before putting it in :class:`ProcessingConfig`; no raw
    Cognee configuration reaches a tracking record (ADR-0005).

    This is fail-closed. An incomplete fingerprint is worse than refusing to
    construct a target because it can mark stale graph/vector data as current.
    """
    try:
        llm_config_module = importlib.import_module("cognee.infrastructure.llm.config")
        embedding_config_module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings.config"
        )
        cognify_config_module = importlib.import_module("cognee.modules.cognify.config")
        ontology_config_module = importlib.import_module(
            "cognee.modules.ontology.ontology_env_config"
        )
        llm_utils_module = importlib.import_module("cognee.infrastructure.llm.utils")
        prompt_module = importlib.import_module("cognee.infrastructure.llm.prompts")

        llm_config = llm_config_module.get_llm_config()
        embedding_config = embedding_config_module.get_embedding_config()
        cognify_config = cognify_config_module.get_cognify_config()
        ontology_config = ontology_config_module.get_ontology_env_config()
        resolve_llm_max = llm_utils_module.get_model_max_completion_tokens
        if not callable(resolve_llm_max):
            raise TypeError("Cognee get_model_max_completion_tokens is not callable")

        framework = _required_str(llm_config, "structured_output_framework")
        base_llm = _llm_derivative_inputs(llm_config, resolve_llm_max)
        llm_inputs: dict[str, Any] = {
            "framework": framework.lower(),
            "instructor_mode": str(getattr(llm_config, "llm_instructor_mode", "")).lower(),
            "temperature": _finite_number(llm_config, "llm_temperature"),
            "model_args": _canonical_model_args(getattr(llm_config, "llm_args", None)),
            "base": base_llm,
            # get_max_chunk_tokens() uses the base LLM client rather than the
            # extraction-stage override. This is the same registry-capped
            # ceiling that Cognee puts on that client.
            "dynamic_chunk": {
                "provider": base_llm["provider"],
                "model": base_llm["model"],
                "max_completion_tokens": base_llm["max_completion_tokens"],
            },
        }
        if not temporal_cognify:
            stage_config = llm_config.stage_config
            if not callable(stage_config):
                raise TypeError("LLMConfig.stage_config is not callable")
            llm_inputs["extraction"] = _llm_derivative_inputs(
                stage_config("extraction"), resolve_llm_max
            )
            llm_inputs["summarization"] = _llm_derivative_inputs(
                stage_config("summarization"), resolve_llm_max
            )
        if framework.lower() == "baml":
            llm_inputs["baml"] = {
                "provider": _required_str(llm_config, "baml_llm_provider"),
                "model": _required_str(llm_config, "baml_llm_model"),
                "temperature": _finite_number(llm_config, "baml_llm_temperature"),
                "api_version": _optional_scalar(llm_config, "baml_llm_api_version"),
            }

        embedding_provider = _required_str(embedding_config, "embedding_provider")
        embedding_model = _required_str(embedding_config, "embedding_model")
        embedding_inputs = {
            "provider": embedding_provider,
            "model": embedding_model,
            "dimensions": _resolved_embedding_dimensions(
                embedding_config_module,
                embedding_config,
                provider=embedding_provider,
                model=embedding_model,
            ),
            "max_completion_tokens": _positive_int(
                embedding_config, "embedding_max_completion_tokens"
            ),
            "tokenizer": _optional_scalar(embedding_config, "huggingface_tokenizer"),
            "api_version": _optional_scalar(embedding_config, "embedding_api_version"),
        }

        prompts = {
            "classification": _fixed_prompt_digest(prompt_module, "classify_content.txt"),
        }
        cognify_inputs: dict[str, Any] = {
            "classification_model": _model_descriptor(cognify_config.classification_model),
        }
        ontology_inputs: dict[str, Any] | None = None
        if temporal_cognify:
            prompts["temporal_graph"] = _configured_prompt_digest(
                prompt_module, llm_config, "temporal_graph_prompt_path"
            )
            prompts["event_entity"] = _configured_prompt_digest(
                prompt_module, llm_config, "event_entity_prompt_path"
            )
        else:
            if not uses_custom_graph_prompt:
                prompts["graph"] = _configured_prompt_digest(
                    prompt_module, llm_config, "graph_prompt_path"
                )
            prompts["summary"] = _fixed_prompt_digest(prompt_module, "summarize_content.txt")
            cognify_inputs.update(
                {
                    "summarization_model": _model_descriptor(cognify_config.summarization_model),
                    "triplet_embedding": _required_bool(cognify_config, "triplet_embedding"),
                }
            )
            ontology_inputs = {
                "resolver": _required_str(ontology_config, "ontology_resolver"),
                "matching_strategy": _required_str(ontology_config, "matching_strategy"),
                "content": _ontology_content_digests(
                    _required_optional_str(ontology_config, "ontology_file_path")
                ),
            }

        result: dict[str, Any] = {
            "cognee_version": load().version,
            "llm": llm_inputs,
            "embedding": embedding_inputs,
            "cognify": cognify_inputs,
            "prompts": prompts,
        }
        if ontology_inputs is not None:
            result["ontology"] = ontology_inputs
        # Validate the whole payload here. This catches a newly introduced
        # non-canonical value before _spec can accidentally persist a partial
        # or process-specific representation.
        json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return result
    except CompatibilityError:
        raise
    except Exception as exc:
        raise CompatibilityError(
            "could not read Cognee's derivative-affecting runtime configuration; "
            "pass an explicit ProcessingConfig to dataset_target() if automatic "
            "configuration invalidation cannot inspect this installation"
        ) from exc


def _llm_derivative_inputs(
    config: Any,
    resolve_model_max: Callable[[str], object],
) -> dict[str, Any]:
    provider = _required_str(config, "llm_provider")
    model = _required_str(config, "llm_model")
    configured_max = _positive_int(config, "llm_max_completion_tokens")
    registry_max = resolve_model_max(model)
    if registry_max is not None and (
        isinstance(registry_max, bool) or not isinstance(registry_max, int) or registry_max <= 0
    ):
        raise TypeError(
            f"Cognee get_model_max_completion_tokens returned a non-positive integer for {model!r}"
        )
    effective_max = (
        min(configured_max, registry_max) if registry_max is not None else configured_max
    )
    inputs: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "api_version": _optional_scalar(config, "llm_api_version"),
        "max_completion_tokens": effective_max,
        "fallback_model": _optional_scalar(config, "fallback_model"),
    }
    if provider.lower() == "llama_cpp":
        # Do not retain the raw model path. The path string digest still
        # notices selecting a different local model without reading a
        # multi-gigabyte weight file during target declaration.
        model_path = _required_optional_str(config, "llama_cpp_model_path")
        inputs["llama_cpp"] = {
            "model_path": _digest_text(model_path) if model_path else None,
            "context_tokens": _positive_int(config, "llama_cpp_n_ctx"),
            "chat_format": _required_str(config, "llama_cpp_chat_format"),
        }
    return inputs


def _resolved_embedding_dimensions(
    config_module: ModuleType,
    config: Any,
    *,
    provider: str,
    model: str,
) -> int:
    configured = _positive_int(config, "embedding_dimensions")
    explicit = _explicit_embedding_dimensions(config)
    if explicit is not None:
        # This is cogindex's declared contract. Cognee 1.4's probe checks
        # os.getenv() directly, so it may still rewrite a value that came only
        # from a Pydantic .env file; the post-pipeline guard rejects any such
        # rewrite when it disagrees with this contract.
        if configured != explicit:
            raise CompatibilityError(
                "Cognee's live embedding dimensions disagree with explicit "
                "EMBEDDING_DIMENSIONS; restore the configured width before "
                "running a pipeline"
            )
        return configured

    resolve_dimensions = getattr(config_module, "_resolve_embedding_dimensions", None)
    if not callable(resolve_dimensions):
        raise TypeError("Cognee embedding dimension registry resolver is unavailable")
    registry_dimensions = resolve_dimensions(provider, model)
    if registry_dimensions is None:
        raise CompatibilityError(
            "Cognee embedding dimensions cannot be verified for unregistered "
            f"model {model!r}; set EMBEDDING_DIMENSIONS to the model's actual "
            "vector width before constructing or running this target"
        )
    if (
        isinstance(registry_dimensions, bool)
        or not isinstance(registry_dimensions, int)
        or registry_dimensions <= 0
    ):
        raise TypeError("Cognee embedding dimension resolver returned an invalid value")
    if configured != registry_dimensions:
        raise CompatibilityError(
            "Cognee's configured embedding dimensions disagree with its model "
            f"registry for {model!r}; set EMBEDDING_DIMENSIONS explicitly to "
            "the model's actual vector width"
        )
    return configured


def _explicit_embedding_dimensions(config: Any) -> int | None:
    raw = os.getenv("EMBEDDING_DIMENSIONS")
    if raw is None:
        raw = _dotenv_setting(config, "EMBEDDING_DIMENSIONS")
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIMENSIONS must be a positive integer") from exc
    if value <= 0:
        raise ValueError("EMBEDDING_DIMENSIONS must be a positive integer")
    return value


def _dotenv_setting(config: Any, field: str) -> str | None:
    model_config = getattr(config, "model_config", {})
    env_files = model_config.get("env_file", ".env")
    if env_files is None:
        return None
    candidates = (env_files,) if isinstance(env_files, (str, os.PathLike)) else tuple(env_files)

    dotenv = importlib.import_module("dotenv")
    found: str | None = None
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        values = dotenv.dotenv_values(path)
        for key, value in values.items():
            if key.upper() == field and value is not None:
                found = value
    return found


def validate_embedding_dimensions() -> int:
    """Return Cognee's verified embedding width or fail before a write commits.

    Local runtimes call this around a pipeline run because Cognee's connection
    probe may rewrite an unexplicit dimension after target declaration.
    """
    try:
        config_module = importlib.import_module(
            "cognee.infrastructure.databases.vector.embeddings.config"
        )
        config = config_module.get_embedding_config()
        provider = _required_str(config, "embedding_provider")
        model = _required_str(config, "embedding_model")
        return _resolved_embedding_dimensions(
            config_module,
            config,
            provider=provider,
            model=model,
        )
    except CompatibilityError:
        raise
    except Exception as exc:
        raise CompatibilityError(
            "could not verify Cognee embedding dimensions; set "
            "EMBEDDING_DIMENSIONS explicitly to the model's actual vector width"
        ) from exc


def _configured_prompt_digest(prompt_module: ModuleType, config: Any, field: str) -> str:
    prompt_path = _required_str(config, field)
    if os.path.isabs(prompt_path):
        base_directory = os.path.dirname(prompt_path)
        filename = os.path.basename(prompt_path)
    else:
        base_directory = None
        filename = prompt_path
    render_prompt = prompt_module.render_prompt
    if not callable(render_prompt):
        raise TypeError("cognee prompt renderer is not callable")
    rendered = render_prompt(filename, {}, base_directory=base_directory)
    if not isinstance(rendered, str):
        raise TypeError(f"rendered prompt {field!r} is not text")
    return _digest_text(rendered)


def _fixed_prompt_digest(prompt_module: ModuleType, filename: str) -> str:
    read_prompt = prompt_module.read_query_prompt
    if not callable(read_prompt):
        raise TypeError("cognee prompt reader is not callable")
    content = read_prompt(filename)
    if not isinstance(content, str):
        raise FileNotFoundError(f"could not read Cognee prompt {filename!r}")
    return _digest_text(content)


def _ontology_content_digests(configured_paths: str | None) -> list[str]:
    if not configured_paths:
        return []
    paths = [part.strip() for part in configured_paths.split(",")]
    if any(not path for path in paths):
        raise ValueError("ontology_file_path contains an empty path")
    return [_digest_bytes(Path(path).read_bytes()) for path in paths]


def _model_descriptor(model: Any) -> dict[str, str | None]:
    if not isinstance(model, type):
        raise TypeError(f"configured cognify model must be a type, got {type(model).__name__}")
    schema_method = getattr(model, "model_json_schema", None)
    schema_digest: str | None = None
    if schema_method is not None:
        if not callable(schema_method):
            raise TypeError("configured model's model_json_schema is not callable")
        schema_digest = _digest_canonical(schema_method())
    return {
        "id": f"{model.__module__}.{model.__qualname__}",
        "schema": schema_digest,
    }


def _canonical_model_args(value: Any) -> Any:
    """Copy model-generation args while removing transport and secret knobs."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("model args must be a mapping")
    canonical = _canonical_model_arg_mapping(value)
    return {} if canonical is _OMIT_MODEL_ARG else canonical


def _canonical_model_arg_mapping(value: Mapping[Any, Any]) -> Any:
    normalized: dict[str, Any] = {}
    omitted = False
    for raw_key, item in value.items():
        if not isinstance(raw_key, str):
            raise TypeError("model arg mappings must use string keys")
        kind = _model_arg_kind(raw_key)
        if kind == "excluded":
            omitted = True
            continue
        if kind == "generation":
            normalized[raw_key] = _canonical_generation_value(item)
            continue

        canonical = _canonical_model_arg_value(item)
        if canonical is _OMIT_MODEL_ARG:
            omitted = True
            continue
        normalized[raw_key] = canonical

    if not normalized and omitted:
        return _OMIT_MODEL_ARG
    return normalized


def _canonical_model_arg_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and _URL_PATTERN.match(value.strip()):
            raise ValueError(
                "URL-like model argument appears under an unrecognized field; "
                "classify it explicitly before automatic fingerprinting"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("model args must not contain NaN or infinity")
        return value
    if isinstance(value, enum.Enum):
        return _canonical_model_arg_value(value.value)
    if isinstance(value, Mapping):
        return _canonical_model_arg_mapping(value)
    if isinstance(value, (list, tuple)):
        normalized_items: list[Any] = []
        omitted = False
        for item in value:
            canonical = _canonical_model_arg_value(item)
            if canonical is _OMIT_MODEL_ARG:
                omitted = True
                continue
            normalized_items.append(canonical)
        if not normalized_items and omitted:
            return _OMIT_MODEL_ARG
        return normalized_items
    raise TypeError(
        f"model args contain unsupported {type(value).__name__}; use JSON-compatible values"
    )


def _canonical_generation_value(value: Any) -> Any:
    """Canonicalize an output-shaping payload without treating schema keys as secrets."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("model args must not contain NaN or infinity")
        return value
    if isinstance(value, enum.Enum):
        return _canonical_generation_value(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("model arg mappings must use string keys")
            normalized[raw_key] = _canonical_generation_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_generation_value(item) for item in value]
    raise TypeError(
        f"model args contain unsupported {type(value).__name__}; use JSON-compatible values"
    )


def _model_arg_kind(key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in _GENERATION_MODEL_ARG_FIELDS:
        return "generation"
    if normalized in _NESTED_MODEL_ARG_FIELDS:
        return "nested"
    if normalized in _EXCLUDED_MODEL_ARG_FIELDS:
        return "excluded"

    tokens = normalized.split("_")
    if _EXCLUDED_MODEL_ARG_TOKENS.intersection(tokens) or any(
        _contains_token_sequence(tokens, sequence)
        for sequence in _EXCLUDED_MODEL_ARG_TOKEN_SEQUENCES
    ):
        return "excluded"
    if tokens and tokens[-1] in _AMBIGUOUS_SECRET_SUFFIXES:
        raise ValueError(
            f"model argument {key!r} is secret-shaped but unrecognized; "
            "classify it explicitly before automatic fingerprinting"
        )
    return "nested"


def _contains_token_sequence(tokens: list[str], sequence: tuple[str, ...]) -> bool:
    width = len(sequence)
    return any(tuple(tokens[index : index + width]) == sequence for index in range(len(tokens)))


def _required_str(config: Any, field: str) -> str:
    value = getattr(config, field)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{type(config).__name__}.{field} must be non-empty text")
    return value


def _required_optional_str(config: Any, field: str) -> str | None:
    value = getattr(config, field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{type(config).__name__}.{field} must be text or None")
    return value


def _optional_scalar(config: Any, field: str) -> str | int | float | bool | None:
    value = getattr(config, field)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"{type(config).__name__}.{field} is not a canonical scalar")


def _finite_number(config: Any, field: str) -> int | float:
    value = getattr(config, field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{type(config).__name__}.{field} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{type(config).__name__}.{field} must be finite")
    return value


def _positive_int(config: Any, field: str) -> int:
    value = getattr(config, field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{type(config).__name__}.{field} must be a positive integer")
    return value


def _required_bool(config: Any, field: str) -> bool:
    value = getattr(config, field)
    if not isinstance(value, bool):
        raise TypeError(f"{type(config).__name__}.{field} must be bool")
    return value


def _digest_canonical(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return _digest_bytes(payload)


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _digest_bytes(value: bytes) -> str:
    return hashlib.blake2b(value, digest_size=16).hexdigest()


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


async def resolve_default_user() -> Any:
    """Return the current default Cognee User object.

    Callers pass this exact object into the SDK operation they are about to
    execute. Returning only its id would let the SDK resolve the default user
    again after its active tenant changed.
    """
    load()
    module = importlib.import_module("cognee.modules.users.methods")
    return await module.get_default_user()


async def resolve_user(user_id: uuid.UUID) -> Any:
    """Refresh one explicit Cognee User, including its active tenant."""
    if not isinstance(user_id, uuid.UUID):
        raise TypeError(f"user_id must be UUID, got {type(user_id).__name__}")
    load()
    module = importlib.import_module("cognee.modules.users.methods")
    return await module.get_user(user_id)


async def ensure_databases_ready() -> None:
    """Create cognee's relational/graph/vector structures if absent.

    Idempotent; upstream's own forget() runs the same setup defensively on
    every call ("In case there is no database...").
    """
    low_level = importlib.import_module("cognee.low_level")
    await low_level.setup()


def ensure_local_sdk_mode() -> None:
    """Reject Cognee's process-global REST routing before local SDK work.

    ``cognee.serve()`` changes the behavior of top-level ``add``, ``cognify``
    and ``forget`` calls in-place. Its REST add endpoint cannot carry the
    caller-supplied ``data_id`` that cogindex's identity contract requires.
    """
    if load().remote_mode_check():
        raise CompatibilityError(
            "LocalCogneeRuntime cannot run while Cognee remote mode is active: "
            "the REST add endpoint cannot preserve cogindex's caller-supplied "
            "data_id. Call `await cognee.disconnect()` before using this runtime."
        )


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
    error_cls = getattr(exceptions_module, "DatasetNotFoundError", None)
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
