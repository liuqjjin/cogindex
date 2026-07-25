# cogindex

[![CI](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cogindex)](https://pypi.org/project/cogindex/)
[![Python](https://img.shields.io/pypi/pyversions/cogindex)](https://pypi.org/project/cogindex/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Keep a [Cognee](https://github.com/topoteretes/cognee) knowledge graph in sync
with a changing set of documents, using
[CocoIndex](https://github.com/cocoindex-io/cocoindex) to decide what changed.

## The problem

Wiring Cognee into a pipeline looks like four lines, and they work:

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

They keep working right up until a document changes. `add()` derives a
document's id from a hash of its content, so an edited document is a *new*
document: the new text lands, the old row stays, and both versions' entities
sit in the graph with equal standing. Retrieval cannot tell them apart. Delete
a source file and nothing happens at all, because nothing ever recorded which
row it became.

Here is what that costs, measured on a real local Cognee stack over six
documents with two edits and one deletion
([how to reproduce](docs/benchmarks.md)):

| after three syncs | the four lines above | cogindex | correct |
|---|---|---|---|
| documents in Cognee | 9 | **5** | 5 |
| stale entities in the graph | 4 | **0** | 0 |
| documents removed on delete | 0 | **1** | 1 |
| wall clock | 31.9s | 20.4s | |

cogindex is also the faster of the two, because re-extracting superseded
content is work that it skips.

## Usage

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

You declare what should exist. Run it again with changed content and only the
changed documents are purged, re-added and re-cognified. Drop a key and the
document goes, along with the graph derivatives nothing else supports; entities
still cited by another document survive, because deletion runs through Cognee's
provenance planner rather than around it.

Try it on a folder, with no API key:

```bash
pip install cogindex
python examples/quickstart_live.py ./my-docs --deterministic
```

See [`examples/`](examples/) for that quickstart and a shared-entity provenance
demo.

## Work scales with the change set

Twenty-four documents on the real stack, changing six:

| | extraction calls | seconds |
|---|---|---|
| first sync | 49 | 9.22 |
| re-run, nothing changed | **0** | **0.02** |
| change 6 of 24 | **12** | 7.92 |

Extraction calls are the number that transfers to your deployment; the
benchmarks stub the LLM, so their wall clock measures database overhead rather
than the thing that dominates a real run. Full numbers, the machine they came
from, and a warning about which column not to trust:
[docs/benchmarks.md](docs/benchmarks.md).

## How it works

Six problems, each with a decision record:

| Problem | Mechanism | ADR |
|---|---|---|
| **Stable identity** | `data_id = uuid5(namespace, runtime ⧺ tenant ⧺ dataset ⧺ key)`, over logical coordinates only, never content, injectively encoded | [0002](docs/adr/0002-stable-document-identity.md) |
| **Idempotent writes** | every operation is a safe ensure: re-add converges, deleting the missing succeeds | [0003](docs/adr/0003-consistency-model.md) |
| **Content replacement** | purge derivatives, re-add the same `data_id`, cognify. Without the purge Cognee keeps the old content's graph and vectors | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Config invalidation** | a processing fingerprint per document, plus lossy invalidation at the dataset level. Cognee's own incremental gate never looks at configuration | [0005](docs/adr/0005-configuration-invalidation.md) |
| **Deletion and ownership** | `managed_by="system"` datasets tear down on unmount; deletion always flows through Cognee's provenance planner | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Convergence after failure** | CocoIndex's precommit/commit tracking, reconciled conservatively over *possible* previous states | [0003](docs/adr/0003-consistency-model.md) |

The interesting one is the last. cogindex is a CocoIndex target connector
rather than a memoized function, because memoization knows whether it already
ran but nothing about the external state it produced: it cannot delete, cannot
replace, and cannot tell a crashed write from a completed one
([ADR-0001](docs/adr/0001-cocoindex-target-not-memoized-function.md)).

## What is guaranteed, and what is not

**Guaranteed, and tested:** at-least-once application of idempotent operations
with eventual convergence. After a crash at any phase of the write protocol,
the next successful sync leaves Cognee holding exactly the declared documents
with fresh derivatives, and reconciliation reaches a fixed point.

**Not guaranteed:** cross-system atomicity. CocoIndex's tracking store and
Cognee's three databases cannot be updated in one transaction, so between a
crash and the next sync a reader can observe partially applied state.
[ADR-0003](docs/adr/0003-consistency-model.md) enumerates every anomaly window
rather than pretending they do not exist.

Known limits, upstream-constrained:

- Cognee must run in-process. There is no REST-backed runtime, because
  Cognee's REST `add` accepts no `data_id`
  ([proposal](docs/upstream-proposals/0002-cognee-rest-add-data-id.md)).
- Emptying a dataset on unmount leaves the empty dataset row behind; upstream
  has no public delete for it.
- `managed_by="user"` means cogindex never destroys anything in that dataset,
  not that it removes only what it added.
- `verify_dataset` compares presence, identity, cognify completion and label.
  Not raw content, and not whether derivatives match current content.

## How correctness is established

| Tier | What runs | Command |
|---|---|---|
| unit | reconcile decision matrix, identity goldens, an 11-scenario fault matrix, lock serialization, the upstream compatibility surface | `make test` |
| property | a Hypothesis state machine over random interleavings of declare, remove, config change, sync and crash. Mutation-validated: no-op the derivative purge and it fails | `make test-property` |
| integration | **real local Cognee** (SQLite, LanceDB, embedded graph) with deterministic LLM and embedding substitutes. Replace protocol and shared-entity provenance asserted at the graph level | `make test-integration` |
| postgres | advisory-lock semantics including crash release, against a real PostgreSQL | `make test-postgres` |
| llm | opt-in, real provider end to end | `make test-llm` |

Coverage across the tiers that need no external service is 89%, which is what
the `coverage` job in CI reports. The two modules well below it,
`_locks_postgres` and `_doctor`, are covered by the postgres tier and by
inspection respectively.

Fake-runtime tests are never presented as integration tests. The in-memory fake
deliberately reproduces Cognee's hazards, including orphaned derivatives on
re-add and a completion-only cognify gate, so the tests fail if the protocol
stops compensating for them.

The integration tier has already earned its runtime twice. It found that
`cognee.add()`'s per-item skip gate silently swallows replacement content under
its own defaults, which the source audit had missed
([the corrected finding](docs/upstream-audit/cognee/findings.md)), and it is
where the per-document graph-worker teardown that made incremental updates
slower than full rebuilds showed up as a number.

## Operations

```python
report = await cogindex.verify_dataset(runtime, COGNEE, "docs", expected)
print(report.render())  # missing / unexpected / incomplete / label drift

print(cogindex.doctor().render())  # versions, capabilities, storage roots, credentials
```

`verify_dataset` only detects; re-running the flow is the repair, which
ADR-0003's convergence property is what makes safe.

Batch application per dataset is serialized by a `LockProvider`: in-process by
default, PostgreSQL advisory locks (`cogindex[postgres]`) for multi-process
updaters. Correctness never depends on the lock, which the property suite
demonstrates by still passing without it; the lock exists to avoid duplicated
work ([ADR-0006](docs/adr/0006-concurrency-and-locking.md)).

## Compatibility

Python 3.11 to 3.13, on Linux and macOS. `cocoindex >=1.0.18,<2`,
`cognee >=1.4.0,<1.5`.

Both upstreams were audited at pinned commits recorded in
[`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json). Every first-party upstream file
carries an explicit review status in the [audit ledger](docs/upstream-audit/),
machine-checked in CI, and the four gaps that audit turned up are written up in
[docs/upstream-proposals/](docs/upstream-proposals/).

## Layout

```
src/cogindex/          the connector; public API re-exported in __init__
docs/adr/              seven decision records, start with 0003 and 0004
docs/upstream-audit/   full-repository audit ledger for both upstreams
docs/benchmarks.md     results, machine, reproduction commands
tests/{unit,property,integration}
benchmarks/            seven-category harness
examples/              runnable demos, no credentials needed
```

Development: `make setup && make ci`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. Not affiliated with the CocoIndex or Cognee projects; both are used
under their Apache-2.0 licenses ([ATTRIBUTION.md](ATTRIBUTION.md)).
