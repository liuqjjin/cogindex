# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Disabled `baseline_comparison` until both comparison groups use isolated
  storage and the edit/delete corpus is corrected. The previous headline
  numbers are withdrawn.
- Treat `external_metadata` as derivative-affecting. Changes now purge and
  cognify instead of taking the label-only update path.
- Copy external metadata when a document is declared so later caller
  mutations cannot change the payload after its fingerprint is computed.
- Bumped the tracking record schema to 2 so records created under the old
  metadata classification are rebuilt.
- Recreate a document row when `importance_weight` changes because Cognee
  1.4 does not update that field on an existing `data_id`.
- Preserve every possible tracking record through precommit and add
  regressions for a sink that succeeds before the CocoIndex commit exits.
- Let permission, validation and ambiguous deletion errors propagate; only an
  explicit `DatasetNotFoundError` is treated as an idempotent no-op.
- Put system-managed dataset teardown under the same dataset lock as document
  batches.
- Bound PostgreSQL lock connection and polling with one timeout, validate lock
  parameters and DSNs, and preserve a sink exception if lock cleanup also
  fails.
- Remove upstream pipeline payloads from public exceptions so document content
  or credentials cannot be copied into application logs.
- Reject mutable or malformed document inputs before reconciliation, and fail
  target construction when model configuration or a declared graph schema
  cannot be fingerprinted.
- Require dataset APIs to receive a `ContextKey` instead of a raw string,
  restrict its persistent key to a logical-name grammar that rejects URL and
  DSN forms, validate identity coordinates even for empty verification sets,
  and avoid echoing persistent runtime keys in connector errors.
- Require explicit local storage roots, reject conflicting live runtime
  configurations and external root changes, support only the default logical
  tenant locally, and bind dataset names only to rows owned by the acting
  Cognee user.
- Make verification and doctor reports immutable, reject duplicate stored
  identities, and classify missing required model credentials as a failed
  health check.
- Let deterministic examples skip provider-credential checks so the no-key
  quickstart does not report false critical findings.
- Correct the tracking-store-loss runbook: a memory-only purge leaves stale
  raw rows, so an exclusively owned dataset must be hard-emptied before a full
  sync; shared datasets require manual recovery.
- Align teardown documentation and the fake runtime with Cognee's hard
  dataset forget, which removes the dataset record as well as its contents.
- Pin workflow actions to full commits, schedule updates for actions,
  `uv.lock` and pre-commit hooks, keep dependency auditing blocking except for
  the documented unpatched `diskcache` advisory, and strengthen the
  clean-wheel smoke test.
- Rewrote the primary README in Chinese and removed claims not supported by
  the current tests or benchmark evidence.

## [0.1.0] - 2026-07-25

First release. Nothing to compare against, so this describes what the package
does rather than what changed.

### Added

- Two-level CocoIndex custom target: `DatasetHandler` container (ownership,
  processing-config tracking, lossy child invalidation) and per-dataset
  `DocumentHandler` (upsert / replace / metadata-update / delete
  classification over possible previous records).
- Stable document identity: `uuid5` over injectively-encoded logical
  coordinates; content never participates (ADR-0002).
- Replace protocol: purge derivatives → re-add under the same `data_id` →
  one incremental cognify per changed batch (ADR-0004), applied
  deletes-first under a per-dataset lock.
- Configuration invalidation via per-document processing fingerprints plus
  dataset-level lossy invalidation (ADR-0005). Cognee's own incremental
  gate checks completion only.
- `CogneeRuntime` protocol with `LocalCogneeRuntime` (lazy database setup,
  absolutized storage roots, idempotent missing-tolerant deletes, add-side
  skip-gate bypass, false-success guard raising `CogneePipelineError`) and
  a behavior-focused `FakeCogneeRuntime` test double with fault injection.
- Locking: in-process provider and PostgreSQL advisory-lock provider
  (`cogindex[postgres]`).
- Drift verification (`verify_dataset`) and environment checks (`doctor`).
- Test suite: unit matrix and identity goldens, a Hypothesis convergence state
  machine over an emulated engine-tracking contract, an
  11-scenario deterministic fault matrix, a real-local-Cognee integration tier
  with deterministic LLM and embedding substitutes, an opt-in real-LLM tier,
  and a PostgreSQL lock tier. `tests/unit/test_compat.py` pins the upstream
  surface so an incompatible cognee release fails in CI rather than at
  runtime. 89% coverage across the tiers that need no external service.
- Seven-category benchmark harness with environment metadata in JSON and
  Markdown reports.

### Performance

- A batch of derivative purges shares one Cognee dataset context instead of
  opening and closing one for every document. The real integration test checks
  that a batch opens one context while still issuing one delete per document.
- Runnable examples: folder → knowledge graph quickstart (one-shot and
  live watch) and a shared-entity provenance demo; both run without
  credentials in deterministic mode.
- Repository inventory with targeted upstream review records and a
  machine-checked path-coverage gate, plus four improvement proposals.
