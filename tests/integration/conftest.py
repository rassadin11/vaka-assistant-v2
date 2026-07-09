"""Shared fixtures for infrastructure integration tests."""

from collections.abc import AsyncIterator

import asyncpg
import pytest

from .db_support import (
    ADMIN_DATABASE_URL,
    APP_DATABASE_URL,
    MIGRATOR_DATABASE_URL,
    ROLE_SQL_PATH,
    SERVICE_DATABASE_URL,
)


async def _connect_or_skip(database_url: str) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(database_url, timeout=2)
    except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
        pytest.skip(f"local dev database is not reachable: {exc}")


async def _pool_or_skip(database_url: str, *, min_size: int, max_size: int) -> asyncpg.Pool:
    try:
        return await asyncpg.create_pool(database_url, min_size=min_size, max_size=max_size)
    except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
        pytest.skip(f"local dev database is not reachable: {exc}")


@pytest.fixture(scope="session")
async def postgres_roles() -> None:
    """Apply idempotent dev roles to support already-initialized volumes."""

    connection = await _connect_or_skip(ADMIN_DATABASE_URL)
    advisory_lock_acquired = False
    try:
        await connection.execute("SELECT pg_advisory_lock(hashtext('assistant.init_roles'))")
        advisory_lock_acquired = True
        await connection.execute(ROLE_SQL_PATH.read_text(encoding="utf-8"))
    finally:
        if advisory_lock_acquired:
            await connection.execute("SELECT pg_advisory_unlock(hashtext('assistant.init_roles'))")
        await connection.close()


@pytest.fixture
async def app_pool(postgres_roles: None) -> AsyncIterator[asyncpg.Pool]:
    pool = await _pool_or_skip(APP_DATABASE_URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def service_pool(postgres_roles: None) -> AsyncIterator[asyncpg.Pool]:
    pool = await _pool_or_skip(SERVICE_DATABASE_URL, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def migrator_connection(postgres_roles: None) -> AsyncIterator[asyncpg.Connection]:
    connection = await _connect_or_skip(MIGRATOR_DATABASE_URL)
    try:
        yield connection
    finally:
        await connection.close()
