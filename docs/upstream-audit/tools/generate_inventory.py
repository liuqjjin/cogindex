#!/usr/bin/env python3
"""Generate the mechanical part of the upstream source-audit ledger.

Walks a git clone, classifies every tracked file, and emits:
  - ``inventory.jsonl``  — one record per tracked file (machine-readable)
  - ``summary.md``       — category/module roll-up (human-readable)

The generated records intentionally leave ``audit_status`` as
``"mechanical"``; deep/skim review states are recorded separately in
``review-state.jsonl`` (hand-maintained) and merged by ``merge_review.py``.

Usage:
    python generate_inventory.py <clone_dir> <output_dir> --name cocoindex
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Category rules are ordered: first match wins.
# (category, reason) selected by predicate on the repo-relative posix path.
_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".webp",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".mov",
    ".zip",
    ".gz",
    ".parquet",
    ".lance",
    ".bin",
    ".onnx",
    ".pt",
    ".npy",
    ".db",
    ".sqlite",
    ".xlsx",
    ".pptx",
    ".docx",
    ".wasm",
}
_DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc", ".ipynb"}
_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python-stub",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonl": "json",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".html": "html",
    ".css": "css",
    ".astro": "astro",
    ".proto": "protobuf",
    ".cfg": "ini",
    ".ini": "ini",
    ".ipynb": "notebook",
    ".robot": "robotframework",
    ".tf": "terraform",
}
_CI_BASENAMES = {
    "makefile",
    "dockerfile",
    "justfile",
    ".dockerignore",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".prettierrc",
    ".eslintrc",
    ".npmrc",
    ".python-version",
    ".tool-versions",
    "renovate.json",
    ".releaserc",
    ".codecov.yml",
    "codecov.yml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "mkdocs.yml",
    "conftest.py",
    ".env.template",
    ".env.example",
    ".deepsource.toml",
    "vercel.json",
    ".coderabbit.yaml",
    "lefthook.yml",
    "prek.yaml",
}
_LOCK_BASENAMES = {
    "uv.lock",
    "cargo.lock",
    "package-lock.json",
    "poetry.lock",
    "yarn.lock",
    "pnpm-lock.yaml",
    "composer.lock",
    "gemfile.lock",
}


def classify(path: str) -> tuple[str, str]:
    """Return (category, reason) for a repo-relative posix path."""
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    parts = p.split("/")

    if base in _LOCK_BASENAMES:
        return "generated/vendor/binary", "dependency lockfile"
    if ext in _BINARY_EXTS:
        return "generated/vendor/binary", f"binary asset ({ext})"
    if "vendor" in parts or "vendored" in parts or "third_party" in parts:
        return "generated/vendor/binary", "vendored third-party code"
    if any(seg in ("node_modules", "dist", "generated", "__generated__") for seg in parts):
        return "generated/vendor/binary", "generated/build output"

    if parts[0] == ".github" or base in _CI_BASENAMES:
        return "build/CI", ".github or build/config file"
    if ext in (".yml", ".yaml") and any(
        s in p
        for s in ("docker-compose", "compose.", "ci", "workflow", "helm", "k8s", "kubernetes")
    ):
        return "build/CI", "CI/deploy config"
    if base in (
        "pyproject.toml",
        "cargo.toml",
        "package.json",
        "tsconfig.json",
        "build.rs",
        "setup.py",
    ):
        return "build/CI", "build manifest"
    if parts[0] in ("deployment", "helm", "docker", "ops", "infra", ".devcontainer", "dev"):
        return "build/CI", f"ops/dev tooling tree ({parts[0]}/)"

    if (
        any(seg in ("tests", "test", "e2e_tests", "integration_tests") for seg in parts)
        or base.startswith("test_")
        or base.endswith("_test.py")
    ):
        return "test", "test tree or test_* naming"

    if parts[0] in (
        "docs",
        "doc",
        "examples",
        "demos",
        "notebooks",
        "examples_data",
        "assets",
        "static",
    ):
        return "docs/example", f"docs/examples tree ({parts[0]}/)"
    if ext in _DOC_EXTS:
        return "docs/example", f"documentation file ({ext})"

    if ext in (".py", ".pyi", ".rs", ".sql", ".sh", ".bash", ".proto"):
        return "first-party source", ""
    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".html", ".css", ".astro"):
        # Frontend / site code: first-party but never on this connector's path.
        return "first-party source", "frontend/site code"
    if ext in (".json", ".jsonl", ".toml", ".yaml", ".yml", ".cfg", ".ini"):
        return "irrelevant", "data/config file with no executable semantics"
    return "irrelevant", f"unclassified extension ({ext or 'none'})"


def module_of(path: str, repo: str) -> str:
    parts = path.split("/")
    if repo == "cocoindex":
        if path.startswith("python/cocoindex/connectors/") and len(parts) > 3:
            return f"python/connectors/{parts[3]}"
        if path.startswith("python/cocoindex/"):
            return "/".join(["python", *parts[2:3]])
        if path.startswith("rust/"):
            return "/".join(parts[:3])
        return parts[0]
    # cognee
    if path.startswith("cognee/") and len(parts) > 2:
        return f"cognee/{parts[1]}" + (
            f"/{parts[2]}"
            if parts[1] in ("modules", "infrastructure", "api", "tasks") and len(parts) > 3
            else ""
        )
    return parts[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clone_dir", type=Path)
    ap.add_argument("output_dir", type=Path)
    ap.add_argument("--name", required=True, choices=["cocoindex", "cognee"])
    args = ap.parse_args()

    ls = subprocess.run(
        ["git", "-C", str(args.clone_dir), "ls-files", "-s"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(args.clone_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    records = []
    for line in ls.stdout.splitlines():
        meta, path = line.split("\t", 1)
        _mode, blob_sha, _stage = meta.split()
        category, reason = classify(path)
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        lang = _LANG_BY_EXT.get(ext, "other")
        loc = None
        if category in ("first-party source", "test") and lang not in ("other",):
            fp = args.clone_dir / path
            try:
                with open(fp, "rb") as f:
                    loc = sum(1 for _ in f)
            except OSError:
                loc = None
        records.append(
            {
                "path": path,
                "repo": args.name,
                "commit": commit,
                "blob_sha": blob_sha,
                "language": lang,
                "loc": loc,
                "module": module_of(path, args.name),
                "category": category,
                "category_reason": reason or None,
                "audit_status": "mechanical",
                "relevance": None,
                "notes": None,
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    inv = args.output_dir / "inventory.jsonl"
    with open(inv, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_cat = Counter(r["category"] for r in records)
    loc_by_cat: Counter[str] = Counter()
    for r in records:
        if r["loc"]:
            loc_by_cat[r["category"]] += r["loc"]
    by_module = Counter(r["module"] for r in records if r["category"] == "first-party source")
    with open(args.output_dir / "summary.md", "w") as f:
        f.write(f"# {args.name} inventory summary\n\n")
        f.write(f"- commit: `{commit}`\n- tracked files: {len(records)}\n\n")
        f.write(
            "## Files by category\n\n| category | files | LOC (source/test only) |\n|---|---|---|\n"
        )
        for cat, n in by_cat.most_common():
            f.write(f"| {cat} | {n} | {loc_by_cat.get(cat, '')} |\n")
        f.write("\n## First-party source files by module\n\n| module | files |\n|---|---|\n")
        for mod, n in sorted(by_module.items(), key=lambda kv: -kv[1]):
            f.write(f"| {mod} | {n} |\n")
    print(f"{args.name}: {len(records)} files -> {inv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
