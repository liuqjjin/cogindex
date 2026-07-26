"""Direct Cognee versus cogindex comparison.

The previous implementation cleared Cognee's process-wide storage before it
created an isolated runtime. It also changed the edited document set between
the second and third sync, so the published scenario and measured counts did
not match.

Keep this category disabled until both sides run in separate temporary storage
roots and the result is derived from observed state rather than constants.
"""

from __future__ import annotations

from ._harness import BenchResult


async def bench_baseline_comparison(mode: str, sizes: dict[str, int]) -> BenchResult:
    """Return an explicit skip result until the isolated baseline is rebuilt."""
    del sizes
    return BenchResult(
        "baseline_comparison",
        {"mode": mode},
        {"skipped": True},
        notes=(
            "disabled: the comparison is being rebuilt with separate temporary "
            "storage roots, a fixed edit/delete corpus, and observed delete counts"
        ),
    )
