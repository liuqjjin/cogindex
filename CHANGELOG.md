# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-24

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
  dataset-level lossy invalidation (ADR-0005) — Cognee's own incremental
  gate checks completion only.
- `CogneeRuntime` protocol with `LocalCogneeRuntime` (lazy database setup,
  absolutized storage roots, idempotent missing-tolerant deletes, add-side
  skip-gate bypass, false-success guard raising `CogneePipelineError`) and
  an upstream-faithful `FakeCogneeRuntime` test double with fault injection.
- Locking: in-process provider and PostgreSQL advisory-lock provider
  (`cogindex[postgres]`), correctness independent of either (ADR-0006).
- Drift verification (`verify_dataset`) and environment checks (`doctor`).
- Test suite: unit matrix + identity goldens, Hypothesis convergence state
  machine over an emulated engine-tracking contract (mutation-validated),
  9-scenario deterministic fault matrix, real-local-Cognee integration tier
  with deterministic LLM/embedding substitutes, opt-in real-LLM tier,
  PostgreSQL lock tier.
- Six-category benchmark harness with environment-fingerprinted JSON/MD
  reports (fake and real modes).
- Runnable examples: folder → knowledge graph quickstart (one-shot and
  live watch) and a shared-entity provenance demo; both run without
  credentials in deterministic mode.
- Full-repository upstream audit ledger with a machine-checked coverage
  gate, plus four upstream improvement proposals.
