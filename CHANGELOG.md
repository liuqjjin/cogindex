# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  an upstream-faithful `FakeCogneeRuntime` test double with fault injection.
- Locking: in-process provider and PostgreSQL advisory-lock provider
  (`cogindex[postgres]`), correctness independent of either (ADR-0006).
- Drift verification (`verify_dataset`) and environment checks (`doctor`).
- Test suite: unit matrix and identity goldens, a Hypothesis convergence state
  machine over an emulated engine-tracking contract (mutation-validated), an
  11-scenario deterministic fault matrix, a real-local-Cognee integration tier
  with deterministic LLM and embedding substitutes, an opt-in real-LLM tier,
  and a PostgreSQL lock tier. `tests/unit/test_compat.py` pins the upstream
  surface so an incompatible cognee release fails in CI rather than at
  runtime. 89% coverage across the tiers that need no external service.
- Seven-category benchmark harness with environment-fingerprinted reports,
  including a comparison against a hand-rolled Cognee integration that
  quantifies the superseded rows and stale graph entities cogindex avoids.
  Results and reproduction commands in `docs/benchmarks.md`.

### Performance

- A batch of derivative purges shares one Cognee dataset context instead of
  opening one per document. Cognee shuts its graph worker down when that
  context closes, on a thread join that measured 1.149s of a 1.183s
  `forget()` call, so the old behaviour made replacing two documents cost more
  than ingesting six. Five documents went from 13.6s to 3.1s, and the
  real-stack incremental-to-full ratio from 2.202 to 0.859. Running those
  deletions concurrently is faster still and leaves an orphaned graph node
  behind, so the loop stays sequential.
- Runnable examples: folder → knowledge graph quickstart (one-shot and
  live watch) and a shared-entity provenance demo; both run without
  credentials in deterministic mode.
- Full-repository upstream audit ledger with a machine-checked coverage
  gate, plus four upstream improvement proposals.
