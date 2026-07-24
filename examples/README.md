# cogindex examples

Both examples run fully locally. With an LLM configured (see
[.env.example](../.env.example)) they use it; with `--deterministic` (or by
default, for the demo) they substitute a deterministic mock LLM and
mock embeddings — the same mechanism the test suite and cognee's own tests
use. Mocked output is clearly labeled and is **not** representative of real
extraction quality; it exists so the *materialization mechanics* can be
demonstrated without credentials.

## quickstart_live.py — folder → knowledge graph

```bash
python examples/quickstart_live.py ./my-docs --deterministic
# edit / add / delete files, run again: the graph follows
python examples/quickstart_live.py ./my-docs --deterministic --search "your question"
# or keep it running and let it watch:
python examples/quickstart_live.py ./my-docs --deterministic --live
```

What it demonstrates: stable identity (file path → same Cognee document
across runs), in-place replacement on edit, cleanup on delete, and
`verify_dataset` confirming the materialized state matches the folder.

Note on batching: this example uses the idiomatic per-file component
pattern (`mount_each`), which lets the engine memoize and live-update per
file — at the cost of one small sync batch per changed file. If you ingest
thousands of documents at once and cognify cost dominates, declare
documents from a single component instead (one batched add + one cognify
per sync; see `tests/unit/test_engine_lifecycle.py` for the pattern).

## shared_entity_demo.py — provenance in action

```bash
python examples/shared_entity_demo.py
```

Three syncs, printed step by step: an entity referenced by two documents
survives the replacement of one and disappears only when its last
supporting document is removed. All deletion flows through Cognee's
provenance planner; cogindex contributes the stable identity and the
replace protocol that keep it correct.

Expected output (deterministic mode):

```
== step 1: both documents synced
   graph entities: ['AlphaCorp', 'Bob', 'Carol', 'SharedOrg']
== step 2: bob.md edited (AlphaCorp -> BetaCorp); SharedOrg must survive
   graph entities: ['BetaCorp', 'Bob', 'Carol', 'SharedOrg']
== step 3: carol.md removed; SharedOrg loses its last reference
   graph entities: ['BetaCorp', 'Bob']
```
