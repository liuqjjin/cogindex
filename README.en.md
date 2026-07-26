# cogindex

[![CI](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

中文说明：[README.md](README.md)

cogindex synchronizes a changing document set into a Cognee knowledge graph.
It is a CocoIndex custom target that supplies stable document ids, incremental
replacement, deletion, retry handling, and reprocessing after configuration
changes.

CocoIndex provides target-state tracking and change detection. Cognee handles
ingestion, extraction, graph storage, and retrieval. cogindex manages the
synchronization rules between them.

> The current `0.1.x` series is undergoing pre-release hardening. Do not use it
> for datasets that cannot be rebuilt before `0.2.0`.

## Why a synchronization layer is needed

A direct Cognee integration can perform an initial import:

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

This code does not retain the relation between a source key and a Cognee
document. When content changes, Cognee's default content-derived id changes.
When a source disappears, the integration has no stored id to delete.

cogindex derives a UUID5 from a stable source key such as a relative path,
database primary key, or object-store key:

```text
data_id = uuid5(fixed namespace, runtime_key + tenant + dataset + document_key)
```

Content is not part of the identity. The same source key can therefore be
updated in place and deleted later.

See [ADR-0002](docs/adr/0002-stable-document-identity.md) for the frozen
identity format.

## Quick start

```bash
git clone https://github.com/liuqjjin/cogindex.git
cd cogindex
uv sync --all-extras
uv run python examples/quickstart_live.py ./my-docs --deterministic
```

Edit, add, or remove Markdown and text files, then run the command again.
Continuous watching is also available:

```bash
uv run python examples/quickstart_live.py ./my-docs --deterministic --live
```

Deterministic mode tests synchronization without an API key. It does not
represent the extraction quality of a real model.

## Use from a CocoIndex flow

```python
import cocoindex as coco
import cogindex

COGNEE = coco.ContextKey[cogindex.CogneeRuntime]("cognee")


@coco.fn
async def app_main(documents: dict[str, str]) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        COGNEE,
        "docs",
    )
    for document_key, content in documents.items():
        target.declare_document(document_key, content)
```

The ContextKey string participates in document identity and is persisted in
CocoIndex's tracking store. Use a non-secret logical name such as `cognee`,
never a DSN, URL, API key, or credential. Public entry points do not accept a
plain string in place of a ContextKey. The key must contain 1–128 ASCII
letters, digits, dots, underscores, or hyphens, and start with a letter or
digit.

Provide a runtime through the same environment:

```python
from pathlib import Path

runtime = cogindex.LocalCogneeRuntime(
    data_root=Path("./data/cognee"),
    system_root=Path("./data/cognee-system"),
)
environment = coco.Environment(
    coco.Settings.from_env(db_path="./data/cocoindex-tracking"),
)
environment.context_provider.provide(COGNEE, runtime)
```

The complete example is
[examples/quickstart_live.py](examples/quickstart_live.py).

## Write order

`reconcile()` compares desired state with tracking records and performs no
external I/O. The sink applies one dataset batch under its dataset lock:

```text
hard deletes
    ↓
purge derivatives for replacements
    ↓
batched add
    ↓
one cognify call
    ↓
commit CocoIndex tracking records
```

Important rules:

- identity depends on logical source coordinates, never content;
- old derivatives are purged before replacement content is written;
- Cognee's add-side incremental skip and data cache are disabled for writes;
- missing data is safe to delete, but permission and write errors propagate;
- tracking is committed only for writes that were attempted successfully.

Design details:

- [ADR-0003: consistency model](docs/adr/0003-consistency-model.md)
- [ADR-0004: replacement and deletion](docs/adr/0004-replace-delete-protocol.md)
- [ADR-0005: configuration invalidation](docs/adr/0005-configuration-invalidation.md)
- [ADR-0006: dataset locking](docs/adr/0006-concurrency-and-locking.md)

## Consistency boundary

cogindex does not provide a transaction across the CocoIndex tracking store
and Cognee's relational, graph, and vector databases. A process failure can
leave a partially applied external state.

Recovery relies on idempotent writes and CocoIndex's possible tracking records.
The automatic recovery model covers uncertainty created by the synchronization
flow itself. Direct external changes to Cognee are different: `verify_dataset`
can detect part of that drift, but an ordinary rerun may not repair it.

## Verification

```python
report = await cogindex.verify_dataset(
    runtime,
    COGNEE,
    "docs",
    expected_documents,
)
print(report.render())
print(cogindex.doctor().render())
```

`verify_dataset` currently checks missing documents, unexpected documents,
incomplete cognify status, and label differences. It does not prove that graph
or vector derivatives were built from the current content.

## Tests

```bash
make test
make test-property
make test-integration
make test-postgres
make test-llm
make ci
```

CI covers Linux and macOS on Python 3.11, 3.12, and 3.13, strict mypy, Ruff,
286 unit tests, a 60-example × 40-step Hypothesis state machine, a local
Cognee instance with deterministic model substitutes, PostgreSQL advisory
locks, and clean wheel installation.

The latest branch-enabled coverage job reports 91% overall (93% statements,
85% branches). Coverage is not a substitute for tests against real upstream
behavior.

## Benchmarks

The benchmark harness remains in the repository, but the old direct-comparison
scenario is disabled. Its corpus definition was wrong and it cleared storage
before binding an isolated runtime.

No old headline numbers are used in this README. See
[docs/benchmarks.md](docs/benchmarks.md) for the rebuild requirements.

## Installation and compatibility

The package is not published to PyPI.

```bash
python3 -m pip install "git+https://github.com/liuqjjin/cogindex.git"
```

Supported versions:

- Python `>=3.11,<3.14`
- CocoIndex `>=1.0.18,<2`
- Cognee `>=1.4.0,<1.5`

The local runtime has three additional constraints:

- `data_root` and `system_root` must both be supplied explicitly;
- live `LocalCogneeRuntime` instances in one process must use the same storage
  roots;
- `LocalCogneeRuntime` accepts only `tenant="default"`; the Cognee `user`
  selects physical tenancy, and name lookup binds only datasets owned by that
  user, never a shared same-name dataset.

Version-sensitive Cognee access is centralized in
[`src/cogindex/_compat.py`](src/cogindex/_compat.py). cogindex does not patch
upstream code.

## Repository layout

```text
src/cogindex/          package implementation and public API
tests/unit/            identity, reconciliation, locks, and runtime tests
tests/property/        generated failure sequences and convergence checks
tests/integration/     local Cognee, PostgreSQL, and optional real-model tests
examples/              folder synchronization and shared-entity examples
docs/adr/              design decisions and amendments
docs/upstream-audit/   records from review of pinned upstream versions
benchmarks/            benchmark harness and scenarios
```

Pinned upstream revisions are recorded in
[`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json). The review is tiered: directly
used paths and tests receive detailed review, adjacent modules are checked for
interface shape, and the rest are classified in the inventory.

## Upstream projects

- [CocoIndex](https://github.com/cocoindex-io/cocoindex): target state,
  change detection, and tracking records.
- [Cognee](https://github.com/topoteretes/cognee): ingestion, extraction,
  graph storage, and retrieval.

See [ATTRIBUTION.md](ATTRIBUTION.md) for dependency and license details.

## License

Apache-2.0. cogindex is not affiliated with CocoIndex or Cognee.
