"""Async PostgreSQL access helpers for PgBouncer transaction pooling."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import asyncpg
from asyncpg.pool import PoolConnectionProxy

DEFAULT_DATABASE_URL = "postgresql://app:dev-local-only@127.0.0.1:6432/assistant"
DEFAULT_SERVICE_DATABASE_URL = "postgresql://service:dev-local-only@127.0.0.1:6432/assistant"
DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 10


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Database connection settings loaded from environment variables."""

    database_url: str
    service_database_url: str
    pool_min_size: int
    pool_max_size: int


def database_settings_from_env() -> DatabaseSettings:
    """Load database settings with dev-only defaults for the local compose stack."""

    return DatabaseSettings(
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        service_database_url=os.getenv("SERVICE_DATABASE_URL", DEFAULT_SERVICE_DATABASE_URL),
        pool_min_size=int(os.getenv("DATABASE_POOL_MIN_SIZE", str(DEFAULT_POOL_MIN_SIZE))),
        pool_max_size=int(os.getenv("DATABASE_POOL_MAX_SIZE", str(DEFAULT_POOL_MAX_SIZE))),
    )


async def create_pool(
    database_url: str | None = None,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
) -> asyncpg.Pool:
    """Create an asyncpg pool for PgBouncer without disabling prepared statements."""

    settings = database_settings_from_env()
    return await asyncpg.create_pool(
        database_url or settings.database_url,
        min_size=min_size if min_size is not None else settings.pool_min_size,
        max_size=max_size if max_size is not None else settings.pool_max_size,
    )


async def create_service_pool(
    database_url: str | None = None,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
) -> asyncpg.Pool:
    """Create an asyncpg pool for cross-user service processes."""

    settings = database_settings_from_env()
    return await create_pool(
        database_url or settings.service_database_url,
        min_size=min_size,
        max_size=max_size,
    )


@asynccontextmanager
async def user_transaction(
    pool: asyncpg.Pool,
    user_id: UUID,
) -> AsyncIterator[PoolConnectionProxy[asyncpg.Record]]:
    """Open a transaction and bind the current user id with SET LOCAL semantics."""

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.user_id', $1, true)", str(user_id))
        yield connection


@asynccontextmanager
async def service_transaction(
    pool: asyncpg.Pool,
) -> AsyncIterator[PoolConnectionProxy[asyncpg.Record]]:
    """Open a transaction for trusted cross-user service work."""

    async with pool.acquire() as connection, connection.transaction():
        yield connection
