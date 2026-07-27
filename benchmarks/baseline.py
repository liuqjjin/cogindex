"""Full dataset rebuild versus cogindex incremental synchronization."""

from __future__ import annotations

import time
from typing import Any

import cogindex

from ._harness import BenchResult, percentile


def _payloads(
    runtime_key: str,
    dataset: str,
    docs: dict[str, str],
    *,
    tenant: str,
) -> list[cogindex.DocumentPayload]:
    return [
        cogindex.DocumentPayload(
            data_id=cogindex.document_data_id(runtime_key, tenant, dataset, key),
            content=content,
        )
        for key, content in docs.items()
    ]


async def _initial_full_load(
    runtime: cogindex.CogneeRuntime,
    runtime_key: str,
    dataset: str,
    docs: dict[str, str],
    *,
    tenant: str,
) -> None:
    handle = await runtime.resolve_dataset(dataset, tenant)
    async with runtime.dataset_lock(handle):
        handle = await runtime.add_documents(
            handle,
            _payloads(runtime_key, dataset, docs, tenant=tenant),
        )
        await runtime.cognify_dataset(handle, cogindex.CognifyProfile())


async def _full_rebuild(
    runtime: cogindex.CogneeRuntime,
    runtime_key: str,
    dataset: str,
    docs: dict[str, str],
    *,
    tenant: str,
) -> float:
    """Delete the comparison dataset and rebuild its current desired contents."""
    handle = await runtime.resolve_dataset(dataset, tenant)
    started = time.perf_counter()
    async with runtime.dataset_lock(handle):
        await runtime.teardown_dataset(handle)
        handle = await runtime.resolve_dataset(dataset, tenant)
        handle = await runtime.add_documents(
            handle,
            _payloads(runtime_key, dataset, docs, tenant=tenant),
        )
        await runtime.cognify_dataset(handle, cogindex.CognifyProfile())
    return time.perf_counter() - started


async def _graph_markers(dataset_id: Any) -> set[str]:
    """Read benchmark entity markers from a real local Cognee graph."""
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    async with set_database_global_context_variables(dataset_id, user.id):
        engine = await get_graph_engine()
        nodes, _ = await engine.get_graph_data()
    return {
        str(properties.get("name", "")).lower()
        for _, properties in nodes
        if isinstance(properties, dict)
        and str(properties.get("name", "")).lower().startswith(("entity", "replacement"))
    }


async def _check_dataset(
    *,
    mode: str,
    runtime: cogindex.CogneeRuntime,
    runtime_key: Any,
    dataset: str,
    docs: dict[str, str],
    changed: int,
    deleted_indexes: range,
    tenant: str,
) -> dict[str, Any]:
    expected = [cogindex.ExpectedDocument(key) for key in docs]
    report = await cogindex.verify_dataset(
        runtime,
        runtime_key,
        dataset,
        expected,
        tenant=tenant,
    )
    handle = await runtime.resolve_dataset(dataset, tenant)
    stored = await runtime.list_documents(handle)

    result: dict[str, Any] = {
        "record_check_ok": report.ok,
        "record_issues": len(report.issues),
        "final_documents": len(stored),
        "derivatives_consistent": True,
        "stale_entities": None,
        "missing_entities": None,
    }
    if mode == "fake":
        from cogindex.testing import FakeCogneeRuntime

        if not isinstance(runtime, FakeCogneeRuntime):
            raise TypeError("fake benchmark did not receive FakeCogneeRuntime")
        result["derivatives_consistent"] = not runtime.unconverged_documents(tenant, dataset)
        return result

    if handle.dataset_id is None:
        result["derivatives_consistent"] = False
        return result

    actual = await _graph_markers(handle.dataset_id)
    desired = {
        (f"replacement{index:05d}" if index < changed else f"entity{index:05d}")
        for index in range(len(docs) + len(deleted_indexes))
        if index not in deleted_indexes
    }
    obsolete = {f"entity{index:05d}" for index in range(changed)}
    obsolete.update(f"entity{index:05d}" for index in deleted_indexes)
    stale = actual & obsolete
    missing = desired - actual
    result["stale_entities"] = len(stale)
    result["missing_entities"] = len(missing)
    result["derivatives_consistent"] = not stale and not missing
    return result


def _same(values: list[int], label: str) -> int:
    if len(set(values)) != 1:
        raise RuntimeError(f"{label} changed between repetitions: {values}")
    return values[0]


