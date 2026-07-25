# cogindex

[![CI](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

中文: [README.md](README.md)

Keep a knowledge graph consistent with a document set that changes.

Edit a document and the graph has to follow. Delete one and the entities only
it supported have to go, while entities another document still cites have to
stay. Crash halfway and the next sync has to converge on its own. None of that
is one API call.

## Where it goes wrong

Loading documents into a knowledge graph looks like four lines, and they work:

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

They stay correct right up until a document changes.

`add()` derives the document id from a hash of the content. Change the content
and the hash changes, so **an edited document becomes a new document**: the new
text lands, the old row stays, and entities extracted from both versions sit in
the graph with equal standing. Retrieval cannot tell which one is current.
Deleting a source file does nothing at all, because nothing ever recorded which
row it became.

This is not misuse. It is what the upstream quickstart shows. The problem is
that **stable identity has to come from somewhere, and if the integration layer
does not supply it, nobody does.**

Measured cost, over six documents with two edits and one deletion, on the same
real local stack. Left column is those four lines
([how to reproduce](docs/benchmarks.md)):

| after three syncs | four hand-written lines | cogindex | correct |
|---|---|---|---|
| documents in the store | 9 | **5** | 5 |
| stale entities in the graph | 4 | **0** | 0 |
| documents removed on delete | 0 | **1** | 1 |
| wall clock | 31.9s | 20.4s | |

cogindex is the faster of the two, because it does not re-extract content that
has already been superseded.

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

You declare what should exist. Run it again and only the documents whose content
changed get their derivatives purged, rewritten and re-extracted. Drop a key and
the document goes along with the graph data only it supported, while entities
another document still cites survive.

Try it on a folder, no API key needed:

```bash
git clone https://github.com/liuqjjin/cogindex && cd cogindex
make setup
python examples/quickstart_live.py ./my-docs --deterministic
```

See [`examples/`](examples/) for that quickstart and a shared-entity provenance
demo.

## Work scales with the change set, not the corpus

Twenty-four documents on the real stack, changing six:

| | extraction calls | seconds |
|---|---|---|
| first sync | 49 | 9.22 |
| re-run, nothing changed | **0** | **0.02** |
| change 6 | **12** | 7.92 |

Read the call counts, not the seconds. The benchmarks swap the model for a
deterministic stub, so seconds measure database overhead; in a real deployment a
single extraction is seconds of latency and a line on an invoice, and that is
what dominates. Change a quarter of the corpus and you pay a quarter of the
extraction cost. Change nothing and you pay none, which an integration without
stable identity can never reach, because it cannot tell that the document in
front of it is one it has already handled.

Full numbers, the machine they came from, and what each one does and does not
mean: [docs/benchmarks.md](docs/benchmarks.md).

## Design

Six problems, each with a decision record:

| Problem | Mechanism | ADR |
|---|---|---|
| **Stable identity** | `data_id = uuid5(namespace, runtime ⧺ tenant ⧺ dataset ⧺ key)`, from logical coordinates only, never content, injectively encoded | [0002](docs/adr/0002-stable-document-identity.md) |
| **Idempotent writes** | every operation is a safe ensure: re-writing converges, deleting the missing succeeds | [0003](docs/adr/0003-consistency-model.md) |
| **Content replacement** | purge derivatives, write back under the same `data_id`, then extract. Without the first step the old content's nodes and vectors stay put | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Config invalidation** | a processing fingerprint per document plus dataset-level propagation. The upstream incremental check looks at completion status and never at configuration | [0005](docs/adr/0005-configuration-invalidation.md) |
| **Deletion and ownership** | `managed_by` decides whether unmounting clears the dataset; deletion always goes through the upstream provenance planner rather than touching the graph directly | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **Convergence after a crash** | conservative reconciliation over *every possible* previous state, using the engine's precommit and commit records | [0003](docs/adr/0003-consistency-model.md) |

The last one is the interesting one. This is a target-state connector rather
than a cached function because a cache knows whether it ran and nothing about
the external state it produced: it cannot delete, cannot replace, and cannot
tell a crashed write from a finished one
([ADR-0001](docs/adr/0001-cocoindex-target-not-memoized-function.md)).

## What is guaranteed, and what is not

**Guaranteed, and tested:** at-least-once delivery of idempotent operations
with eventual convergence. After a crash at any phase of the write protocol,
the next successful sync leaves the graph exactly matching what was declared,
with fresh derivatives, and reconciliation reaches a fixed point.

**Not guaranteed:** cross-system atomicity. The tracking store and the graph's
three databases cannot be updated in one transaction, so between a crash and
the next sync a reader can observe partially applied state.
[ADR-0003](docs/adr/0003-consistency-model.md) lists every anomaly window
rather than pretending they do not exist.

Known limits, all upstream constraints rather than unfinished work:

- The graph has to run in-process. There is no HTTP-backed implementation,
  because the upstream REST endpoint accepts no caller-supplied document id
  ([proposal 0002](docs/upstream-proposals/0002-cognee-rest-add-data-id.md)).
- Unmounting clears a dataset but leaves the empty dataset row; upstream has no
  public delete for it.
- `managed_by="user"` means "never destroy anything in this dataset", not
  "remove only what I added".
- `verify_dataset` compares presence, identity, completion and label. Not raw
  content, and not whether derivatives match current content.

## How correctness is established

| Tier | What runs | Command |
|---|---|---|
| unit | reconcile decision matrix, identity goldens, an 11-scenario fault-injection matrix, lock serialization, the upstream compatibility surface | `make test` |
| property | Hypothesis state machine over random interleavings of declare, remove, config change, sync and crash. Mutation-validated: no-op the derivative purge and it fails | `make test-property` |
| integration | **the real local stack** (SQLite, LanceDB, embedded graph) with deterministic model substitutes. Replace protocol and shared-entity provenance asserted at the graph level | `make test-integration` |
| PostgreSQL | advisory-lock semantics including automatic release when the holding session dies | `make test-postgres` |
| real model | opt-in, end to end against a real provider | `make test-llm` |

Coverage across the tiers that need no external service is 89%, as computed by
the `coverage` job in CI. The two modules well below it, `_locks_postgres` and
`_doctor`, are covered by the PostgreSQL tier and by read-only checks
respectively.

Tests built on the in-memory substitute never pose as integration tests. That
substitute deliberately reproduces the upstream hazards, including derivatives
left behind after a re-write and an incremental check that only looks at
completion, so the tests fail the moment the protocol stops compensating.

The integration tier has paid for its runtime twice. It found that the upstream
`add()` per-item skip gate silently swallows replacement content under its own
defaults, which the source audit had missed
([the corrected finding](docs/upstream-audit/cognee/findings.md)), and it turned
"one graph-worker teardown per document", which made incremental updates slower
than full rebuilds, into a concrete number.

## Operations

```python
report = await cogindex.verify_dataset(runtime, COGNEE, "docs", expected)
print(report.render())  # missing / unexpected / incomplete / label drift

print(cogindex.doctor().render())  # versions, capabilities, storage roots, credentials
```

`verify_dataset` only detects. The repair is to re-run the flow, and what makes
that safe is ADR-0003's convergence property.

Batch writes per dataset are serialized by a `LockProvider`: in-process by
default, PostgreSQL advisory locks (`cogindex[postgres]`) for multi-process
setups. Correctness never depends on the lock, and the property suite still
passing without it is the evidence; the lock exists to avoid duplicated work
([ADR-0006](docs/adr/0006-concurrency-and-locking.md)).

## Install and compatibility

Not published to PyPI. Install from source:

```bash
pip install git+https://github.com/liuqjjin/cogindex
```

Python 3.11 to 3.13, Linux and macOS. Dependency ranges
`cocoindex >=1.0.18,<2` and `cognee >=1.4.0,<1.5`.

Both upstreams were read through and audited at fixed commits recorded in
[`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json). Every first-party upstream source
file carries an explicit review status in the
[audit ledger](docs/upstream-audit/), machine-checked in CI, and the four gaps
that audit turned up are written up as
[proposals](docs/upstream-proposals/).

## Layout

```
src/cogindex/          the connector; public API re-exported in __init__
docs/adr/              seven decision records, start with 0003 and 0004
docs/upstream-audit/   read-through audit ledger for both upstreams
docs/benchmarks.md     results, the machine, reproduction commands
tests/{unit,property,integration}
benchmarks/            seven benchmark categories
examples/              runnable demos, no credentials needed
```

Development: `make setup && make ci`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

This project stands on two open-source projects, each of which solved the hard
problems in its own domain:

- [CocoIndex](https://github.com/cocoindex-io/cocoindex), an incremental data
  processing engine, which provides target-state declaration, change detection
  and post-crash tracking semantics.
- [Cognee](https://github.com/topoteretes/cognee), a knowledge graph memory
  layer, which handles ingestion, extraction, provenance and retrieval.

cogindex neither modifies nor copies any of their code. It drives them through
public interfaces and supplies the consistency protocol in between, which
neither of them covers. See [ATTRIBUTION.md](ATTRIBUTION.md) for the dependency
and version details.

## License

Apache-2.0. Not affiliated with either project; both are used under their
respective Apache-2.0 licenses.
