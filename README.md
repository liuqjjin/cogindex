# cogindex

Reliable, incremental materialization of
[CocoIndex](https://github.com/cocoindex-io/cocoindex)-managed documents into
[Cognee](https://github.com/topoteretes/cognee) knowledge graphs.

You declare *what* documents should exist in a Cognee dataset; the CocoIndex
engine detects what changed; cogindex turns the difference into idempotent
Cognee operations — batched adds under stable identities, in-place
replacement, provenance-respecting deletion, one incremental cognify per
changed batch — and converges to the declared state even across crashes.

```python
import cocoindex as coco
import cogindex

COGNEE = coco.ContextKey[cogindex.CogneeRuntime]("cognee")


@coco.fn
async def app_main(docs: dict[str, str]) -> None:
    target = await coco.use_mount(cogindex.declare_dataset_target, COGNEE, "docs")
    for key, content in docs.items():
        target.declare_document(key, content)
```

Run it again with changed content: only the changed documents are purged,
re-added and re-cognified. Remove a key: the document and its exclusive
graph derivatives are deleted, while entities still supported by other
documents survive (Cognee's provenance planner decides; cogindex feeds it
correct identities). See [`examples/`](examples/) for a runnable folder →
knowledge-graph quickstart and a shared-entity provenance demo — both work
without any LLM key in deterministic mode.

## The problems this connector actually solves

Wiring `cognee.add()` into a pipeline is easy. Making the result *converge*
is not — these are the six problems cogindex exists for, each with a
decision record:

| Problem | Mechanism | ADR |
|---|---|---|
| **Stable identity** | `data_id = uuid5(namespace, runtime ⧺ tenant ⧺ dataset ⧺ key)` — logical coordinates only, never content, injectively encoded | [0002](docs/adr/0002-stable-document-identity.md) |
| **Idempotent writes** | every operation is a safe ensure: re-add converges, deleting the missing succeeds | [0003](docs/adr/0003-consistency-model.md) |
| **Content replacement** | purge derivatives → re-add same `data_id` → cognify; without the purge, Cognee keeps orphaned graph/vector derivatives (audited, and emulated in the test fake) | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Config invalidation** | processing fingerprint per document + lossy child invalidation at the dataset level; Cognee's own incremental gate never checks configuration | [0005](docs/adr/0005-configuration-invalidation.md) |
| **Deletion & ownership** | `managed_by="system"` datasets tear down on unmount; deletion always flows through Cognee's provenance planner | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Convergence after failure** | CocoIndex's precommit/commit tracking + conservative reconciliation over *possible* previous states; any successful sync reaches the declared state | [0003](docs/adr/0003-consistency-model.md) |

## What is honestly guaranteed (and what is not)

**Guaranteed** (and tested): at-least-once application of idempotent
operations with eventual convergence — after any sequence of crashes at any
phase of the write protocol, the next successful sync leaves Cognee holding
exactly the declared documents with fresh derivatives, and reconciliation
reaches a fixed point.

**Not guaranteed**: cross-system atomicity. CocoIndex's tracking store and
Cognee's three databases cannot be updated in one transaction; between a
crash and the next sync, readers can observe stale or partially-applied
state. The consistency model documents every anomaly window instead of
pretending they don't exist: [ADR-0003](docs/adr/0003-consistency-model.md).

Known limitations (upstream-constrained, documented not papered over):

- Emptying a dataset on unmount leaves the (empty) dataset row — Cognee has
  no public dataset-row delete API.
- Cognee must run in-process. There is no REST-backed runtime, because
  Cognee's REST add accepts no `data_id` and stable identity is the
  foundation of everything else here
  ([proposal](docs/upstream-proposals/0002-cognee-rest-add-data-id.md),
  [ADR-0007](docs/adr/0007-runtime-abstraction.md)).
- `verify_dataset` compares presence/identity/completion/label, not raw
  content or metadata (Cognee stores those in storage-specific envelopes).
- Unmounting a `managed_by="user"` dataset leaves *everything* in it,
  including documents cogindex added (engine-verified semantics, see
  ADR-0004).

## How correctness is tested

| Tier | What runs | Command |
|---|---|---|
| unit | reconcile decision matrix, identity goldens, 11-scenario deterministic fault matrix, lock serialization, false-success guards, compatibility surface — no services | `make test` |
| property | Hypothesis state machine: random interleavings of declare/remove/config-change/sync/crash against an emulation of the engine's precommit→apply→commit contract. Mutation-validated: no-op the derivative purge, or misclassify a replace as metadata-only, and it fails. Removing the dataset lock does *not* fail this tier (correctness never depended on the lock); that regression is caught by the unit fault matrix instead | `make test-property` |
| integration | **real local Cognee** (SQLite + LanceDB + embedded graph) with deterministic LLM/embedding substitutes — replace protocol and shared-entity provenance asserted at the graph level; the incremental gate proven by LLM call counts | `make test-integration` |
| integration_llm | opt-in, real LLM end to end (`COGINDEX_RUN_LLM_TESTS=1` + key) | `make test-llm` |
| postgres | advisory-lock semantics incl. crash-release, against a real PostgreSQL (CI service container or Docker) | `make test-postgres` |

Fake-runtime tests are never presented as integration tests; the in-memory
fake deliberately reproduces Cognee's hazards (orphaned derivatives on
re-add, completion-only cognify gate) so tests fail if the protocol stops
compensating for them.

The integration tier has already paid for itself: it caught that
`cognee.add()`'s per-item skip gate (on by default via `data_cache` /
`incremental_loading`) silently swallows replacement content — a behavior
the source audit missed. See
[the corrected finding](docs/upstream-audit/cognee/findings.md).

