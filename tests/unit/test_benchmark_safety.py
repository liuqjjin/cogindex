from __future__ import annotations

from benchmarks._harness import BenchResult
from benchmarks.baseline import bench_baseline_comparison
from benchmarks.run import _results_failed


async def test_fake_baseline_observes_incremental_work_and_consistency() -> None:
    result = await bench_baseline_comparison(
        "fake",
        {
            "n_docs": 9,
            "k_changes": 2,
            "baseline_repetitions": 3,
        },
    )

    assert result.params == {
        "mode": "fake",
        "n_docs": 9,
        "k_changes": 2,
        "k_deleted": 1,
        "repetitions": 3,
    }
    assert result.metrics["full_reindex_processed_documents"] == 8
    assert result.metrics["incremental_processed_documents"] == 2
    assert result.metrics["full_reindex_unnecessary_reprocessed"] == 6
    assert result.metrics["incremental_unnecessary_reprocessed"] == 0
    assert result.metrics["consistent"] is True
    assert "CORRECTNESS_FAILURE" not in result.metrics


def test_benchmark_exit_status_rejects_correctness_and_recovery_failures() -> None:
    assert not _results_failed([BenchResult("ok", {}, {"consistent": True, "issues": 0})])
    assert _results_failed([BenchResult("bad", {}, {"CORRECTNESS_FAILURE": True})])
    assert _results_failed([BenchResult("bad", {}, {"consistent": False})])
    assert _results_failed([BenchResult("bad", {}, {"converged_after_recovery": False})])
    assert _results_failed([BenchResult("bad", {}, {"issues": 1})])
