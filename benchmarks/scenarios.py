"""Benchmark scenarios for synchronization, deletion, recovery, and verification.

0. baseline_comparison  disabled until its storage isolation is rebuilt
1. initial_ingest       first sync of N documents
2. incremental_update   resync after changing K of N
3. freshness            per-change sync latency distribution
4. deletion             remove K of N documents
5. crash_recovery       retry after an injected mid-batch failure (fake mode)
6. verify_read          drift verification read path over N documents

Modes:
- ``fake``: connector layer over the in-memory FakeCogneeRuntime: measures
  cogindex + cocoindex engine overhead in isolation.
- ``real``: real local Cognee stack (SQLite + LanceDB + embedded graph) with
  deterministic LLM/embedding substitutes: measures the full local pipeline
  without model latency. Much smaller document counts.

All scenarios use one asyncio loop because Cognee caches async engines per loop.
"""

from __future__ import annotations

import contextlib
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cocoindex as coco

import cogindex
from cogindex import ExpectedDocument, ProcessingConfig, verify_dataset
from cogindex.testing import FakeCogneeRuntime

from ._harness import BenchResult, percentile
from .baseline import bench_baseline_comparison

RUNTIME_KEY = coco.ContextKey[cogindex.CogneeRuntime]("cogindex_bench_runtime")
PROCESSING = ProcessingConfig(graph_model_id="bench.Model")
TENANT = "default"

FAKE_PROFILES: dict[str, dict[str, int]] = {
    "smoke": {"n_docs": 40, "k_changes": 6, "m_freshness": 6},
    "default": {"n_docs": 500, "k_changes": 25, "m_freshness": 30},
    "large": {"n_docs": 5000, "k_changes": 100, "m_freshness": 50},
}
REAL_PROFILES: dict[str, dict[str, int]] = {
    "smoke": {"n_docs": 6, "k_changes": 2, "m_freshness": 2},
    "default": {"n_docs": 24, "k_changes": 6, "m_freshness": 5},
    "large": {"n_docs": 80, "k_changes": 12, "m_freshness": 8},
}


@coco.fn
async def _bench_main(dataset: str, docs_holder: dict[str, dict[str, str]]) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target, RUNTIME_KEY, dataset, processing=PROCESSING
    )
    for key, content in docs_holder["docs"].items():
        target.declare_document(key, content)


def _docs(n: int, *, version: int = 0, changed_first: int = 0) -> dict[str, str]:
    """N deterministic documents; the first ``changed_first`` get ``version``."""
    return {
        f"doc-{i:05d}.md": (
            f"Document {i} version {version if i < changed_first else 0} "
            f"mentions Entity{i % 7} and SharedEntity."
        )
        for i in range(n)
    }


class BenchContext:
    """Isolated CocoIndex environment and Cognee runtime for one scenario."""

    def __init__(self, mode: str, label: str) -> None:
        self.mode = mode
        self.label = label
        db_path = Path(tempfile.mkdtemp()) / f"bench-{label}"
        self.env = coco.Environment(coco.Settings.from_env(db_path=db_path))
        self.runtime: cogindex.CogneeRuntime
        if mode == "fake":
            self.runtime = FakeCogneeRuntime()
        else:
            storage = Path(tempfile.mkdtemp())
            self.runtime = cogindex.LocalCogneeRuntime(
                data_root=storage / "data", system_root=storage / "system"
            )
        self.env.context_provider.provide(RUNTIME_KEY, self.runtime)
        self.dataset = f"bench_{label}"
        # Reuse one App so repeated updates share the same tracking history.
        self._docs_holder: dict[str, dict[str, str]] = {"docs": {}}
        self._app = coco.App(
            coco.AppConfig(name=f"bench_{label}", environment=self.env),
            _bench_main,
            dataset=self.dataset,
            docs_holder=self._docs_holder,
        )

    async def prepare(self) -> None:
        if self.mode == "real":
            import cognee
            from cognee.modules.engine.operations.setup import setup

            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)
            await setup()

    async def sync(self, docs: dict[str, str]) -> float:
        self._docs_holder["docs"] = docs
        started = time.perf_counter()
        await self._app.update().result()
        return time.perf_counter() - started

    def llm_calls(self) -> int:
        """Return extraction call count, or -1 when the mode has no LLM."""
        if self.mode != "real":
            return -1
        from cognee.infrastructure.llm import LLMGateway

        count = getattr(LLMGateway.acreate_structured_output, "call_count", None)
        return int(count) if count is not None else -1

    # -- fake-mode op accounting ---------------------------------------------

    def op_calls(self, op: str) -> int:
        if isinstance(self.runtime, FakeCogneeRuntime):
            return sum(1 for call in self.runtime.calls if call[0] == op)
        return -1

    def added_ids(self) -> int:
        if isinstance(self.runtime, FakeCogneeRuntime):
            return sum(len(call[2]) for call in self.runtime.calls if call[0] == "add_documents")
        return -1


