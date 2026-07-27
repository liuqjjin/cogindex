"""Benchmark CLI: ``uv run python -m benchmarks.run --profile smoke``.

Reports are written to benchmarks/reports/ as JSON and Markdown with code,
dependency, and machine metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence


def _results_failed(results: Sequence[object]) -> bool:
    """Return whether any benchmark reported a correctness or recovery failure."""
    for result in results:
        metrics = getattr(result, "metrics", {})
        if metrics.get("CORRECTNESS_FAILURE") is True:
            return True
        if "consistent" in metrics and metrics["consistent"] is not True:
            return True
        if (
            "converged_after_recovery" in metrics
            and metrics["converged_after_recovery"] is not True
        ):
            return True
        if "crashed_as_injected" in metrics and metrics["crashed_as_injected"] is not True:
            return True
        if isinstance(metrics.get("issues"), int) and metrics["issues"] > 0:
            return True
    return False


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

    try:
        env = _harness.environment_fingerprint(args.mode, args.profile)
        print(f"cogindex benchmarks: mode={args.mode} profile={args.profile}")
        results = asyncio.run(scenarios.run_all(args.mode, args.profile, args.categories))

        timestamp = env["timestamp_utc"].replace("+00:00", "Z").replace(":", "-")
        stem = f"bench_{args.mode}_{args.profile}_{timestamp}"
        json_path, md_path = _harness.write_report(env, results, stem=stem)

        for result in results:
            headline = ", ".join(
                f"{name}={value}" for name, value in list(result.metrics.items())[:3]
            )
            print(f"  {result.category:20s} {headline}")
        print(f"report: {json_path}")
        print(f"report: {md_path}")
        return 1 if _results_failed(results) else 0
    finally:
        scenarios.cleanup_benchmark_storage()


if __name__ == "__main__":
    code = main()
    # Cognee leaves non-daemon graph-worker threads alive in real mode.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
