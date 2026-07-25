"""PostgresAdvisoryLockProvider against a real PostgreSQL (ADR-0006).

Verifies the cross-process guarantees the in-process provider cannot give:
mutual exclusion between independent providers (as two updater processes
would be), release on scope exit, and, the reason advisory locks were
chosen: automatic release when the holding session dies without unlocking.

Database resolution order: ``POSTGRES_DSN`` env var (CI service container),
then testcontainers (local Docker), else skip with the reason recorded.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

from cogindex import LockTimeoutError
from cogindex._locks_postgres import PostgresAdvisoryLockProvider, advisory_lock_key

asyncpg = pytest.importorskip("asyncpg", reason="postgres extra not installed")

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_dsn() -> Iterator[str]:
    env_dsn = os.environ.get("POSTGRES_DSN")
    if env_dsn:
        yield env_dsn
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("POSTGRES_DSN not set and testcontainers not installed")
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        pytest.skip(f"POSTGRES_DSN not set and Docker unavailable: {exc}")
    try:
        yield container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    finally:
        container.stop()


def _provider(dsn: str, *, timeout: float | None) -> PostgresAdvisoryLockProvider:
    return PostgresAdvisoryLockProvider(dsn, timeout=timeout, poll_interval=0.05)


async def test_mutual_exclusion_between_independent_providers(pg_dsn: str) -> None:
    """Two providers = two updater processes: the second times out while the
    first holds the scope."""
    holder = _provider(pg_dsn, timeout=5)
    contender = _provider(pg_dsn, timeout=0.3)
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with holder.lock("cogindex-it-scope"):
            held.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(held.wait(), timeout=10)
    try:
        with pytest.raises(LockTimeoutError):
            async with contender.lock("cogindex-it-scope"):
                pass
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)


async def test_release_on_exit_allows_reacquire(pg_dsn: str) -> None:
    provider_one = _provider(pg_dsn, timeout=5)
    provider_two = _provider(pg_dsn, timeout=5)
    async with provider_one.lock("cogindex-it-reacquire"):
        pass
    # Released on exit: a different provider acquires promptly.
    async with provider_two.lock("cogindex-it-reacquire"):
        pass


async def test_session_death_releases_lock_without_unlock(pg_dsn: str) -> None:
    """The crash-safety property: a session that never calls unlock releases
    its advisory locks when it ends."""
    scope = "cogindex-it-crash"
    key = advisory_lock_key(scope)
    dying = await asyncpg.connect(pg_dsn)
    try:
        acquired = await dying.fetchval("SELECT pg_try_advisory_lock($1)", key)
        assert acquired is True
    finally:
        # Session ends WITHOUT pg_advisory_unlock.
        await dying.close()

    survivor = _provider(pg_dsn, timeout=5)
    async with survivor.lock(scope):
        pass  # acquirable again, the dead session's lock was auto-released


async def test_distinct_scopes_do_not_contend(pg_dsn: str) -> None:
    provider = _provider(pg_dsn, timeout=1)
    entered_b = asyncio.Event()

    async def inner() -> None:
        async with provider.lock("cogindex-it-scope-b"):
            entered_b.set()

    async with provider.lock("cogindex-it-scope-a"):
        await asyncio.wait_for(inner(), timeout=10)
    assert entered_b.is_set()
