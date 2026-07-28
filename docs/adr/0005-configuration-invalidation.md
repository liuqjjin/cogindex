# ADR-0005: Configuration invalidation

Status: accepted · Date: 2026-07-24

## Context

Cognee's incremental gate checks a single per-item status
(`pipeline_status[pipeline][dataset_id] == COMPLETED`). It carries **no
fingerprint of the prompt, graph model, chunker, LLM, or embedding model**,
changing any of these upstream re-processes nothing. Derivative correctness
under configuration change is therefore entirely this connector's job.

## Decision

A `processing_fingerprint` is computed over every input that shapes
derivatives:

- for the default pipeline, graph model identity plus a fingerprint of its
  JSON schema and custom extraction-prompt content,
- chunker identity, explicit chunk size and temporal mode,
- one `runtime_config_fingerprint` over Cognee's effective configuration:
  - Cognee version;
  - base, extraction and summarization LLM provider/model/API version;
    structured-output framework, instructor mode, configured temperature,
    fallback model, effective registry-capped token ceiling and generation
    arguments;
  - the base LLM model and effective token ceiling used by dynamic chunk
    sizing;
  - embedding provider/model/vector dimensions/input-token ceiling/tokenizer
    and API version;
  - classification and summarization model schemas plus triplet-embedding
    mode;
  - the contents of the graph, classification, summary and temporal prompts
    used by the selected pipeline;
  - ontology resolver, matching strategy and ontology file contents;
- cogindex's own record-schema version.

Values the caller leaves unset are resolved from the installed Cognee
signature before fingerprinting. Runtime configuration comes from Cognee 1.4's
actual `LLMConfig`, per-stage `stage_config()`, `EmbeddingConfig`,
`CognifyConfig`, ontology config and prompt loaders. A graph model with no
JSON-schema method falls back to its qualified class name, while a model whose
declared schema method fails is rejected.

Python class identity cannot describe an implementation edit that keeps the
same module and qualified name. The same applies to graph-model behavior that
changes without changing its JSON schema. Callers making either kind of change
must bump a stable revision in `ProcessingConfig.extras`, for example
`("implementation_revision", "2")`. This is explicit rather than based on
`inspect.getsource()`: source text is unavailable for some classes and is not a
stable deployment artifact.

The same rule applies to a mutable local model artifact. Replacing a llama.cpp
GGUF file at the same path does not change the path digest recorded by
automatic inspection. Deployments doing that must bump a content or release
revision in `ProcessingConfig.extras`, such as
`("llama_cpp_model_revision", "sha256:...")`. Hashing a multi-gigabyte model on
every target declaration would be a poor implicit contract.

`get_max_chunk_tokens()` is asynchronous because it constructs the configured
embedding engine. Target declaration stays synchronous, but Cognee's
`get_model_max_completion_tokens()` lookup is synchronous. For the base,
extraction and summarization clients, the fingerprint records the same value
Cognee gives the adapter: the smaller of the configured ceiling and LiteLLM's
registered model limit, or the configured ceiling when the model is unknown.
The dynamic-chunk entry uses that effective base-model limit; the embedding
input-token ceiling is recorded separately. A registry change therefore
invalidates only when it changes the limit Cognee actually uses.

Embedding width has a stricter rule. Cognee resolves registered models when it
builds `EmbeddingConfig`, then its connection probe may replace that value
with the length of the first returned vector when `EMBEDDING_DIMENSIONS` is
not explicitly set. A target declared before that probe would otherwise freeze
a fingerprint for a width the pipeline never uses. Automatic derivation
therefore accepts an explicit non-empty `EMBEDDING_DIMENSIONS`, or a
registry-known model whose current configured width agrees with the registry.
“Explicit” includes the process environment and Cognee's configured `.env`
file; the parsed value must agree with the live `EmbeddingConfig`. An
unregistered model without either source, and an unexplicit
configured/registry mismatch, both fail closed. The local runtime repeats this
validation around pipeline execution, so a connection probe that changes the
width cannot be followed by a successful tracking commit. The caller can make
a custom model's width explicit with `EMBEDDING_DIMENSIONS`.

Cognee's processing settings are process-global mutable objects. The
fingerprint is a target-construction snapshot, so callers must not mutate
model, prompt, ontology or embedding configuration between target construction
and sink completion. A deliberate configuration change starts a new flow run
so the target is declared with a new fingerprint.

If any required input cannot be read safely, target construction fails and
tells the caller to pass an explicit `ProcessingConfig`; continuing with a
partial fingerprint could leave old derivatives marked current.

Prompt and ontology paths are never fingerprinted as paths. Their contents are
digested, so moving an unchanged file does not rebuild a dataset and no local
path reaches tracking. When `custom_prompt` is non-empty, Cognee does not read
its default graph prompt, so that default is deliberately omitted. Normal and
temporal profiles likewise include only the prompts used by their selected
pipeline.

