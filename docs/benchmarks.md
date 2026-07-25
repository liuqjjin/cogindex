# Benchmarks

Every number here was produced by `python -m benchmarks.run` on the machine
described at the bottom, and every one of them is reproducible with the command
printed above it. None of them should be compared against a number from a
different machine.

## Read the extraction counts, not the seconds

The benchmarks substitute a deterministic stub for the LLM, because a real
provider makes results unrepeatable and expensive. That choice has a
consequence worth stating plainly: **wall-clock here measures database
overhead, not the thing that dominates a production deployment.** A real
extraction call is seconds of latency and a line on an invoice. A stubbed one
returns instantly.

So the numbers that transfer to your deployment are the call counts. The
seconds are useful for comparing one commit of cogindex against another on the
same machine, and for very little else.

## What a hand-rolled integration does instead

This is the comparison that motivates the project. Both columns ingest the same
six documents, edit two, then delete one, against the same real local Cognee
stack with the same LLM stub. The left column is the loop Cognee's own
quickstart suggests:

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

```bash
python -m benchmarks.run --profile smoke --mode real --categories baseline_comparison
```

| after three syncs | hand-rolled | cogindex | correct |
|---|---|---|---|
| documents in Cognee | 9 | **5** | 5 |
| stale entities in the graph | 4 | **0** | 0 |
| documents removed on delete | 0 | **1** | 1 |
| seconds | 31.9 | 20.4 | |

The hand-rolled integration is not broken code, and on a corpus that never
changes it produces exactly the right graph. It diverges on the second sync,
because `add()` without an explicit `data_id` derives the id from a hash of the
content. An edited document is therefore a *different* document: the new text
lands, the old row stays, and both versions' entities sit in the graph with
equal standing. Retrieval cannot tell them apart. Deleting a source file does
nothing at all, because nothing ever recorded which row it became.

Four superseded rows and four stale entities is what that costs after two
edits. The ratio does not improve with time.

It is also *slower*, which surprises people: re-extracting superseded content
is work, and cogindex skips it.

## Work scales with the change set

Twenty-four documents on the real stack, changing six of them.

```bash
python -m benchmarks.run --profile default --mode real --categories incremental_update
```

| | extraction calls | seconds |
|---|---|---|
| first sync of 24 documents | 49 | 9.22 |
| re-run, nothing changed | **0** | **0.02** |
| change 6 of 24 | **12** | 7.92 |

Changing a quarter of the corpus costs a quarter of the extraction work. A
re-run with nothing changed costs nothing at all, which is the floor an
integration without stable identity can never reach: it has no way to know that
a document it is about to ingest is one it already has.

The seconds column is the honest one to be unimpressed by. 7.92s to change six
documents against 9.22s for a full rebuild is a ratio of 0.86, and the reason
it is not far lower is a fixed cost of roughly 2.8s per batch, described below.

## The fixed cost per batch, and where it went

Cognee scopes its graph engine per dataset and shuts the graph worker down when
that scope closes, blocking on a thread join. Profiling a single
`forget(memory_only=True)` put 1.149s of 1.183s in that teardown, against about
0.07s of actual deletion work.

Until [5e39090](../CHANGELOG.md) cogindex paid that per document, which made
replacing two documents cost more than ingesting six from scratch, with an
incremental-to-full ratio of 2.202. Holding one dataset context open across the
batch collapses N teardowns into one:

| purging N documents | before | after |
|---|---|---|
| N = 1 | 2.7s | 2.8s |
| N = 5 | 13.6s | 3.1s |

Purge cost is now effectively flat in the size of the change set. The remaining
constant is one teardown per batch, which no public Cognee API avoids, and it
stops mattering as the corpus grows: the real-stack ratio is 1.566 at six
documents and 0.859 at twenty-four.

Running the per-document deletions concurrently is 5.5x faster and **wrong**:
it leaves an orphaned type node behind, because the provenance planner's
shared-node cleanup races. That was measured, not assumed, and the loop is
deliberately sequential.

## Connector overhead in isolation

The `fake` mode replaces Cognee with an in-memory emulator, so these numbers
measure cogindex plus the CocoIndex engine and nothing else. They are the ones
to watch for a performance regression in the connector itself.

```bash
python -m benchmarks.run --profile default   # 500 documents, fake mode
```

| category | result |
|---|---|
| initial ingest, 500 documents | 0.037s, one add batch, one cognify |
| change 25 of 500 | 25 documents written, **0 wasted writes** |
| single-document change, end to end | p50 20.9ms, p95 21.8ms |
| delete 25 of 500 | 0.021s, 475 remaining |
| crash mid-batch, then recover | converged; 50 writes for a 25-document change set |
| verification read, 500 documents | 0.0015s |

`wasted_writes == 0` is the assertion behind "work is proportional to the
change set": 25 documents changed, 25 documents written, none of the other 475
touched. The crash-recovery row shows the cost of at-least-once delivery: the
crashed attempt's 25 writes plus the retry's 25, converging to the right state.

## Environment

| | |
|---|---|
| platform | macOS 26.5.2, arm64 |
| CPU count | 15 |
| Python | 3.12.13 |
| cocoindex | 1.0.18 |
| cognee | 1.4.0 |
| commit | 5e39090 |

Reports land in `benchmarks/reports/` as JSON and Markdown, each carrying this
fingerprint. They are gitignored on purpose: a checked-in benchmark result is a
number that silently becomes a lie.
