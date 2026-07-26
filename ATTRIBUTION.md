# Attribution

cogindex is an independent integration project. It does not vendor, copy, or
redistribute source code from either upstream project; it depends on their
published PyPI packages.

## CocoIndex

- Repository: https://github.com/cocoindex-io/cocoindex
- License: Apache License 2.0
- Relationship: cogindex implements CocoIndex's public custom target connector
  API (`TargetHandler` / `TargetActionSink` / `register_root_target_states_provider`).
  The design of cogindex's connector tests follows the patterns of CocoIndex's
  own connector test suite (`python/tests/common/target_states.py`), re-implemented
  for this project.

## Cognee

- Repository: https://github.com/topoteretes/cognee
- License: Apache License 2.0
- Relationship: cogindex drives Cognee through its public Python API
  (`cognee.add`, `cognee.cognify`, `cognee.forget` and dataset inspection)
  plus the `DataItem` dataclass (`cognee.tasks.ingestion.data_item`), which is
  not yet re-exported at the package top level (see
  `docs/upstream-proposals/`). The examples use `cognee.search` only to show
  their resulting graph.

## Audited versions

The exact upstream commits this project was audited and developed against are
pinned in [`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json). The repository-wide
inventory and targeted source-review ledger live in
[`docs/upstream-audit/`](docs/upstream-audit/).
