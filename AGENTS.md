# AGENTS.md

Guidance for coding agents working in this repository. There is no
`CLAUDE.md` in this repo (or in `~/.claude/`); this file is the single
agent-facing contract. Human-facing docs are `README.md`, `CONTRIBUTING.md`
and `docs/adr/`.

## What this project is

`cogindex` materializes CocoIndex-managed documents into Cognee knowledge
graphs as a **CocoIndex custom target connector**. The hard problems are
identity, idempotency, replacement, config invalidation, deletion, and
convergence after failure — not API wiring. Read `docs/adr/` before touching
`src/`; ADR-0003 (consistency model) and ADR-0004 (replace/delete protocol)
are the load-bearing documents.

Status: pre-release `0.1.0`, 15 local commits on `main`, no git remote, never
published. Milestones M0–M13 are committed; the final adversarial-review
milestone was interrupted mid-flight and its findings are still open (see
"Known open defects").

## Environment and commands

Requires Python 3.11–3.14 (`.python-version` pins 3.12) and
[uv](https://docs.astral.sh/uv/). A prepared `.venv` already exists; add
`--no-sync` to `uv run` to use it without re-resolving.

```bash
make setup             # uv sync --all-extras
make lint              # ruff check . && ruff format --check .
make typecheck         # mypy --strict over src + tests
make test              # tests/unit          — no services, no network, no LLM
make test-property     # tests/property      — Hypothesis state machine
make test-integration  # tests/integration   — REAL local Cognee, deterministic LLM/embeddings
make test-postgres     # needs POSTGRES_DSN or Docker (testcontainers)
make test-llm          # opt-in real LLM: COGINDEX_RUN_LLM_TESTS=1 + LLM_API_KEY
make ci                # lint typecheck test test-property (exactly the required CI job)
make smoke             # build wheel, install and import in a clean venv
make benchmark-smoke   # tiny benchmark run to validate the harness
make upstream-lock     # regenerate audit inventories from .upstream/ clones
```

Observed on 2026-07-25 (macOS arm64, Python 3.12.13, cognee 1.4.x,
cocoindex 1.0.18): ruff clean (64 files), `mypy --strict` clean (34 files),
`tests/unit` **122 passed** in ~4 s (10 warnings, all Pydantic deprecations
from inside cognee), `tests/property` 1 passed in ~0.4 s, `tests/integration
-m integration` **3 passed, 5 deselected** in ~58 s. `tests/unit` collects
122 — the README's "123 tests" is stale.

Not run here (record the reason if you skip them too): `make test-postgres`
(needs Docker/PostgreSQL), `make test-llm` (needs a paid key),
`make smoke` / `make build` (mutate `dist/`), benchmarks (machine-specific).

Extra gate that exists but is **not wired into CI, the Makefile, or
pre-commit** — run it manually when touching the audit ledger:

```bash
uv run python docs/upstream-audit/tools/check_coverage.py   # 303/303 and 1650/1650 as of 2026-07-25
```

## Layout

- `src/cogindex/` — the package; modules are `_`-private, public API is
  re-exported in `__init__.py` (`__all__` is the API contract).
- `tests/unit`, `tests/property`, `tests/integration` — tiered as in CI;
  markers: `property`, `integration`, `integration_llm`, `postgres`.
  `tests/common/` holds the shared CocoIndex `Environment` helper and
  `engine_model.py`, an emulation of the engine's tracking semantics.
- `benchmarks/` — six-category harness (`python -m benchmarks.run`); reports
  land in the gitignored `benchmarks/reports/`.
- `examples/` — runnable demos; both work with no credentials in
  deterministic mode.
- `docs/adr/` — seven decision records. `docs/upstream-audit/` — audit ledger
  for the pinned upstream commits (`UPSTREAM_LOCK.json`).
  `docs/upstream-proposals/` — four upstream gaps written up as proposals.
- `.upstream/` — gitignored clones of cocoindex/cognee at the pinned commits.
  Read them when in doubt; upstream *tests* are the authoritative statement
  of upstream semantics.

## Architecture and data flow

Two-level target, registered once at import time as
`"cogindex/cognee/dataset"`:

1. `DatasetHandler.reconcile()` (root/container) diffs the dataset's
   `ProcessingConfig` fingerprint and `managed_by` ownership. It **always**
   returns an output, even when converged, because the sink must run to hand
   the engine a child handler. A processing-config `replace` also sets
   `child_invalidation="lossy"`, which makes the engine pass
   `prev_may_be_missing=True` to every document.
2. `_apply_dataset_actions` (container sink) resolves the `CogneeRuntime`
   from the `ContextProvider` by key string, resolves the dataset, and
   returns a `ChildTargetDef(DocumentHandler(...))` with the runtime, handle,
   profile and processing fingerprint already bound.
3. `DocumentHandler.reconcile()` derives `data_id` from logical coordinates,
   collects `stale_data_ids` (previously recorded ids that no longer match),
   runs `statediff.diff()`, then `_classify_write()` maps the diff onto one
   of `upsert | replace | update_metadata | delete`.
4. `DocumentHandler._apply()` (document sink, one batch per dataset because
   the sink is a bound method) groups the batch and executes, under
   `runtime.dataset_lock(handle)`, in this fixed order: hard deletes →
   derivative purges → one batched `add_documents` → one `cognify_dataset`.
   `delete_ids -= payloads.keys()` guarantees an identity written in this
   batch is never deleted in it.

`CogneeRuntime` is the only seam to the outside: `LocalCogneeRuntime` (the
supported implementation, driving the in-process cognee library) and
`FakeCogneeRuntime` in `cogindex.testing` (in-memory, fault-injecting,
deliberately reproducing upstream's hazards). Every version-sensitive cognee
import goes through `_compat.py`, lazily, behind a capability check.

Side channels: `verify_dataset()` (read-only drift detection — re-running the
flow is the repair) and `doctor()` (read-only environment checks).

## Load-bearing invariants

1. `reconcile()` implementations are synchronous and perform **no I/O** — the
   engine calls them while holding a tokio mutex over the declared-states map.
   All external calls go in action sinks.
2. Target keys and tracking records never contain credentials, URLs,
   connections, or raw document content. External resources are referenced by
   `ContextKey` strings and resolved at sink time.
3. Document identity is `uuid5` over logical coordinates (ADR-0002) — never
   content hashes. The `COGINDEX_NAMESPACE` literal, `IDENTITY_SCHEMA_VERSION`
   and the golden `data_id`s in `tests/unit/test_identity.py` are frozen
   contracts: changing any of them renames every managed document.
4. Every sink action must be idempotent and safe under `prev_may_be_missing`
   and multiple `prev_possible_records`. Deleting or purging something absent
   is success.
5. `prev_may_be_missing=True` over a **non-empty** `prev_possible_records`
   classifies as `replace`, never `upsert`: upstream's hard delete drops
   derivatives before the row and its COMPLETED status, so the create path
   would commit a tracking record over a document with no derivatives and
   never revisit it (ADR-0004's second amendment). Empty `prev_records` keeps
   the create path on purpose — purging every fresh document costs ~1.17 s
   per document on a real stack.
6. Fingerprint taxonomy decides the write op, so keep the three apart:
   `content_fingerprint` / `annotations_fingerprint` (node_set,
   importance_weight) / `processing_fingerprint` are derivative-affecting →
   `replace`; `metadata_fingerprint` (label, external_metadata) is benign →
   `update_metadata` (re-add, no purge, no cognify).
7. All Cognee imports that are version-sensitive (e.g. `DataItem`) go through
   `src/cogindex/_compat.py` only. No monkey-patching upstream.
8. Never commit a tracking record for a write that was not attempted, and
   never treat a cognee result payload containing `PipelineRunErrored` as
   success (`_raise_on_errored_runs`).
9. Structured logs carry phase, counts and timing only — never content,
   never secrets (pinned by `test_apply_logs_never_contain_document_content`).

## Test gates

- Required gate is `make ci` = lint + typecheck + `tests/unit` +
  `tests/property`. CI additionally runs the deterministic integration tier,
  the PostgreSQL lock tier, a packaging smoke test, gitleaks, and pip-audit
  (advisory only). The core matrix is ubuntu/macos × 3.11/3.12/3.13 — **3.14
  is claimed in metadata but never exercised.**
- Do not weaken a test to make it pass; the 9-scenario fault matrix
  (`tests/unit/test_fault_matrix.py`) and the Hypothesis machine
  (`tests/property/test_convergence_machine.py`) encode the convergence
  contract, not merely error paths.
- Fake-runtime tests must never be presented as real integration tests. The
  deterministic-LLM integration tier must never be presented as real E2E.
- New behavior needs a test at the right tier; a change to the fault model
  needs a fault-injection case.
- Verify mutation claims before repeating them: disabling the purge phase
  does make the property tier fail; disabling the dataset lock does **not**
  (only `tests/unit/test_fault_matrix.py::test_concurrent_batches_serialize_
  under_dataset_lock` catches that).

## Coding standards

- Ruff (line length 100, target py311) with `E,F,W,I,UP,B,C4,SIM,RUF,ASYNC,
  S,T20`; `E501` and `S101` are the only global ignores. `T20` means no stray
  `print` outside examples/benchmarks/tests/audit tools.
- `mypy --strict` over `src` and `tests`, `python_version = 3.12`, with
  `warn_unreachable` and `ignore-without-code, redundant-expr, truthy-bool`.
  Untyped cognee/asyncpg/testcontainers imports are allowed only via the
  existing overrides.
- `from __future__ import annotations` at the top of every module; explicit
  `__all__`; `__slots__` on hot handler classes; frozen dataclasses for specs
  and `msgspec.Struct(frozen=True)` for tracking records.
- Comments explain *why* (upstream hazards, deliberate deviations), never
  what the next line does. Long-form rationale belongs in an ADR.
- Conventional-commit subjects (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Prohibited

- **Never push to a remote, and never create commits unless explicitly
  asked.** There is no remote configured; keep it that way.
- Never vendor or copy upstream source into this repo, and never
  monkey-patch upstream at runtime. If an upstream gap blocks you, write
  `docs/upstream-proposals/NNNN-*.md`.
- Never put secrets, API keys, DSNs or `.env` content in code, tests,
  fixtures, logs, tracking records or target keys.
- Never claim a test, benchmark or command passed without running it, and
  never restate a doc claim as verified without checking it against code.
- Never quote machine-specific benchmark numbers in the README or ADRs.
- Never do I/O inside `reconcile()`; never derive identity from content.
- Never "simplify away" `add(..., incremental_loading=False,
  data_cache=False)` (see below).
- Do not edit files under `.upstream/`, `.venv/`, `dist/`, or the generated
  `docs/upstream-audit/*/inventory.jsonl` by hand (regenerate instead).

## Upstream facts agents commonly get wrong

- Re-adding changed content to Cognee under the same `data_id` does **not**
  remove old graph/vector derivatives — `forget(memory_only=True)` first.
- With `add()`'s defaults (`incremental_loading=True`, `data_cache=True`),
  an already-added `data_id` is **skipped entirely** — replacement content
  silently never lands, because `forget(memory_only=True)` resets only
  `cognify_pipeline`, never `add_pipeline`. cogindex always passes both as
  False on add; the integration replace tests catch a regression here.
- Cognee's incremental cognify gate compares one per-item status
  (`pipeline_status["cognify_pipeline"][dataset_id] ==
  "DATA_ITEM_PROCESSING_COMPLETED"`) and has **no config fingerprint**;
  config invalidation is cogindex's job.
- Cognee's hard delete (`datasets.delete_data`) removes graph/vector
  derivatives **first** and the relational row holding `pipeline_status`
  **last**, so a torn delete can leave derivatives gone with the status still
  COMPLETED. This is the premise of the open critical defect below.
- Cognee's dataset lock is process-local asyncio only, with age-based stale-run
  recovery (default 3600 s) as its only multi-process safety net.
- Cognee's default storage paths land inside its own installed package;
  always pass `data_root=` / `system_root=` (and absolutize them).
- The CocoIndex handler attachment method is `attachments() -> dict` (plural);
  the upstream SKILL.md showing `attachment(att_type)` is stale.
- CocoIndex forces `prev_may_be_missing=True` for fresh keys, delete markers,
  pending states, schema-version mismatch and full reprocess; two live
  records with no delete marker keep it **False** — the handler's own record
  comparison must decide.

## Known limitations (upstream-constrained, by design)

- Emptying a dataset on unmount leaves the (empty) dataset row: upstream has
  no public dataset-row delete API.
- `managed_by="user"` means "cogindex never destroys anything here", not
  "cogindex removes only what it added": on unmount the engine drops child
  tracking without issuing per-document deletes.
- No remote/REST write path exists: cognee's REST `add` accepts no `data_id`.
- `verify_dataset` compares presence, identity, cognify completion and label
  — not raw content or `external_metadata`.
- No cross-system atomicity; ADR-0003 enumerates the anomaly windows.

## Known open defects (as of 2026-07-25 — do not trust the docs they contradict)

A six-lens adversarial review ran on 2026-07-24 and produced 19 findings; its
per-finding refutation stage was cut short by a session limit, so these are
**unrefuted, not adversarially confirmed**. Three of its six lenses (DoD
audit, API/packaging, runtime failure model) never produced a result at all,
so that surface is still un-reviewed.

**Fixed** — `src/cogindex/_target.py` `_classify_write` no longer sends
uncertain state over a recorded document down the no-purge create path
(ADR-0004's second amendment, pinned by fault-matrix scenario 10). The
residual `prev=[]`-after-tracking-loss gap is documented there with its
O(1) recovery, not silently closed.

- **CRITICAL `examples/quickstart_live.py:124-128` vs `:152`** — with a
  relative folder argument the declared keys keep the folder prefix while the
  expectations strip it, so `verify_dataset` reports every document as both
  missing and unexpected. Works only with an absolute path.
- **CRITICAL `docs/adr/0007-runtime-abstraction.md:38-45`,
  `docs/upstream-proposals/0002`, `README.md:68`** — they describe
  `RemoteCogneeRuntime` as existing; no such class exists anywhere, and
  `UnsupportedCapabilityError` is exported but never raised.
- **CRITICAL `README.md:82`** — claims the property tier's mutation
  validation covers removing the lock. It does not (verified twice).
- **CRITICAL `docs/adr/0006-concurrency-and-locking.md:63-64`** — cites a
  "two-worker fault-injection test ... with and without the cross-process
  lock"; no such test exists.
- **MAJOR** `docs/adr/0002:60-61` claims path-separator canonicalization is
  implemented and unit-tested — `normalize_external_key` does NFC only.
  `docs/adr/0002:15-16` still states the pre-CORRECTION "re-add is an upsert"
  fact. `docs/adr/0005:22` claims embedding *dimensions* are in the
  processing fingerprint — they are not, so a dimensions-only change
  invalidates nothing. `docs/adr/0005:53` claims an "unrelated config →
  zero re-processing" test that does not exist. `README.md:122`'s "hard
  `wasted_writes == 0` check" is an unchecked fake-mode-only metric.
  `CONTRIBUTING.md:48` and the nightly workflow point at a nonexistent
  `tests/unit/test_compat.py`. `README.md:81` says 123 unit tests; 122 are
  collected.
- **MAJOR `tests/common/engine_model.py:71`** — forces
  `prev_may_be_missing=True` whenever a pending record is retained, so no
  fault-matrix or property test can produce the audited "two live records,
  missing=False" transition; the metadata-preserving retry path is untested
  repo-wide, and `sync_expect_crash`'s docstring overstates fidelity.
- **MINOR** `_spec.py:103` (`chunk_size=None` is not resolved to cognee's
  effective default, contradicting `CognifyProfile`'s docstring);
  `docs/adr/0002:43` misassigns node_set/importance_weight to the benign
  metadata fingerprint; `tests/unit/test_fault_matrix.py:3` claims every
  scenario asserts exact mid-crash state (scenarios 6–9 do not);
  `update_metadata` is never exercised against real Cognee (all e2e runs use
  constant labels); Python 3.14 is claimed but never CI-tested;
  `docs/upstream-proposals/0003:42` cites the internal milestone label "M11".
- Also noted while auditing: `docs/upstream-audit/cocoindex/findings.md:130`
  claims "a unit test that runs reconcile with all I/O seams stubbed to
  raise" — no such test exists.

When you fix any of the above, fix the doc and the test in the same change,
and delete the entry from this list.
