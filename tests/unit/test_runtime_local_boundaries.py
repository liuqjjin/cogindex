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
from unittest.mock import AsyncMock, Mock

import pytest

import cogindex._compat as compat_module
import cogindex._runtime_local as runtime_module
from cogindex import CompatibilityError
from cogindex._identity import canonical_join, document_data_id
from cogindex._runtime import DatasetHandle, DocumentPayload
from cogindex._runtime_local import CogneePipelineError, LocalCogneeRuntime
from cogindex._spec import CognifyProfile


class PipelineRunCompleted(SimpleNamespace):
    pass


class _CompatHarness:
    def __init__(self) -> None:
        self.roots: tuple[str | None, str | None] = (None, None)
        self.configure_calls: list[tuple[str, str]] = []
        self.ensure_ready = AsyncMock()
        self.remote_mode = False
        self.default_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=None)
        self.resolve_default_user = AsyncMock(return_value=self.default_user)
        self.resolve_user = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, tenant_id=None)
        )
        self.list_datasets = AsyncMock(return_value=[])
        self.list_data = AsyncMock(return_value=[])

        def add_result(items: list[Any], dataset_name: str, **kwargs: Any) -> Any:
            dataset_id = kwargs.get("dataset_id", uuid.uuid4())
            return PipelineRunCompleted(
                dataset_id=dataset_id,
                dataset_name=dataset_name,
                data_ingestion_info=[
                    {
                        "run_info": PipelineRunCompleted(
                            dataset_id=dataset_id,
                            dataset_name=dataset_name,
                        ),
                        "data_id": item.data_id,
                    }
                    for item in items
                ],
            )

        def cognify_result(datasets: list[uuid.UUID], **kwargs: Any) -> Any:
            del kwargs
            dataset_id = datasets[0]
            return {
                dataset_id: PipelineRunCompleted(
                    dataset_id=dataset_id,
                    dataset_name="docs",
                    data_ingestion_info=[
                        {
                            "run_info": PipelineRunCompleted(
                                dataset_id=dataset_id,
                                dataset_name="docs",
                            )
                        }
                    ],
                )
            }

        self.add = AsyncMock(side_effect=add_result)
        self.cognify = AsyncMock(side_effect=cognify_result)
        self.validate_embedding_dimensions = Mock(return_value=3072)
        self.compat = SimpleNamespace(
            remote_mode_check=lambda: self.remote_mode,
            data_item_cls=lambda **kwargs: SimpleNamespace(**kwargs),
            cognee=SimpleNamespace(
                add=self.add,
                cognify=self.cognify,
                datasets=SimpleNamespace(
                    list_datasets=self.list_datasets,
                    list_data=self.list_data,
                ),
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
        "resolve_default_user",
        harness.resolve_default_user,
    )
    monkeypatch.setattr(compat_module, "resolve_user", harness.resolve_user)
    monkeypatch.setattr(
        compat_module,
        "validate_embedding_dimensions",
        harness.validate_embedding_dimensions,
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
    handle = await runtime.resolve_dataset("remote-rejected", "default")
    compat_harness.remote_mode = True

    with pytest.raises(CompatibilityError, match=r"await cognee\.disconnect"):
        await runtime._ensure_ready()

    payload = DocumentPayload(data_id=uuid.uuid4(), content="literal content")
    with pytest.raises(CompatibilityError, match="REST add endpoint"):
        await runtime.add_documents(
            handle,
            [payload],
        )

    compat_harness.ensure_ready.assert_awaited_once()


async def test_add_rejects_embedding_dimension_change_after_pipeline(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    dataset = _dataset("docs", compat_harness.default_user.id)
    compat_harness.list_datasets.return_value = [dataset]
    compat_harness.validate_embedding_dimensions.side_effect = [
        3072,
        CompatibilityError("embedding dimensions changed"),
    ]
    runtime = _runtime(tmp_path)
    handle = await runtime.resolve_dataset("docs", "default")

    with pytest.raises(CompatibilityError, match="dimensions changed"):
        await runtime.add_documents(
            handle,
            [DocumentPayload(data_id=uuid.uuid4(), content="literal content")],
        )

    compat_harness.add.assert_awaited_once()
    assert compat_harness.validate_embedding_dimensions.call_count == 2


async def test_cognify_rejects_embedding_dimension_change_after_pipeline(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    dataset = _dataset("docs", compat_harness.default_user.id)
    compat_harness.list_datasets.return_value = [dataset]
    compat_harness.validate_embedding_dimensions.side_effect = [
        3072,
        CompatibilityError("embedding dimensions changed"),
    ]
    runtime = _runtime(tmp_path)
    handle = await runtime.resolve_dataset("docs", "default")

    with pytest.raises(CompatibilityError, match="dimensions changed"):
        await runtime.cognify_dataset(handle, CognifyProfile())

    compat_harness.cognify.assert_awaited_once()
    assert compat_harness.validate_embedding_dimensions.call_count == 2


async def test_same_root_default_runtimes_share_dataset_lock(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    data_root, system_root = _roots(tmp_path)
    first = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    second = LocalCogneeRuntime(data_root=data_root, system_root=system_root)
    first_handle = await first.resolve_dataset("shared-lock", "default")
    second_handle = await second.resolve_dataset("shared-lock", "default")
    assert first_handle.identity_scope == second_handle.identity_scope
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first() -> None:
        async with first.dataset_lock(first_handle):
            first_entered.set()
            await release_first.wait()

    async def enter_second() -> None:
        await first_entered.wait()
        async with second.dataset_lock(second_handle):
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
    compat_harness.resolve_default_user.assert_not_awaited()
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


async def test_falsey_lock_provider_is_preserved(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    provider = _FalseyLockProvider()
    runtime = _runtime(tmp_path, lock_provider=provider)
    handle = await runtime.resolve_dataset("docs", "default")

    context = runtime.dataset_lock(handle)

    assert context is provider.context
    assert provider.scopes == [
        canonical_join(
            "cogindex",
            handle.identity_scope,
            "default",
            "docs",
        )
    ]


async def test_non_default_tenant_is_rejected_by_every_direct_operation(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    runtime = _runtime(tmp_path)
    handle = DatasetHandle(
        name="docs",
        tenant="other",
        identity_scope=str(uuid.uuid4()),
        dataset_id=uuid.uuid4(),
    )

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


def _user(*, tenant_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)


def _dataset(
    name: str,
    owner_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        owner_id=owner_id,
        tenant_id=tenant_id,
    )


@pytest.mark.parametrize("owned_first", [False, True])
async def test_resolve_dataset_ignores_shared_same_name_regardless_of_order(
    tmp_path: Path,
    compat_harness: _CompatHarness,
    owned_first: bool,
) -> None:
    user = _user()
    owned = _dataset("docs", user.id)
    shared = _dataset("docs", uuid.uuid4())
    compat_harness.list_datasets.return_value = [owned, shared] if owned_first else [shared, owned]
    runtime = _runtime(tmp_path, user=user)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id == owned.id
    assert handle.identity_scope == runtime_module._physical_identity_scope(user)
    resolved_user = compat_harness.resolve_user.await_args_list[0].args[0]
    assert resolved_user == user.id
    sdk_user = compat_harness.list_datasets.await_args_list[0].kwargs["user"]
    assert (sdk_user.id, sdk_user.tenant_id) == (user.id, user.tenant_id)


async def test_resolve_dataset_returns_missing_for_shared_only_same_name(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user = _user()
    compat_harness.list_datasets.return_value = [
        _dataset("docs", uuid.uuid4()),
        _dataset("other", user.id),
    ]
    runtime = _runtime(tmp_path, user=user)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id is None
    assert handle.identity_scope == runtime_module._physical_identity_scope(user)
    with pytest.raises(CogneePipelineError, match="without materializing"):
        await runtime.add_documents(
            handle,
            [DocumentPayload(data_id=uuid.uuid4(), content="literal content")],
        )
    compat_harness.add.assert_awaited_once()
    assert compat_harness.list_datasets.await_count == 2


async def test_resolve_dataset_rejects_duplicate_owned_same_name(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user = _user()
    compat_harness.list_datasets.return_value = [
        _dataset("docs", user.id),
        _dataset("docs", user.id),
    ]
    runtime = _runtime(tmp_path, user=user)

    with pytest.raises(RuntimeError, match="multiple Cognee datasets"):
        await runtime.resolve_dataset("docs", "default")


async def test_resolve_dataset_binds_and_passes_resolved_default_user(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user = _user(tenant_id=uuid.uuid4())
    owned = _dataset("docs", user.id, user.tenant_id)
    compat_harness.resolve_default_user.return_value = user
    compat_harness.list_datasets.return_value = [owned]
    runtime = _runtime(tmp_path)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id == owned.id
    assert handle.identity_scope == runtime_module._physical_identity_scope(user)
    compat_harness.resolve_default_user.assert_awaited_once_with()
    assert compat_harness.list_datasets.await_args_list[0].kwargs["user"] is user

    await runtime.list_documents(handle)

    assert compat_harness.list_data.await_count == 1
    assert compat_harness.list_data.await_args_list[0].kwargs["user"] is user


async def test_user_identity_separates_document_ids_and_lock_scopes(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    first_user = _user()
    second_user = _user()
    provider = _FalseyLockProvider()
    first = _runtime(tmp_path, user=first_user, lock_provider=provider)
    second = _runtime(tmp_path, user=second_user, lock_provider=provider)

    first_handle = await first.resolve_dataset("docs", "default")
    second_handle = await second.resolve_dataset("docs", "default")
    first.dataset_lock(first_handle)
    second.dataset_lock(second_handle)

    assert first_handle.identity_scope == runtime_module._physical_identity_scope(first_user)
    assert second_handle.identity_scope == runtime_module._physical_identity_scope(second_user)
    assert provider.scopes == [
        canonical_join("cogindex", first_handle.identity_scope, "default", "docs"),
        canonical_join("cogindex", second_handle.identity_scope, "default", "docs"),
    ]
    assert document_data_id(
        "runtime",
        first_handle.identity_scope,
        "default",
        "docs",
        "same.md",
    ) != document_data_id(
        "runtime",
        second_handle.identity_scope,
        "default",
        "docs",
        "same.md",
    )


async def test_rejects_handle_from_another_cognee_user(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    first_user = _user()
    second_user = _user()
    runtime = _runtime(tmp_path, user=first_user)
    await runtime.resolve_dataset("docs", "default")
    foreign_handle = DatasetHandle(
        name="docs",
        tenant="default",
        identity_scope=runtime_module._physical_identity_scope(second_user),
    )

    with pytest.raises(ValueError, match="does not match"):
        runtime.dataset_lock(foreign_handle)


async def test_resolve_dataset_fails_when_acting_user_id_is_unavailable(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    compat_harness.resolve_default_user.return_value = SimpleNamespace(
        id=None,
        tenant_id=None,
    )
    runtime = _runtime(tmp_path)

    with pytest.raises(RuntimeError, match="no UUID id"):
        await runtime.resolve_dataset("docs", "default")

    compat_harness.list_datasets.assert_not_awaited()


async def test_dataset_lookup_matches_owner_and_active_tenant(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    tenant = uuid.uuid4()
    other_tenant = uuid.uuid4()
    user = _user(tenant_id=tenant)
    in_active_tenant = _dataset("docs", user.id, tenant)
    same_owner_other_tenant = _dataset("docs", user.id, other_tenant)
    compat_harness.resolve_user.side_effect = None
    compat_harness.resolve_user.return_value = user
    compat_harness.list_datasets.return_value = [
        same_owner_other_tenant,
        in_active_tenant,
    ]
    runtime = _runtime(tmp_path, user=user)

    handle = await runtime.resolve_dataset("docs", "default")

    assert handle.dataset_id == in_active_tenant.id


async def test_same_user_different_tenants_separate_ids_and_lock_scopes(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user_id = uuid.uuid4()
    first_user = SimpleNamespace(id=user_id, tenant_id=uuid.uuid4())
    second_user = SimpleNamespace(id=user_id, tenant_id=uuid.uuid4())
    compat_harness.resolve_user.side_effect = [first_user, second_user]
    provider = _FalseyLockProvider()
    first = _runtime(tmp_path, user=first_user, lock_provider=provider)
    second = _runtime(tmp_path, user=second_user, lock_provider=provider)

    first_handle = await first.resolve_dataset("docs", "default")
    second_handle = await second.resolve_dataset("docs", "default")
    first.dataset_lock(first_handle)
    second.dataset_lock(second_handle)

    assert first_handle.identity_scope != second_handle.identity_scope
    assert provider.scopes == [
        canonical_join("cogindex", first_handle.identity_scope, "default", "docs"),
        canonical_join("cogindex", second_handle.identity_scope, "default", "docs"),
    ]
    assert document_data_id(
        "runtime",
        first_handle.identity_scope,
        "default",
        "docs",
        "same.md",
    ) != document_data_id(
        "runtime",
        second_handle.identity_scope,
        "default",
        "docs",
        "same.md",
    )


@pytest.mark.parametrize("drift_kind", ["user", "tenant"])
async def test_default_user_scope_drift_fails_before_sdk_operation(
    tmp_path: Path,
    compat_harness: _CompatHarness,
    drift_kind: str,
) -> None:
    user_id = uuid.uuid4()
    first = SimpleNamespace(id=user_id, tenant_id=uuid.uuid4())
    drifted = SimpleNamespace(
        id=uuid.uuid4() if drift_kind == "user" else user_id,
        tenant_id=first.tenant_id if drift_kind == "user" else uuid.uuid4(),
    )
    dataset = _dataset("docs", user_id, first.tenant_id)
    compat_harness.resolve_default_user.side_effect = [first, drifted]
    compat_harness.list_datasets.return_value = [dataset]
    runtime = _runtime(tmp_path)
    handle = await runtime.resolve_dataset("docs", "default")

    with pytest.raises(RuntimeError, match="changed after"):
        await runtime.list_documents(handle)

    compat_harness.list_data.assert_not_awaited()


async def test_explicit_user_tenant_drift_fails_before_sdk_operation(
    tmp_path: Path,
    compat_harness: _CompatHarness,
) -> None:
    user_id = uuid.uuid4()
    configured = SimpleNamespace(id=user_id, tenant_id=uuid.uuid4())
    drifted = SimpleNamespace(id=user_id, tenant_id=uuid.uuid4())
    dataset = _dataset("docs", user_id, configured.tenant_id)
    compat_harness.resolve_user.side_effect = [configured, drifted]
    compat_harness.list_datasets.return_value = [dataset]
    runtime = _runtime(tmp_path, user=configured)
    handle = await runtime.resolve_dataset("docs", "default")

    with pytest.raises(RuntimeError, match="active tenant changed"):
        await runtime.list_documents(handle)

    compat_harness.list_data.assert_not_awaited()
