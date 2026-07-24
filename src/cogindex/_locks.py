"""Dataset lock providers (ADR-0006).

Locks serialize expensive per-dataset batch application (one cognify at a
time per dataset) and reduce wasted duplicate work across concurrent
updaters. Correctness never depends on them: the write protocol stays
convergent without any lock (ADR-0003).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, runtime_checkable

from ._errors import LockTimeoutError

__all__ = ["InProcessLockProvider", "LockProvider"]


@runtime_checkable
class LockProvider(Protocol):
    """Provides a mutual-exclusion scope keyed by an opaque string."""

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]: ...


class InProcessLockProvider:
    """Per-scope ``asyncio.Lock`` map — the default provider.

    Serializes work within one process/event loop only, which matches
    Cognee's own dataset locking (also process-local). Use
    :class:`cogindex.PostgresAdvisoryLockProvider` when multiple updater
    processes share one Cognee deployment.
    """

    __slots__ = ("_locks", "_timeout")

    def __init__(self, *, timeout: float | None = None) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._timeout = timeout

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]:
        return self._lock(scope)

    @asynccontextmanager
    async def _lock(self, scope: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(scope, asyncio.Lock())
        if self._timeout is None:
            await lock.acquire()
        else:
            try:
                async with asyncio.timeout(self._timeout):
                    await lock.acquire()
            except TimeoutError as exc:
                raise LockTimeoutError(
                    f"timed out after {self._timeout}s acquiring lock {scope!r}"
                ) from exc
        try:
            yield
        finally:
            lock.release()