Cognee's temporal pipeline ignores `graph_model` and `custom_prompt`.
`CognifyProfile` therefore rejects an explicit graph model or a non-empty
custom prompt in temporal mode, and its processing fingerprint leaves the
corresponding fields unset. This avoids both a misleading declaration and a
full reprocessing caused by an input Cognee would not use.

Cognee selects a custom prompt by truthiness. An empty string is therefore
normalized to the same effective configuration as `None`: neither receives a
custom-prompt digest, and both continue to track the default graph prompt.

`include_runtime_models=False` remains an explicit escape hatch for callers
that require machine-independent fingerprints. Despite its historical name,
it now disables all automatic Cognee runtime-configuration invalidation, not
only model identifiers. Such a caller owns passing a changed explicit
`ProcessingConfig` when a runtime input changes.

The fingerprint uses canonical serialization (sorted keys, explicit types), so
dict ordering or equivalent representations cannot cause spurious changes.
Only the final digest is stored in `ProcessingConfig`; raw prompts, ontology
content, model configuration and file paths never enter a tracking record.

Two invalidation mechanisms are used **together**:

1. **Per-document:** the `processing_fingerprint` is stored in every document
   tracking record. `reconcile()` treats a fingerprint mismatch exactly like a
   content change → *replace* (purge derivatives via
   `forget(memory_only=True)`, re-add, re-cognify).
2. **Dataset-level:** when the dataset spec's processing configuration
   changes, the dataset handler also returns `child_invalidation="lossy"`,
   which makes the engine pass `prev_may_be_missing=True` to every child,
   forcing conservative replay even for documents whose tracking records the
   engine can no longer trust.

Mechanism 1 alone is sufficient in the common case; mechanism 2 covers the
uncertainty window around a torn dataset-level update and makes the behavior
explainable from either side. The redundancy is cheap (replays are
fingerprint-gated no-ops when nothing actually changed at the document level).

## What must NOT invalidate

Credentials, API keys, access tokens, endpoints/URLs, headers, lock providers,
rate limits, batch sizes, concurrency, timeouts, retries, streaming, telemetry
and log settings stay out of the fingerprint. Model arguments use three
ordered rules:

1. LiteLLM 1.93 generation fields are preserved first. This includes
   `parallel_tool_calls`; the word “parallel” does not make it an execution
   setting.
2. Explicit credential, transport and execution fields are removed. Broad
   substring matching is not used.
3. Other finite JSON-compatible fields are preserved so provider-specific
   sampling options are not lost. Unsupported values and ambiguous fields
   fail closed.

Nested parameter namespaces such as `extra_body` apply the same filtering
recursively. Structured generation payloads such as `tools` and
`response_format` are canonicalized as content instead: a JSON Schema property
named `api_key`, or a schema `$id` containing a URL, is part of the request
shape rather than a deployed credential or endpoint and must affect the
fingerprint.

A URL-like value under a recognized endpoint/URL field is excluded with that
field. The same value under an unknown model-argument name is ambiguous: it
might select output-producing content rather than a transport. Automatic
derivation fails closed instead of silently discarding it. An otherwise
unknown field ending in a secret-shaped `key` or `token` likewise fails closed;
recognized fields such as `auth`, `api_key` and `access_token` are excluded.

API version is included even though endpoint is not. It selects provider
request/response behavior and can affect structured output. An endpoint is a
deployment location; rotating it or a credential must not rebuild the graph.
If two services behind different endpoints implement different model
semantics, the caller must give them different model identifiers or pass an
explicit `ProcessingConfig`.

Both directions are pinned by tests, in
`tests/unit/test_records_and_spec.py` at the fingerprint level and
`tests/unit/test_engine_lifecycle.py` at the reconcile level:

- every field of `ProcessingConfig` changed one at a time produces a different
  fingerprint (parametrized, with a guard test asserting the parametrization
  covers every field, so a new field cannot be added without covering it);
- real Cognee 1.4 config mutations cover stage model/provider/API version,
  temperature/framework/model arguments, effective per-stage token limits,
  embedding and dynamic chunk inputs, prompt contents, Cognify models and
  ontology contents;
- credential, endpoint, rate-limit, batching and nested transport-argument
  changes leave the fingerprint unchanged;
- generation arguments, including `parallel_tool_calls`, provider-specific
  JSON fields and structured-output schemas, change it;
- unknown embedding dimensions and unexplicit registry conflicts fail closed;
- a derivative-affecting change purges and re-cognifies every document;
- a change to something outside `ProcessingConfig` produces an identical
  fingerprint and therefore issues no purge and no cognify;
- an identical re-run is a no-op with zero mutating calls.
