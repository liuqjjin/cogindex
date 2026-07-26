"""PostgreSQL advisory-lock provider (ADR-0006).

For multiple updater processes sharing one Cognee deployment. Session-scoped
``pg_advisory_lock``: if the holding connection dies, PostgreSQL releases the
lock, so nothing has to sweep up stale ones.

Requires the ``postgres`` extra, which pulls in asyncpg.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from ._errors import CompatibilityError, LockTimeoutError
from ._locks import _validate_scope

__all__ = ["PostgresAdvisoryLockProvider"]

logger = logging.getLogger("cogindex.locks")


def advisory_lock_key(scope: str) -> int:
    """Map a scope string onto PostgreSQL's signed 64-bit advisory key space."""
    _validate_scope(scope)
    digest = hashlib.blake2b(scope.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class PostgresAdvisoryLockProvider:
    """Cross-process dataset locks backed by PostgreSQL advisory locks.

    Each acquisition opens a dedicated connection that is held for the lock's
    lifetime (session-scoped locks must outlive statement pooling). Scopes are
    hashed onto PostgreSQL's 64-bit advisory key space. A collision would add
    unnecessary serialization but would not permit overlapping writes.
    """

    __slots__ = ("_dsn", "_poll_interval", "_timeout")

    def __init__(
        self,
        dsn: str,
        *,
        timeout: float | None = 30.0,
        poll_interval: float = 0.2,
    ) -> None:
        if not isinstance(dsn, str):
            raise TypeError(f"dsn must be str, got {type(dsn).__name__}")
        if not dsn.strip() or "\x00" in dsn:
            raise ValueError("dsn must be a non-empty string without NUL characters")
        if timeout is not None and (
            isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number or None")
        if (
            isinstance(poll_interval, bool)
            or not math.isfinite(poll_interval)
            or poll_interval <= 0
        ):
            raise ValueError("poll_interval must be a finite positive number")
        self._dsn = dsn
        self._timeout = timeout
        self._poll_interval = poll_interval

    def lock(self, scope: str) -> AbstractAsyncContextManager[None]:
        _validate_scope(scope)
        return self._lock(scope)

    @asynccontextmanager
    async def _lock(self, scope: str) -> AsyncIterator[None]:
        asyncpg = _import_asyncpg()
        key = advisory_lock_key(scope)
        loop = asyncio.get_running_loop()
        deadline = None if self._timeout is None else loop.time() + self._timeout
        try:
            if deadline is None:
                conn = await asyncpg.connect(self._dsn)
            else:
                async with asyncio.timeout_at(deadline):
                    conn = await asyncpg.connect(self._dsn)
        except TimeoutError as exc:
            raise LockTimeoutError(
                f"timed out after {self._timeout}s connecting for advisory lock {scope!r}"
            ) from exc
        lock_acquired = False
        primary_error: BaseException | None = None
        try:
            try:
                if deadline is None:
                    await self._acquire(conn, key, scope, deadline)
                else:
                    async with asyncio.timeout_at(deadline):
                        await self._acquire(conn, key, scope, deadline)
            except TimeoutError as exc:
                raise LockTimeoutError(
                    f"timed out after {self._timeout}s acquiring advisory lock "
                    f"{scope!r} (key {key})"
                ) from exc
            lock_acquired = True
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            if lock_acquired:
                try:
                    await conn.execute("SELECT pg_advisory_unlock($1)", key)
                except BaseException as exc:
                    cleanup_error = exc
            try:
                await conn.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    logger.error(
                        "advisory-lock connection close also failed",
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                logger.error(
                    "advisory-lock cleanup failed while propagating primary error",
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )

    async def _acquire(
        self,
        conn: Any,
        key: int,
        scope: str,
        deadline: float | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        while True:
            acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
            if acquired:
                return
            if deadline is not None and loop.time() >= deadline:
                raise LockTimeoutError(
                    f"timed out after {self._timeout}s acquiring advisory lock "
                    f"{scope!r} (key {key})"
                )
            remaining = (
                self._poll_interval
                if deadline is None
                else min(self._poll_interval, max(0.0, deadline - loop.time()))
            )
            await asyncio.sleep(remaining)


def _import_asyncpg() -> Any:
    try:
        import asyncpg
    except ImportError as exc:
        raise CompatibilityError(
            "PostgresAdvisoryLockProvider requires asyncpg; "
            "reinstall with the 'postgres' extra to pull it in"
        ) from exc
    return asyncpg
