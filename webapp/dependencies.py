"""Authentication, app-role database, and rate-limit dependencies for handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from core.db import user_transaction
from core.rate_limit import RateLimitRedis, allow_webapp_request
from webapp.auth import SessionValidationError, validate_session_token
from webapp.errors import WebAppError
from webapp.metrics import WebAppMetrics


@dataclass(frozen=True, slots=True)
class RequestUser:
    """Current active user state read inside the request's RLS transaction."""

    user_id: UUID
    timezone: str
    plan: str
    connection: Any


async def resolve_active_user(pool: asyncpg.Pool, telegram_user_id: int) -> UUID:
    """Resolve a validated Telegram id through the one permitted SECURITY DEFINER function."""

    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT user_id, status FROM public.webapp_resolve_user($1)", telegram_user_id
        )
    if row is None or row["status"] != "active":
        raise WebAppError(403, "access_denied", "Доступ к Mini App недоступен.")
    return UUID(str(row["user_id"]))


def bearer_subject(authorization: str | None, session_secret: str) -> UUID:
    """Parse a bearer authorization header into a signed session subject."""

    if authorization is None:
        raise WebAppError(401, "invalid_session", "Требуется авторизация.")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or " " in token:
        raise WebAppError(401, "invalid_session", "Требуется авторизация.")
    try:
        return validate_session_token(token, session_secret)
    except SessionValidationError as exc:
        raise WebAppError(401, "invalid_session", "Сессия недействительна или истекла.") from exc


async def require_webapp_rate_limit(
    redis: RateLimitRedis,
    user_id: UUID,
    metrics: WebAppMetrics,
) -> None:
    """Reject a protected request when its separate Redis bucket is empty."""

    try:
        allowed = await allow_webapp_request(redis, user_id)
    except Exception as exc:
        raise WebAppError(503, "dependency_unavailable", "Сервис временно недоступен.") from exc
    if not allowed:
        metrics.rate_limited.inc()
        raise WebAppError(429, "rate_limited", "Слишком много запросов. Попробуйте позже.")


@asynccontextmanager
async def active_request_user(
    pool: asyncpg.Pool,
    user_id: UUID,
) -> AsyncIterator[RequestUser]:
    """Open one app-role RLS transaction and refresh the user's current access state."""

    async with user_transaction(pool, user_id) as connection:
        row = await connection.fetchrow(
            "SELECT status, timezone, plan FROM users WHERE id = $1", user_id
        )
        if row is None or row["status"] != "active":
            raise WebAppError(403, "access_denied", "Доступ к Mini App недоступен.")
        timezone = row["timezone"]
        plan = row["plan"]
        if not isinstance(timezone, str) or not isinstance(plan, str):
            raise WebAppError(403, "access_denied", "Доступ к Mini App недоступен.")
        yield RequestUser(user_id=user_id, timezone=timezone, plan=plan, connection=connection)
