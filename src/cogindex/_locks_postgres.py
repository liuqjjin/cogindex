"""PostgreSQL advisory-lock provider (ADR-0006).

For multiple updater processes sharing one Cognee deployment. Session-scoped
``pg_advisory_lock``: if the holding connection dies, PostgreSQL releases the
lock, so nothing has to sweep up stale ones.

Requires the ``postgres`` extra, which pulls in asyncpg.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from ._errors import CompatibilityError, LockTimeoutError

__all__ = ["PostgresAdvisoryLockProvider"]


def advisory_lock_key(scope: str) -> int:
    """Map a scope string onto PostgreSQL's signed 64-bit advisory key space."""
    digest = hashlib.blake2b(scope.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class PostgresAdvisoryLockProvider:
    """Cross-process dataset locks backed by PostgreSQL advisory locks.

    Each acquisition opens a dedicated connection that is held for the lock's
    lifetime (session-scoped locks must outlive statement pooling). Scopes are
    hashed onto the 64-bit advisory key space; the ~1e-19 collision odds for
    realistic dataset counts would only over-serialize, never corrupt.
    """

    __slots__ = ("_dsn", "_poll_interval", "_timeout")

    def __init__(
        self,
        dsn: str,
        *,
        timeout: float | None = 30.0,
        poll_interval: float = 0.2,
    ) -> None:
        self._dsn = dsn
        self._timeout = timeout
        self._poll_interval = poll_interval

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]:
        return self._lock(scope)

    @asynccontextmanager
    async def _lock(self, scope: str) -> AsyncIterator[None]:
        asyncpg = _import_asyncpg()
        key = advisory_lock_key(scope)
        conn = await asyncpg.connect(self._dsn)
        try:
            await self._acquire(conn, key, scope)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
        finally:
            await conn.close()

    async def _acquire(self, conn: Any, key: int, scope: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = None if self._timeout is None else loop.time() + self._timeout
        while True:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
            if acquired:
                return
            if deadline is not None and loop.time() >= deadline:
                raise LockTimeoutError(
                    f"timed out after {self._timeout}s acquiring advisory lock "
                    f"{scope!r} (key {key})"
                )
            await asyncio.sleep(self._poll_interval)


def _import_asyncpg() -> Any:
    try:
        import asyncpg
    except ImportError as exc:
        raise CompatibilityError(
            "PostgresAdvisoryLockProvider requires asyncpg; "
            "reinstall with the 'postgres' extra to pull it in"
        ) from exc
    return asyncpg