## Operations

```python
report = await cogindex.verify_dataset(runtime, COGNEE, "docs", expected)
print(report.render())  # missing / unexpected / incomplete / label drift

print(cogindex.doctor().render())  # versions, capabilities, storage roots, credentials
```

Re-running the flow is the repair; `verify_dataset` only detects.

Concurrency: batch application per dataset is serialized by a
`LockProvider` — in-process by default, PostgreSQL advisory locks
(`cogindex[postgres]`) for multi-process updaters. Correctness never
depends on the lock; it prevents wasted duplicate work
([ADR-0006](docs/adr/0006-concurrency-and-locking.md)).

## Benchmarks

```bash
python -m benchmarks.run --profile default          # connector layer (in-memory fake)
python -m benchmarks.run --profile smoke --mode real  # real local stack, deterministic substitutes
```

Categories: baseline comparison against a naive integration, initial ingest,
incremental update, freshness percentiles, deletion correctness, crash
recovery, verification reads. Reports land in the gitignored
`benchmarks/reports/` as JSON and Markdown with a full environment
fingerprint.

See [docs/benchmarks.md](docs/benchmarks.md) for results, the machine they
were measured on, and what each number does and does not mean.

## Install & compatibility

```bash
pip install cogindex            # not yet published; from source: pip install .
```

Python 3.11 to 3.13, tested on Linux and macOS. Pinned upstream ranges:
`cocoindex >=1.0.18,<2`, `cognee >=1.4.0,<1.5`. The audited upstream commits are locked in
[`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json); every first-party upstream file
carries an explicit review status in the
[audit ledger](docs/upstream-audit/) (machine-checked by
`docs/upstream-audit/tools/check_coverage.py`). Four upstream improvement
proposals derived from the audit live in
[docs/upstream-proposals/](docs/upstream-proposals/).

## Project layout

```
src/cogindex/          the connector (public API re-exported in __init__)
docs/adr/              seven decision records — start with 0003 and 0004
docs/upstream-audit/   full-repo audit ledger for both upstreams
docs/upstream-proposals/
tests/{unit,property,integration}
benchmarks/            six-category benchmark harness
examples/              runnable demos (deterministic mode needs no keys)
```

Development: `make setup && make ci` (exactly what required CI runs). See
[CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## License

Apache-2.0. Not affiliated with the CocoIndex or Cognee projects; both are
used under their Apache-2.0 licenses (see [ATTRIBUTION.md](ATTRIBUTION.md)).