async def bench_baseline_comparison(mode: str, sizes: dict[str, int]) -> BenchResult:
    """Compare a fair full rebuild with a fixed incremental edit/delete set."""
    # Imported here to keep the scenario registry simple without duplicating
    # its isolated environment and deterministic corpus builder.
    from .scenarios import RUNTIME_KEY, TENANT, BenchContext, _docs

    n = sizes["n_docs"]
    k = sizes["k_changes"]
    repetitions = sizes.get("baseline_repetitions", 3)
    deleted = max(1, min(k // 2, n - k))
    if repetitions < 1:
        raise ValueError("baseline_repetitions must be positive")
    if k < 1 or k + deleted > n:
        raise ValueError("benchmark requires disjoint, non-empty edit and delete sets")

    initial_docs = _docs(n)
    desired_docs = _docs(n, version=1, changed_first=k)
    deleted_indexes = range(n - deleted, n)
    desired_docs = {
        key: content
        for index, (key, content) in enumerate(desired_docs.items())
        if index not in deleted_indexes
    }

    ctx = BenchContext(mode, "baseline")
    await ctx.prepare()
    full_times: list[float] = []
    incremental_times: list[float] = []
    full_processed: list[int] = []
    incremental_processed: list[int] = []
    full_checks: list[dict[str, Any]] = []
    incremental_checks: list[dict[str, Any]] = []

    for repetition in range(repetitions):
        full_dataset = f"{ctx.dataset}_full_{repetition}"
        incremental_dataset = f"{ctx.dataset}_incremental_{repetition}"

        await _initial_full_load(
            ctx.observed_runtime,
            RUNTIME_KEY.key,
            full_dataset,
            initial_docs,
            tenant=TENANT,
        )
        await ctx.sync(initial_docs, dataset=incremental_dataset)

        full_cursor = len(ctx.observed_runtime.add_calls)
        if repetition % 2 == 0:
            full_times.append(
                await _full_rebuild(
                    ctx.observed_runtime,
                    RUNTIME_KEY.key,
                    full_dataset,
                    desired_docs,
                    tenant=TENANT,
                )
            )
            incremental_times.append(await ctx.sync(desired_docs, dataset=incremental_dataset))
        else:
            incremental_times.append(await ctx.sync(desired_docs, dataset=incremental_dataset))
            full_times.append(
                await _full_rebuild(
                    ctx.observed_runtime,
                    RUNTIME_KEY.key,
                    full_dataset,
                    desired_docs,
                    tenant=TENANT,
                )
            )

        full_processed.append(len(ctx.observed_runtime.added_ids(full_dataset, after=full_cursor)))
        incremental_processed.append(
            len(ctx.observed_runtime.added_ids(incremental_dataset, after=full_cursor))
        )
        full_checks.append(
            await _check_dataset(
                mode=mode,
                runtime=ctx.runtime,
                runtime_key=RUNTIME_KEY,
                dataset=full_dataset,
                docs=desired_docs,
                changed=k,
                deleted_indexes=deleted_indexes,
                tenant=TENANT,
            )
        )
        incremental_checks.append(
            await _check_dataset(
                mode=mode,
                runtime=ctx.runtime,
                runtime_key=RUNTIME_KEY,
                dataset=incremental_dataset,
                docs=desired_docs,
                changed=k,
                deleted_indexes=deleted_indexes,
                tenant=TENANT,
            )
        )

    full_count = _same(full_processed, "full rebuild processed count")
    incremental_count = _same(incremental_processed, "incremental processed count")
    final_documents = n - deleted
    consistent = all(
        check["record_check_ok"]
        and check["final_documents"] == final_documents
        and check["derivatives_consistent"]
        for check in [*full_checks, *incremental_checks]
    )
    metrics: dict[str, Any] = {
        "full_reindex_median_seconds": round(percentile(full_times, 0.5), 4),
        "full_reindex_min_seconds": round(min(full_times), 4),
        "full_reindex_max_seconds": round(max(full_times), 4),
        "incremental_median_seconds": round(percentile(incremental_times, 0.5), 4),
        "incremental_min_seconds": round(min(incremental_times), 4),
        "incremental_max_seconds": round(max(incremental_times), 4),
        "full_reindex_processed_documents": full_count,
        "incremental_processed_documents": incremental_count,
        "full_reindex_unnecessary_reprocessed": full_count - k,
        "incremental_unnecessary_reprocessed": incremental_count - k,
        "final_documents": final_documents,
        "full_reindex_record_check": all(check["record_check_ok"] for check in full_checks),
        "incremental_record_check": all(check["record_check_ok"] for check in incremental_checks),
        "full_reindex_derivative_check": all(
            check["derivatives_consistent"] for check in full_checks
        ),
        "incremental_derivative_check": all(
            check["derivatives_consistent"] for check in incremental_checks
        ),
        "consistent": consistent,
        "full_reindex_seconds_samples": [round(value, 4) for value in full_times],
        "incremental_seconds_samples": [round(value, 4) for value in incremental_times],
    }
    if mode == "real":
        metrics["full_reindex_stale_entities"] = sum(
            int(check["stale_entities"]) for check in full_checks
        )
        metrics["incremental_stale_entities"] = sum(
            int(check["stale_entities"]) for check in incremental_checks
        )
        metrics["full_reindex_missing_entities"] = sum(
            int(check["missing_entities"]) for check in full_checks
        )
        metrics["incremental_missing_entities"] = sum(
            int(check["missing_entities"]) for check in incremental_checks
        )
    if not consistent:
        metrics["CORRECTNESS_FAILURE"] = True

    return BenchResult(
        "baseline_comparison",
        {
            "mode": mode,
            "n_docs": n,
            "k_changes": k,
            "k_deleted": deleted,
            "repetitions": repetitions,
        },
        metrics,
        notes=(
            "Both arms start from the same corpus. The baseline hard-deletes its "
            "temporary dataset and rebuilds every current document; cogindex applies "
            "the fixed, disjoint edit/delete set. Arm order alternates between repetitions."
        ),
    )
