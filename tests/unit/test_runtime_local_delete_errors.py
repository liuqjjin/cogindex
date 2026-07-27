"""Exception boundaries for idempotent local-runtime deletion."""

from __future__ import annotations

import contextlib
import importlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from cogindex import _compat
from cogindex import _runtime_local as runtime_module
from cogindex._runtime import DatasetHandle
from cogindex._runtime_local import LocalCogneeRuntime

DeleteOperation = Literal["delete", "purge", "teardown"]


class _CogneeThatRaises:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def forget(self, **kwargs: object) -> None:
        raise self._error


def _upstream_error_types() -> tuple[type[BaseException], type[BaseException]]:
    module = importlib.import_module("cognee.modules.data.exceptions")
    dataset_not_found = cast(type[BaseException], module.DatasetNotFoundError)
    unauthorized = cast(type[BaseException], module.UnauthorizedDataAccessError)
    return dataset_not_found, unauthorized


@contextlib.asynccontextmanager
async def _noop_dataset_context(*args: object) -> AsyncIterator[None]:
    yield


def _runtime_with_forget_error(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> tuple[LocalCogneeRuntime, str]:
    missing_errors = _compat.load().dataset_missing_errors
    test_root = Path("/tmp/cogindex-runtime-delete-tests")
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=None)
    runtime = LocalCogneeRuntime(
        data_root=test_root / "data",
        system_root=test_root / "system",
        user=user,
    )
    identity_scope = runtime_module._physical_identity_scope(user)
    runtime._resolved_identity_scope = identity_scope
    compat_info = SimpleNamespace(
        cognee=_CogneeThatRaises(error),
        dataset_missing_errors=missing_errors,
        remote_mode_check=lambda: False,
    )
    monkeypatch.setattr(_compat, "load", lambda: compat_info)

    async def ensure_ready() -> None:
        return None

    monkeypatch.setattr(_compat, "ensure_databases_ready", ensure_ready)
    monkeypatch.setattr(_compat, "dataset_database_context", _noop_dataset_context)

    async def resolve_user(user_id: uuid.UUID) -> SimpleNamespace:
        assert user_id == user.id
        return user

    monkeypatch.setattr(_compat, "resolve_user", resolve_user)
    return runtime, identity_scope


async def _delete(
    runtime: LocalCogneeRuntime,
    operation: DeleteOperation,
    handle: DatasetHandle,
    data_id: uuid.UUID,
) -> None:
    actions: dict[DeleteOperation, Callable[[], Awaitable[None]]] = {
        "delete": lambda: runtime.delete_documents(handle, [data_id]),
        "purge": lambda: runtime.purge_document_memory(handle, [data_id]),
        "teardown": lambda: runtime.teardown_dataset(handle),
    }
    await actions[operation]()


@pytest.mark.parametrize("operation", ["delete", "purge", "teardown"])
async def test_explicit_dataset_not_found_is_an_idempotent_noop(
    monkeypatch: pytest.MonkeyPatch, operation: DeleteOperation
) -> None:
    dataset_not_found, _ = _upstream_error_types()
    runtime, identity_scope = _runtime_with_forget_error(
        monkeypatch,
        dataset_not_found("gone"),
    )

    await _delete(
        runtime,
        operation,
        DatasetHandle(
            name="docs",
            tenant="default",
            identity_scope=identity_scope,
            dataset_id=uuid.uuid4(),
        ),
        uuid.uuid4(),
    )


@pytest.mark.parametrize("operation", ["delete", "purge", "teardown"])
@pytest.mark.parametrize("error_kind", ["unauthorized", "value-error"])
async def test_ambiguous_or_unauthorized_delete_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
    operation: DeleteOperation,
    error_kind: Literal["unauthorized", "value-error"],
) -> None:
    _, unauthorized = _upstream_error_types()
    error = (
        unauthorized("denied")
        if error_kind == "unauthorized"
        else ValueError("not found or not accessible")
    )
    runtime, identity_scope = _runtime_with_forget_error(monkeypatch, error)

    with pytest.raises(type(error)) as exc_info:
        await _delete(
            runtime,
            operation,
            DatasetHandle(
                name="docs",
                tenant="default",
                identity_scope=identity_scope,
                dataset_id=uuid.uuid4(),
            ),
            uuid.uuid4(),
        )
    assert exc_info.value is error
