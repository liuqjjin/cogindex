"""Dataset lock providers (ADR-0006).

Locks serialize per-dataset document batches and whole-dataset teardown.
Document actions remain safe to replay, but teardown must share this lock so
it cannot overlap a connector batch (ADR-0003/0006).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable

from ._errors import LockTimeoutError

__all__ = ["InProcessLockProvider", "LockProvider"]


@runtime_checkable
class LockProvider(Protocol):
    """Provides a mutual-exclusion scope keyed by an opaque string."""

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]: ...


def _validate_scope(scope: str) -> None:
    if not isinstance(scope, str):
        raise TypeError(f"lock scope must be str, got {type(scope).__name__}")
    if not scope:
        raise ValueError("lock scope must be non-empty")
    if "\x00" in scope:
        raise ValueError("lock scope must not contain NUL characters")


class InProcessLockProvider:
    """The default provider: one ``asyncio.Lock`` per scope.

    Serializes work within one process/event loop only, which matches
    Cognee's own dataset locking (also process-local). Use
    :class:`cogindex.PostgresAdvisoryLockProvider` when multiple updater
    processes share one Cognee deployment.
    """

    __slots__ = ("_locks", "_timeout")

    def __init__(self, *, timeout: float | None = None) -> None:
        if timeout is not None and (
            isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number or None")
        self._locks: dict[str, _LockEntry] = {}
        self._timeout = timeout

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]:
        _validate_scope(scope)
        return self._lock(scope)

    @asynccontextmanager
    async def _lock(self, scope: str) -> AsyncIterator[None]:
        entry = self._locks.setdefault(scope, _LockEntry())
        entry.users += 1
        try:
            if self._timeout is None:
                await entry.lock.acquire()
            else:
                try:
                    async with asyncio.timeout(self._timeout):
                        await entry.lock.acquire()
                except TimeoutError as exc:
                    raise LockTimeoutError(
                        f"timed out after {self._timeout}s acquiring lock {scope!r}"
                    ) from exc
            try:
                yield
            finally:
                entry.lock.release()
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(scope) is entry:
                del self._locks[scope]


class _LockEntry:
    """One lock plus the holders and waiters that still reference it."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0
