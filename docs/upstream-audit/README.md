# Upstream source audit ledger

This directory is the audit trail for the two upstream codebases cogindex
integrates with. The audited commits are pinned in
[`../../UPSTREAM_LOCK.json`](../../UPSTREAM_LOCK.json); the clones live in the
gitignored `.upstream/` directory.

Combined scale at the audited commits: **~3,900 tracked files, ~298k lines of
first-party source and ~178k lines of tests**. Nobody reads that end-to-end in
one pass, and this ledger does not pretend to. Instead it records, for every
tracked file, *what it is* and *how deeply it was reviewed*, so the claim
"audited" is checkable.

## Layout

- `tools/generate_inventory.py` — regenerates the mechanical inventory from a
  clone (`git ls-files` + classification rules + LOC + blob hashes).
- `cocoindex/inventory.jsonl`, `cognee/inventory.jsonl` — one record per
  tracked file: path, language, LOC, blob hash, module, category
  (`first-party source` / `test` / `build/CI` / `docs/example` /
  `generated/vendor/binary` / `irrelevant` with reason), audit status,
  relevance, notes.
- `cocoindex/summary.md`, `cognee/summary.md` — roll-ups by category/module.
- `cocoindex/review-state.jsonl`, `cognee/review-state.jsonl` — hand-maintained
  review results merged over the mechanical inventory: per file or per module,
  `audit_status ∈ {reviewed-deep, reviewed-skim, classified-only}`, relevance
  to this connector, and risk notes.
- `cocoindex/findings.md`, `cognee/findings.md` — the semantic audit: what the
  state machinery actually does, with file/line references, and every place
  upstream documentation disagreed with upstream code.

## Method

1. The inventory is generated mechanically; classification is rule-based and
   the rules are in the generator.
2. Files on the connector's critical path (target-state engine, ingestion,
   deletion/provenance, pipeline status, locking, config) were read in full
   ("reviewed-deep"), including their tests — test assertions are treated as
   the authoritative statement of upstream semantics.
3. Adjacent modules were skimmed for interface shape ("reviewed-skim").
4. Everything else is "classified-only": identified, categorized, and judged
   not to affect the integration, with the judgment recorded per module.

The release gate requires every first-party source file to carry an explicit
status; "we read everything deeply" is not claimed anywhere.
