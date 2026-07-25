"""Benchmark CLI: ``python -m benchmarks.run --profile smoke``.

Reports land in benchmarks/reports/ (gitignored) as JSON + Markdown with a
full environment fingerprint. Numbers are machine-specific; regenerate
locally, never quote in documentation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=["smoke", "default", "large"],
        default="default",
        help="document-count profile (smoke validates the harness end to end)",
    )
    parser.add_argument(
        "--mode",
        choices=["fake", "real"],
        default="fake",
        help=(
            "fake: connector layer over an in-memory Cognee emulator; "
            "real: real local Cognee stack with deterministic LLM/embedding "
            "substitutes (slower, smaller corpora)"
        ),
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        metavar="NAME",
        help="subset of categories to run (default: all)",
    )
    args = parser.parse_args(argv)

    if args.mode == "real":
        # Must be set before cognee is imported anywhere in this process.
        os.environ.setdefault("TELEMETRY_DISABLED", "1")
        os.environ.setdefault("MOCK_EMBEDDING", "true")

    from . import _harness, scenarios

    unknown = set(args.categories or []) - set(scenarios.CATEGORIES)
    if unknown:
        parser.error(
            f"unknown categories: {sorted(unknown)}; valid: {sorted(scenarios.CATEGORIES)}"
        )

    env = _harness.environment_fingerprint(args.mode, args.profile)
    print(f"cogindex benchmarks: mode={args.mode} profile={args.profile}")
    results = asyncio.run(scenarios.run_all(args.mode, args.profile, args.categories))

    stem = f"bench_{args.mode}_{args.profile}_" + env["timestamp_utc"].replace(":", "-").replace(
        "+", "Z"
    )
    json_path, md_path = _harness.write_report(env, results, stem=stem)

    for result in results:
        headline = ", ".join(f"{name}={value}" for name, value in list(result.metrics.items())[:3])
        print(f"  {result.category:20s} {headline}")
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    code = main()
    # Hard exit rather than returning through the interpreter's shutdown. In
    # real mode cognee leaves its graph-worker harness running on non-daemon
    # threads, so a normal exit blocks in threading._shutdown long after the
    # reports are on disk (observed: half an hour). Everything this process
    # owns is already flushed by here.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
