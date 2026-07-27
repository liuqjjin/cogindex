"""Boundary contracts for ``LocalCogneeRuntime`` without real databases."""

from __future__ import annotations

import asyncio
import gc
import uuid
import weakref
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import AsyncMock

import pytest

import cogindex._compat as compat_module
import cogindex._runtime_local as runtime_module
from cogindex import CompatibilityError
from cogindex._runtime import DatasetHandle, DocumentPayload
from cogindex._runtime_local import LocalCogneeRuntime
from cogindex._spec import CognifyProfile


class _CompatHarness:
    def __init__(self) -> None:
        self.roots: tuple[str | None, str | None] = (None, None)
        self.configure_calls: list[tuple[str, str]] = []
        self.ensure_ready = AsyncMock()
        self.remote_mode = False
        self.default_user_id = AsyncMock(return_value=uuid.uuid4())
        self.list_datasets = AsyncMock(return_value=[])
        self.compat = SimpleNamespace(
            remote_mode_check=lambda: self.remote_mode,
            cognee=SimpleNamespace(
                datasets=SimpleNamespace(list_datasets=self.list_datasets),
            ),
        )

    def load(self) -> Any:
        return self.compat

    def configure_storage(self, data_root: str, system_root: str) -> None:
        self.configure_calls.append((data_root, system_root))
        self.roots = data_root, system_root

    def storage_roots(self) -> tuple[str | None, str | None]:
        return self.roots


@pytest.fixture
def compat_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> _CompatHarness:
    harness = _CompatHarness()
    live_runtimes: weakref.WeakSet[LocalCogneeRuntime] = weakref.WeakSet()
    monkeypatch.setattr(runtime_module, "_LIVE_LOCAL_RUNTIMES", live_runtimes)
    monkeypatch.setattr(compat_module, "load", harness.load)
    monkeypatch.setattr(
        compat_module,
        "configure_storage",
        harness.configure_storage,
    )
    monkeypatch.setattr(compat_module, "storage_roots", harness.storage_roots)
    monkeypatch.setattr(
        compat_module,
        "ensure_databases_ready",
        harness.ensure_ready,
    )
    monkeypatch.setattr(
        compat_module,
        "default_user_id",
        harness.default_user_id,
    )
    return harness


def _roots(tmp_path: Path, label: str = "one") -> tuple[Path, Path]:
    return tmp_path / label / "data", tmp_path / label / "system"


def _runtime(
    tmp_path: Path,
    *,
    label: str = "one",
    user: Any | None = None,
    lock_provider: Any | None = None,
) -> LocalCogneeRuntime:
    data_root, system_root = _roots(tmp_path, label)
    return LocalCogneeRuntime(
        data_root=data_root,
        system_root=system_root,
        user=user,
        lock_provider=lock_provider,
    )


@pytest.mark.parametrize(
    ("data_root", "system_root"),
    [
        (None, None),
        ("data", None),
        (None, "system"),
    ],
)
def test_both_storage_roots_are_required(
    compat_harness: _CompatHarness,
    data_root: str | None,
    system_root: str | None,
) -> None:
    with pytest.raises(ValueError, match="must both be explicitly supplied"):
        LocalCogneeRuntime(data_root=data_root, system_root=system_root)

    assert compat_harness.configure_calls == []