@contextlib.contextmanager
def deterministic_llm() -> Iterator[None]:
    """Patch cognee's LLM gateway deterministically (real mode).

    Same pattern as the integration tests and upstream's own pipeline tests.
    """
    from unittest.mock import AsyncMock, patch

    from cognee.infrastructure.llm import LLMGateway
    from cognee.shared.data_models import KnowledgeGraph, Node, SummarizedContent

    def fake_llm(text_input: str, system_prompt: str, response_model: type, **kwargs: Any) -> Any:
        if text_input == "test":
            return "test"
        if response_model is SummarizedContent:
            return SummarizedContent(summary="bench summary", description="bench summary")
        if response_model is KnowledgeGraph:
            # Keep marker tokens distinct so per-document changes remain visible.
            token = next(
                (
                    word.rstrip(".,")
                    for word in text_input.split()
                    if word[:1].isupper() and word.rstrip(".,") != "Document"
                ),
                "BenchNode",
            )
            return KnowledgeGraph(
                nodes=[Node(id=token, name=token, type="Thing", description=token)],
                edges=[],
            )
        raise AssertionError(f"unmocked LLM response model: {response_model!r}")

    with patch.object(LLMGateway, "acreate_structured_output", new_callable=AsyncMock) as mock:
        mock.side_effect = fake_llm
        yield


# ---------------------------------------------------------------------------
# categories
# ---------------------------------------------------------------------------


async def bench_initial_ingest(mode: str, sizes: dict[str, int]) -> BenchResult:
    n = sizes["n_docs"]
    ctx = BenchContext(mode, "initial")
    await ctx.prepare()
    seconds = await ctx.sync(_docs(n))
    metrics: dict[str, Any] = {
        "seconds": round(seconds, 4),
        "docs_per_second": round(n / seconds, 1),
    }
    if mode == "fake":
        metrics["add_batches"] = ctx.op_calls("add_documents")
        metrics["cognify_calls"] = ctx.op_calls("cognify_dataset")
    return BenchResult("initial_ingest", {"n_docs": n, "mode": mode}, metrics)


async def bench_incremental_update(mode: str, sizes: dict[str, int]) -> BenchResult:
    n, k = sizes["n_docs"], sizes["k_changes"]
    ctx = BenchContext(mode, "incremental")
    await ctx.prepare()

    llm_start = ctx.llm_calls()
    full_seconds = await ctx.sync(_docs(n))
    llm_after_full = ctx.llm_calls()
    adds_before = ctx.added_ids()

    unchanged_seconds = await ctx.sync(_docs(n))
    llm_after_unchanged = ctx.llm_calls()

    inc_seconds = await ctx.sync(_docs(n, version=1, changed_first=k))
    llm_after_incremental = ctx.llm_calls()

    metrics: dict[str, Any] = {
        "full_sync_seconds": round(full_seconds, 4),
        "unchanged_resync_seconds": round(unchanged_seconds, 4),
        "incremental_seconds": round(inc_seconds, 4),
        "incremental_vs_full_ratio": round(inc_seconds / full_seconds, 3),
        "changed_docs": k,
    }
    if mode == "fake":
        adds_incremental = ctx.added_ids() - adds_before
        metrics["docs_written_incrementally"] = adds_incremental
        metrics["wasted_writes"] = adds_incremental - k
    else:
        metrics["llm_calls_full_sync"] = llm_after_full - llm_start
        metrics["llm_calls_unchanged_resync"] = llm_after_unchanged - llm_after_full
        metrics["llm_calls_incremental"] = llm_after_incremental - llm_after_unchanged
    return BenchResult(
        "incremental_update",
        {"n_docs": n, "k_changes": k, "mode": mode},
        metrics,
        notes=(
            "Reports writes in fake mode and extraction calls in real mode. "
            f"The scenario changes {k} of {n} documents; no complexity claim "
            "is inferred from one sample."
        ),
    )


