"""Lock providers, FakeCogneeRuntime upstream-faithfulness, secret-free logging.

Contracts under test:

- ``InProcessLockProvider`` serializes work per scope, times out with
  ``LockTimeoutError``, and never couples distinct scopes (ADR-0006).
- ``advisory_lock_key`` maps scopes deterministically onto PostgreSQL's
  signed 64-bit advisory key space (no database involved).
- ``FakeCogneeRuntime`` emulates the exact upstream Cognee semantics cogindex
  depends on: stale derivatives survive content re-adds, and the incremental
  cognify gate ignores configuration (the gap cogindex closes).
- ``DocumentHandler._apply`` logs batch summaries without document content.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import cast

import cocoindex as coco
import pytest

from cogindex import (
    CognifyProfile,
    DatasetHandle,
    DocumentPayload,
    InProcessLockProvider,
    LockTimeoutError,
)
from cogindex._identity import fingerprint_content
from cogindex._locks_postgres import advisory_lock_key
from cogindex._target import DocumentHandler, _DocumentAction
from cogindex.testing import FakeCogneeRuntime, FakeDocument, InjectedFault

TENANT = "default"
DATASET = "ds"


def _data_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cogindex-test-{name}")


def _handle() -> DatasetHandle:
    return DatasetHandle(name=DATASET, tenant=TENANT)


def _document(runtime: FakeCogneeRuntime, data_id: uuid.UUID) -> FakeDocument:
    document = runtime.document(TENANT, DATASET, data_id)
    assert document is not None
    return document


# =============================================================================
# Part A — InProcessLockProvider
# =============================================================================


async def test_lock_mutual_exclusion_serializes_one_scope() -> None:
    provider = InProcessLockProvider()
    order: list[str] = []
    first_inside = asyncio.Event()
    second_waiting = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with provider.lock("scope"):
            order.append("first-enter")
            first_inside.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_inside.wait()
        second_waiting.set()
        async with provider.lock("scope"):
            order.append("second-enter")

    task_first = asyncio.create_task(first())
    task_second = asyncio.create_task(second())
    await asyncio.wait_for(second_waiting.wait(), timeout=5)
    # Yield repeatedly: a broken lock would let `second` enter here.
    for _ in range(10):
        await asyncio.sleep(0)
    assert order == ["first-enter"]
    assert not task_second.done()

    release_first.set()
    await asyncio.wait_for(asyncio.gather(task_first, task_second), timeout=5)
    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_lock_timeout_raises_lock_timeout_error() -> None:
    provider = InProcessLockProvider(timeout=0.05)
    held = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with provider.lock("scope"):
            held.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await asyncio.wait_for(held.wait(), timeout=5)
    with pytest.raises(LockTimeoutError):
        async with provider.lock("scope"):
            pass
    release.set()
    await asyncio.wait_for(task, timeout=5)


async def test_lock_distinct_scopes_do_not_block_each_other() -> None:
    provider = InProcessLockProvider()
    a_inside = asyncio.Event()
    b_inside = asyncio.Event()

    async def hold_a() -> None:
        async with provider.lock("scope-a"):
            a_inside.set()
            # Deadlocks (and trips wait_for) if scope-b were coupled to scope-a.
            await b_inside.wait()

    async def hold_b() -> None:
        await a_inside.wait()
        async with provider.lock("scope-b"):
            b_inside.set()

    await asyncio.wait_for(asyncio.gather(hold_a(), hold_b()), timeout=5)


# =============================================================================
# Part B — advisory_lock_key (pure function; no database)
# =============================================================================


def test_advisory_lock_key_deterministic_distinct_and_in_range() -> None:
    scopes = ["", "a", "b", "scope", "scope2", "tenant/ds", "тема", "a" * 500]
    keys = [advisory_lock_key(scope) for scope in scopes]
    # Deterministic: same scope always maps to the same key.
    assert keys == [advisory_lock_key(scope) for scope in scopes]
    # Distinct scopes give distinct keys.
    assert len(set(keys)) == len(scopes)
    # Every key fits PostgreSQL's signed 64-bit advisory key space.
    for key in keys:
        assert -(2**63) <= key < 2**63


# =============================================================================
# Part C — FakeCogneeRuntime upstream-faithful semantics
# =============================================================================


async def test_add_then_cognify_completes_with_content_fingerprint() -> None:
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="hello world")]
    )
    await runtime.cognify_dataset(handle, CognifyProfile())
    document = _document(runtime, data_id)
    assert document.cognify_complete is True
    assert document.derived_fragments == {fingerprint_content("hello world")}
    assert document.derivatives_stale is False


async def test_readd_changed_content_keeps_stale_derivatives() -> None:
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="version one")]
    )
    await runtime.cognify_dataset(handle, CognifyProfile())
    old_fingerprint = fingerprint_content("version one")

    await runtime.add_documents(handle, [DocumentPayload(data_id=data_id, content="version two")])
    document = _document(runtime, data_id)
    # Upstream behavior: status resets, but old derivatives are NOT removed.
    assert document.cognify_complete is False
    assert document.derived_fragments == {old_fingerprint}
    assert document.derivatives_stale is True


async def test_cognify_without_purge_accumulates_orphaned_derivatives() -> None:
    """The upstream hazard ADR-0004's replace protocol closes: re-adding
    changed content and cognifying WITHOUT a purge leaves the old content's
    derivatives orphaned next to the new ones."""
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    profile = CognifyProfile()
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="version one")]
    )
    await runtime.cognify_dataset(handle, profile)

    await runtime.add_documents(handle, [DocumentPayload(data_id=data_id, content="version two")])
    await runtime.cognify_dataset(handle, profile)

    document = _document(runtime, data_id)
    assert document.cognify_complete is True
    assert document.derived_fragments == {
        fingerprint_content("version one"),
        fingerprint_content("version two"),
    }
    assert document.derivatives_stale is True
    assert runtime.unconverged_documents(TENANT, DATASET) == [data_id]


async def test_readd_same_content_different_label_keeps_complete() -> None:
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="same content", label="old")]
    )
    await runtime.cognify_dataset(handle, CognifyProfile())

    await runtime.add_documents(
        handle, [DocumentPayload(data_id=data_id, content="same content", label="new")]
    )
    document = _document(runtime, data_id)
    assert document.cognify_complete is True
    assert document.payload.label == "new"
    assert document.derivatives_stale is False


async def test_purge_document_memory_resets_then_cognify_rebuilds() -> None:
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    profile = CognifyProfile()
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="content")]
    )
    await runtime.cognify_dataset(handle, profile)

    await runtime.purge_document_memory(handle, [data_id])
    document = _document(runtime, data_id)
    assert document.derived_fragments == set()
    assert document.derived_profile is None
    assert document.cognify_complete is False

    await runtime.cognify_dataset(handle, profile)
    document = _document(runtime, data_id)
    assert document.cognify_complete is True
    assert document.derived_fragments == {fingerprint_content("content")}
    assert document.derived_profile == profile


async def test_cognify_gate_ignores_profile_change() -> None:
    runtime = FakeCogneeRuntime()
    data_id = _data_id("doc-1")
    old_profile = CognifyProfile()
    new_profile = CognifyProfile(chunk_size=64)
    assert old_profile != new_profile
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="content")]
    )
    await runtime.cognify_dataset(handle, old_profile)

    # Upstream incremental gate checks completion only, never configuration —
    # the already-complete document is skipped and keeps the OLD profile.
    await runtime.cognify_dataset(handle, new_profile)
    document = _document(runtime, data_id)
    assert document.cognify_complete is True
    assert document.derived_profile == old_profile
    assert runtime.unconverged_documents(TENANT, DATASET, profile=new_profile) == [data_id]
    assert runtime.unconverged_documents(TENANT, DATASET, profile=old_profile) == []


async def test_missing_targets_are_noops_and_teardown_keeps_dataset() -> None:
    runtime = FakeCogneeRuntime()
    missing_handle = DatasetHandle(name="never-created", tenant=TENANT)
    ghost_id = _data_id("ghost")

    # Missing dataset: all removal ops succeed as no-ops.
    await runtime.delete_documents(missing_handle, [ghost_id])
    await runtime.purge_document_memory(missing_handle, [ghost_id])
    await runtime.teardown_dataset(missing_handle)

    # Existing dataset, missing data_id: also a no-op.
    data_id = _data_id("doc-1")
    handle = await runtime.add_documents(
        _handle(), [DocumentPayload(data_id=data_id, content="content")]
    )
    await runtime.delete_documents(handle, [ghost_id])
    await runtime.purge_document_memory(handle, [ghost_id])
    assert runtime.document(TENANT, DATASET, data_id) is not None

    # Teardown empties documents but keeps the dataset entry.
    dataset_id_before = (await runtime.resolve_dataset(DATASET, TENANT)).dataset_id
    assert dataset_id_before is not None
    await runtime.teardown_dataset(handle)
    dataset = runtime.dataset(TENANT, DATASET)
    assert dataset is not None
    assert dataset.documents == {}
    resolved = await runtime.resolve_dataset(DATASET, TENANT)
    assert resolved.dataset_id == dataset_id_before


async def test_inject_fault_times_after_items_custom_exc_and_unknown_op() -> None:
    runtime = FakeCogneeRuntime()
    handle = _handle()

    # times=2: exactly the next two calls raise, the third succeeds.
    runtime.inject_fault("cognify_dataset", times=2)
    with pytest.raises(InjectedFault):
        await runtime.cognify_dataset(handle, CognifyProfile())
    with pytest.raises(InjectedFault):
        await runtime.cognify_dataset(handle, CognifyProfile())
    await runtime.cognify_dataset(handle, CognifyProfile())

    # after_items=1 on a 3-payload batch: first payload applied, then raise.
    payloads = [
        DocumentPayload(data_id=_data_id(f"doc-{i}"), content=f"content {i}") for i in range(3)
    ]
    runtime.inject_fault("add_documents", after_items=1)
    with pytest.raises(InjectedFault):
        await runtime.add_documents(handle, payloads)
    dataset = runtime.dataset(TENANT, DATASET)
    assert dataset is not None
    assert set(dataset.documents) == {payloads[0].data_id}
    assert ("add_documents", DATASET, (str(payloads[0].data_id),)) in runtime.calls

    # A custom exception instance is raised as-is.
    boom = RuntimeError("boom")
    runtime.inject_fault("delete_documents", exc=boom)
    with pytest.raises(RuntimeError) as excinfo:
        await runtime.delete_documents(handle, [payloads[0].data_id])
    assert excinfo.value is boom

    # Unknown op name is rejected immediately.
    with pytest.raises(ValueError, match="unknown op"):
        runtime.inject_fault("frobnicate")


# =============================================================================
# Part D — secret-free logging
# =============================================================================


async def test_apply_logs_never_contain_document_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "TOPSECRET-CONTENT-XYZ"
    runtime = FakeCogneeRuntime()
    handler = DocumentHandler(
        runtime=runtime,
        runtime_key="cognee",
        handle=_handle(),
        profile=CognifyProfile(),
        processing_fingerprint="test-processing-fingerprint",
    )
    data_id = _data_id("secret-doc")
    action = _DocumentAction(
        op="upsert",
        external_key="secret.md",
        data_id=data_id,
        stale_data_ids=(),
        payload=DocumentPayload(data_id=data_id, content=marker, label="secret"),
    )

    with caplog.at_level(logging.INFO):
        await handler._apply(cast(coco.ContextProvider, object()), [action])

    assert _document(runtime, data_id).payload.content == marker
    messages = [record.getMessage() for record in caplog.records]
    assert any("apply" in message for message in messages)
    assert all(marker not in message for message in messages)
    assert marker not in caplog.text