@pytest.mark.parametrize(
    ("data_root", "system_root", "message"),
    [
        ("", "system", "data_root.*empty"),
        ("data", "", "system_root.*empty"),
        (" \t ", "system", "data_root.*whitespace"),
        ("data", "\n", "system_root.*whitespace"),
        ("bad\x00data", "system", "data_root.*NUL"),
        ("data", "bad\x00system", "system_root.*NUL"),
    ],
)
def test_invalid_storage_roots_are_rejected_before_configuration(
    compat_harness: _CompatHarness,
    data_root: str,
    system_root: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        LocalCogneeRuntime(data_root=data_root, system_root=system_root)

    assert compat_harness.configure_calls == []


def test_same_normalized_root_pair_allows_multiple_live_runtimes(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    data_root, system_root = _roots(tmp_path)
    first = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    second = LocalCogneeRuntime(
        data_root=str(data_root),
        system_root=str(system_root),
    )
    normalized = str(data_root.resolve()), str(system_root.resolve())

    assert first._storage_roots == normalized
    assert second._storage_roots == normalized
    assert compat_harness.configure_calls == [normalized]


async def test_local_mode_runs_setup_without_false_positive(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    runtime = _runtime(tmp_path)

    await runtime._ensure_ready()
    await runtime._ensure_ready()

    compat_harness.ensure_ready.assert_awaited_once()


async def test_remote_mode_enabled_after_setup_rejects_ready_and_add(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    runtime = _runtime(tmp_path)
    await runtime._ensure_ready()
    compat_harness.remote_mode = True

    with pytest.raises(CompatibilityError, match=r"await cognee\.disconnect"):
        await runtime._ensure_ready()

    payload = DocumentPayload(data_id=uuid.uuid4(), content="literal content")
    with pytest.raises(CompatibilityError, match="REST add endpoint"):
        await runtime.add_documents(
            DatasetHandle(name="remote-rejected", tenant="default"),
            [payload],
        )

    compat_harness.ensure_ready.assert_awaited_once()


async def test_same_root_default_runtimes_share_dataset_lock(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    del compat_harness
    data_root, system_root = _roots(tmp_path)
    first = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    second = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    handle = DatasetHandle(name="shared-lock", tenant="default")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first() -> None:
        async with first.dataset_lock(handle):
            first_entered.set()
            await release_first.wait()

    async def enter_second() -> None:
        await first_entered.wait()
        async with second.dataset_lock(handle):
            second_entered.set()

    first_task = asyncio.create_task(hold_first())
    second_task = asyncio.create_task(enter_second())
    await asyncio.wait_for(first_entered.wait(), timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=5)
    assert second_entered.is_set()


def test_different_root_pair_is_rejected_while_runtime_is_alive(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    first = _runtime(tmp_path, label="first")

    with pytest.raises(RuntimeError, match="process-global"):
        _runtime(tmp_path, label="second")

    assert first._storage_roots == tuple(str(path.resolve()) for path in _roots(tmp_path, "first"))
    assert len(compat_harness.configure_calls) == 1


def test_different_root_pair_is_allowed_after_previous_runtime_dies(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    first = _runtime(tmp_path, label="first")
    first_ref = weakref.ref(first)
    del first
    gc.collect()
    assert first_ref() is None

    second = _runtime(tmp_path, label="second")

    assert second._storage_roots == tuple(
        str(path.resolve()) for path in _roots(tmp_path, "second")
    )
    assert len(compat_harness.configure_calls) == 2


async def test_external_storage_root_mutation_fails_before_database_io(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    runtime = _runtime(tmp_path)
    other_data, other_system = _roots(tmp_path, "external")
    compat_harness.roots = str(other_data.resolve()), str(other_system.resolve())

    with pytest.raises(RuntimeError, match="process-global storage roots changed"):
        await runtime.resolve_dataset("docs", "default")

    compat_harness.ensure_ready.assert_not_awaited()
    compat_harness.default_user_id.assert_not_awaited()
    compat_harness.list_datasets.assert_not_awaited()


class _AsyncNullContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _FalseyLockProvider:
    def __init__(self) -> None:
        self.context = _AsyncNullContext()
        self.scopes: list[str] = []

    def __bool__(self) -> bool:
        return False

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]:
        self.scopes.append(scope)
        return self.context


def test_falsey_lock_provider_is_preserved(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    del compat_harness
    provider = _FalseyLockProvider()
    runtime = _runtime(tmp_path, lock_provider=provider)

    context = runtime.dataset_lock(DatasetHandle(name="docs", tenant="default"))

    assert context is provider.context
    assert provider.scopes == ["8:cogindex7:default4:docs"]


async def test_non_default_tenant_is_rejected_by_every_direct_operation(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    runtime = _runtime(tmp_path)
    handle = DatasetHandle(name="docs", tenant="other", dataset_id=uuid.uuid4())

    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.resolve_dataset("docs", "other")
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.add_documents(handle, [])
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.purge_document_memory(handle, [])
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.delete_documents(handle, [])
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.cognify_dataset(handle, CognifyProfile())
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.teardown_dataset(handle)
    with pytest.raises(ValueError, match="only tenant 'default'"):
        await runtime.list_documents(handle)
    with pytest.raises(ValueError, match="only tenant 'default'"):
        runtime.dataset_lock(handle)

    compat_harness.ensure_ready.assert_not_awaited()
    compat_harness.list_datasets.assert_not_awaited()


@pytest.mark.parametrize("name", ["", "bad\x00name"])
async def test_invalid_dataset_name_is_rejected_before_database_io(
    tmp_path: Path,
    compat_harness: _CompatHarness,
    name: str,
) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(ValueError):
        await runtime.resolve_dataset(name, "default")

    compat_harness.ensure_ready.assert_not_awaited()
    compat_harness.list_datasets.assert_not_awaited()


def _dataset(name: str, owner_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name=name, owner_id=owner_id)


@pytest.mark.parametrize("owned_first", [False, True])
async def test_resolve_dataset_ignores_shared_same_name_regardless_of_order(
    tmp_path: Path,
    compat_harness: _CompatHarness,
    owned_first: bool,
) -> None:
    user_id = uuid.uuid4()
    user = SimpleNamespace(id=user_id)
    owned = _dataset("docs", user_id)
    shared = _dataset("docs", uuid.uuid4())
    compat_harness.list_datasets.return_value = [owned, shared] if owned_first else [shared, owned]
    runtime = _runtime(tmp_path, user=user)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id == owned.id
    compat_harness.list_datasets.assert_awaited_once_with(user=user)


async def test_resolve_dataset_returns_missing_for_shared_only_same_name(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    compat_harness.list_datasets.return_value = [
        _dataset("docs", uuid.uuid4()),
        _dataset("other", user.id),
    ]
    runtime = _runtime(tmp_path, user=user)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id is None


async def test_resolve_dataset_rejects_duplicate_owned_same_name(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    compat_harness.list_datasets.return_value = [
        _dataset("docs", user.id),
        _dataset("docs", user.id),
    ]
    runtime = _runtime(tmp_path, user=user)

    with pytest.raises(RuntimeError, match="multiple Cognee datasets"):
        await runtime.resolve_dataset("docs", "default")


async def test_resolve_dataset_uses_resolved_default_user_id(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user_id = uuid.uuid4()
    owned = _dataset("docs", user_id)
    compat_harness.default_user_id.return_value = user_id
    compat_harness.list_datasets.return_value = [owned]
    runtime = _runtime(tmp_path)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id == owned.id
    compat_harness.default_user_id.assert_awaited_once_with()


async def test_resolve_dataset_fails_when_acting_user_id_is_unavailable(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    compat_harness.default_user_id.return_value = None
    runtime = _runtime(tmp_path)

    with pytest.raises(RuntimeError, match="cannot resolve"):
        await runtime.resolve_dataset("docs", "default")

    compat_harness.list_datasets.assert_not_awaited()