async def bench_freshness(mode: str, sizes: dict[str, int]) -> BenchResult:
    n, m = sizes["n_docs"], sizes["m_freshness"]
    ctx = BenchContext(mode, "freshness")
    await ctx.prepare()
    await ctx.sync(_docs(n))
    latencies: list[float] = []
    for round_number in range(1, m + 1):
        docs = _docs(n, version=round_number, changed_first=1)
        latencies.append(await ctx.sync(docs))
    return BenchResult(
        "freshness",
        {"n_docs": n, "updates": m, "mode": mode},
        {
            "p50_ms": round(percentile(latencies, 0.5) * 1000, 1),
            "p95_ms": round(percentile(latencies, 0.95) * 1000, 1),
            "mean_ms": round(sum(latencies) / len(latencies) * 1000, 1),
        },
        notes="elapsed time for one single-document update run.",
    )


async def bench_deletion(mode: str, sizes: dict[str, int]) -> BenchResult:
    n, k = sizes["n_docs"], sizes["k_changes"]
    ctx = BenchContext(mode, "deletion")
    await ctx.prepare()
    await ctx.sync(_docs(n))
    kept = {key: content for index, (key, content) in enumerate(_docs(n).items()) if index >= k}
    seconds = await ctx.sync(kept)
    handle = await ctx.runtime.resolve_dataset(ctx.dataset, TENANT)
    remaining = len(await ctx.runtime.list_documents(handle))
    metrics: dict[str, Any] = {
        "seconds": round(seconds, 4),
        "deleted": k,
        "remaining": remaining,
        "remaining_expected": n - k,
    }
    if remaining != n - k:
        metrics["CORRECTNESS_FAILURE"] = True
    return BenchResult("deletion", {"n_docs": n, "k_deleted": k, "mode": mode}, metrics)


async def bench_crash_recovery(mode: str, sizes: dict[str, int]) -> BenchResult:
    n, k = sizes["n_docs"], sizes["k_changes"]
    if mode != "fake":
        return BenchResult(
            "crash_recovery",
            {"mode": mode},
            {"skipped": True},
            notes="fault injection requires the fake runtime; not run in real mode.",
        )
    ctx = BenchContext(mode, "crash")
    await ctx.prepare()
    await ctx.sync(_docs(n))
    adds_before = ctx.added_ids()
    assert isinstance(ctx.runtime, FakeCogneeRuntime)
    ctx.runtime.inject_fault("cognify_dataset")
    changed = _docs(n, version=1, changed_first=k)
    crashed = False
    started = time.perf_counter()
    try:
        await ctx.sync(changed)
    except Exception:
        crashed = True
    crash_seconds = time.perf_counter() - started
    ctx.runtime.clear_faults()
    recovery_seconds = await ctx.sync(changed)
    unconverged = ctx.runtime.unconverged_documents(TENANT, ctx.dataset)
    writes_total = ctx.added_ids() - adds_before
    return BenchResult(
        "crash_recovery",
        {"n_docs": n, "k_changes": k, "mode": mode},
        {
            "crashed_as_injected": crashed,
            "crashed_sync_seconds": round(crash_seconds, 4),
            "recovery_seconds": round(recovery_seconds, 4),
            "converged_after_recovery": unconverged == [],
            "total_writes_including_retry": writes_total,
            "minimum_possible_writes": k,
        },
        notes="observed writes and end state after one injected failure and retry.",
    )


async def bench_verify_read(mode: str, sizes: dict[str, int]) -> BenchResult:
    n = sizes["n_docs"]
    ctx = BenchContext(mode, "verify")
    await ctx.prepare()
    await ctx.sync(_docs(n))
    expected = [ExpectedDocument(key) for key in _docs(n)]
    started = time.perf_counter()
    report = await verify_dataset(ctx.runtime, RUNTIME_KEY, ctx.dataset, expected, tenant=TENANT)
    seconds = time.perf_counter() - started
    return BenchResult(
        "verify_read",
        {"n_docs": n, "mode": mode},
        {
            "seconds": round(seconds, 4),
            "docs_per_second": round(n / seconds, 1),
            "issues": len(report.issues),
        },
    )


CATEGORIES = {
    "baseline_comparison": bench_baseline_comparison,
    "initial_ingest": bench_initial_ingest,
    "incremental_update": bench_incremental_update,
    "freshness": bench_freshness,
    "deletion": bench_deletion,
    "crash_recovery": bench_crash_recovery,
    "verify_read": bench_verify_read,
}


async def run_all(mode: str, profile: str, categories: list[str] | None) -> list[BenchResult]:
    sizes = (REAL_PROFILES if mode == "real" else FAKE_PROFILES)[profile]
    selected = categories or list(CATEGORIES)
    llm_context = deterministic_llm() if mode == "real" else contextlib.nullcontext()
    results: list[BenchResult] = []
    with llm_context:
        for name in selected:
            results.append(await CATEGORIES[name](mode, sizes))
    return results
