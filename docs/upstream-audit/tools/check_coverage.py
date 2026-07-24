"""Verify the audit-ledger release gate: every first-party source file in the
mechanical inventory must be covered by a review-state entry (an exact file
entry, or a module entry whose path prefix covers it).

Usage: python docs/upstream-audit/tools/check_coverage.py
Exit code 1 with a gap listing if any first-party file is uncovered.
"""

from __future__ import annotations

import json
import pathlib
import sys

LEDGER_ROOT = pathlib.Path(__file__).resolve().parent.parent
REPOS = ("cocoindex", "cognee")
FIRST_PARTY = "first-party source"


def load_jsonl(path: pathlib.Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def check_repo(repo: str) -> tuple[int, int, list[str]]:
    inventory = load_jsonl(LEDGER_ROOT / repo / "inventory.jsonl")
    review = load_jsonl(LEDGER_ROOT / repo / "review-state.jsonl")

    file_entries: set[str] = set()
    module_prefixes: list[str] = []
    for entry in review:
        raw_path = str(entry["path"])
        if entry["scope"] == "file":
            file_entries.add(raw_path)
        else:
            # A module entry may list several whitespace-separated prefixes.
            module_prefixes.extend(p for p in raw_path.split() if p)

    gaps: list[str] = []
    total = 0
    for record in inventory:
        if record.get("category") != FIRST_PARTY:
            continue
        total += 1
        path = str(record["path"])
        if path in file_entries:
            continue
        if any(path.startswith(prefix) for prefix in module_prefixes):
            continue
        gaps.append(path)

    stale = sorted(
        p for p in file_entries if not any(str(r["path"]) == p for r in inventory)
    )
    for p in stale:
        print(f"  WARN {repo}: review-state file entry not in inventory: {p}")

    return total, total - len(gaps), gaps


def main() -> int:
    failed = False
    for repo in REPOS:
        total, covered, gaps = check_repo(repo)
        print(f"{repo}: {covered}/{total} first-party source files covered")
        if gaps:
            failed = True
            for path in gaps[:40]:
                print(f"  GAP  {path}")
            if len(gaps) > 40:
                print(f"  ... and {len(gaps) - 40} more")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
