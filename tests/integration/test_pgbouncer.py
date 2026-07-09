"""PgBouncer smoke tests for asyncpg prepared statements."""

import asyncio

import asyncpg
import pytest

from .db_support import APP_DATABASE_URL

pytestmark = pytest.mark.integration


async def test_asyncpg_prepared_statements_work_through_pgbouncer(
    postgres_roles: None,
) -> None:
    try:
        pool = await asyncpg.create_pool(APP_DATABASE_URL, min_size=4, max_size=4)
    except (OSError, TimeoutError, asyncpg.CannotConnectNowError) as exc:
        pytest.skip(f"local dev PgBouncer is not reachable: {exc}")

    try:

        async def run_queries(worker_id: int) -> list[int]:
            async with pool.acquire() as connection:
                return [
                    await connection.fetchval(
                        "SELECT ($1::int * 1000) + $2::int",
                        worker_id,
                        index,
                    )
                    for index in range(50)
                ]

        results = await asyncio.gather(*(run_queries(worker_id) for worker_id in range(8)))
    finally:
        await pool.close()

    for worker_id, values in enumerate(results):
        assert values == [(worker_id * 1000) + index for index in range(50)]
