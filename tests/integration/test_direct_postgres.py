"""RLS and pooling over a direct Postgres connection, with no PgBouncer (S6)."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest

from core.db import create_pool, service_transaction, user_transaction
from core.profile import DeploymentProfile, settings_from_env

from .db_support import APP_DATABASE_URL, SERVICE_DATABASE_URL

pytestmark = pytest.mark.integration

# The small profile drops PgBouncer, so the app talks to Postgres on its own
# port instead of the pooler's 6432.
DIRECT_APP_URL = APP_DATABASE_URL.replace(":6432/", ":5432/")
DIRECT_SERVICE_URL = SERVICE_DATABASE_URL.replace(":6432/", ":5432/")


async def test_set_local_isolates_users_without_pgbouncer(postgres_roles: None) -> None:
    """``SET LOCAL`` is transaction-scoped either way: the pooler is irrelevant to it."""

    app_pool = await asyncpg.create_pool(DIRECT_APP_URL, min_size=1, max_size=2)
    service_pool = await asyncpg.create_pool(DIRECT_SERVICE_URL, min_size=1, max_size=2)
    owner, stranger = uuid4(), uuid4()
    try:
        for user_id in (owner, stranger):
            chat_id = int(user_id.int % 1_000_000_000)
            async with service_transaction(service_pool) as connection:
                await connection.execute(
                    """
                    INSERT INTO users (
                        id, tg_user_id, tg_chat_id, status, timezone, plan,
                        created_at, updated_at
                    )
                    VALUES ($1, $2, $2, 'active', 'Europe/Moscow', 'trial', now(), now())
                    """,
                    user_id,
                    chat_id,
                )

        async with user_transaction(app_pool, owner) as connection:
            await connection.execute(
                """
                INSERT INTO notes (user_id, title, content, created_at, updated_at)
                VALUES ($1, 'Прямое подключение', 'запись владельца', now(), now())
                """,
                owner,
            )
            mine = await connection.fetchval("SELECT count(*) FROM notes")

        async with user_transaction(app_pool, stranger) as connection:
            theirs = await connection.fetchval("SELECT count(*) FROM notes")

        assert mine == 1
        assert theirs == 0
    finally:
        async with service_transaction(service_pool) as connection:
            await connection.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", [owner, stranger]
            )
        await app_pool.close()
        await service_pool.close()


async def test_app_user_id_does_not_leak_between_transactions(postgres_roles: None) -> None:
    """A reused pooled connection must not carry the previous user's binding."""

    app_pool = await asyncpg.create_pool(DIRECT_APP_URL, min_size=1, max_size=1)
    owner = uuid4()
    try:
        async with user_transaction(app_pool, owner) as connection:
            bound = await connection.fetchval("SELECT current_setting('app.user_id', true)")

        # Same single pooled connection, no user_transaction wrapper this time.
        async with app_pool.acquire() as connection:
            leaked = await connection.fetchval("SELECT current_setting('app.user_id', true)")

        assert bound == str(owner)
        assert leaked in (None, "")
    finally:
        await app_pool.close()


async def test_pool_ceiling_fits_the_small_profile_connection_budget(
    postgres_roles: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two of these pools must still leave connections for alembic and psql."""

    monkeypatch.setenv("DEPLOYMENT_PROFILE", DeploymentProfile.SMALL.value)
    monkeypatch.setenv("DATABASE_URL", DIRECT_APP_URL)
    ceiling = settings_from_env().database_pool_max_size

    pool = await create_pool()
    try:
        assert pool.get_max_size() == ceiling
        assert ceiling * 2 < 20
    finally:
        await pool.close()
