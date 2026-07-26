from __future__ import annotations

from benchmarks.baseline import bench_baseline_comparison


async def test_real_baseline_is_disabled_until_storage_is_isolated() -> None:
    result = await bench_baseline_comparison("real", {})

    assert result.metrics == {"skipped": True}
    assert "separate temporary storage roots" in result.notes
