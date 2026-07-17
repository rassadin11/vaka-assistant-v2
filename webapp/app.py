"""FastAPI application factory for the Telegram Mini App backend."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from core.db import create_pool
from core.finance_summary import FinanceSummaryRedis
from core.llm import LLMProvider
from core.llm_openrouter import OpenRouterProvider, openrouter_settings_from_env
from core.llm_resilient import ResilientLLMProvider, ResilientRedis
from core.rate_limit import RateLimitRedis
from core.secrets import EnvSecretsProvider, SecretNotFoundError
from core.tracing import reset_trace_id, set_trace_id
from webapp.auth import InitDataValidationError, create_session_token, validate_init_data
from webapp.dependencies import (
    active_request_user,
    bearer_subject,
    require_webapp_rate_limit,
    resolve_active_user,
)
from webapp.errors import WebAppError, error_response
from webapp.metrics import WebAppMetrics, default_metrics
from webapp.routers.calendar import install_calendar_routes
from webapp.routers.finance import install_finance_routes
from webapp.settings import WebAppSettings, settings_from_env

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]
STATIC_DIRECTORY = Path(__file__).with_name("static")


class ImmutableStaticFiles(StaticFiles):
    """Serve content-hashed SPA assets with a browser-long immutable cache policy."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == status.HTTP_200_OK:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class ClosableRedis(RateLimitRedis, Protocol):
    """Redis operations required from a client owned by the app lifespan."""

    def ping(self) -> Awaitable[object]:
        """Ping Redis."""
        ...

    def aclose(self) -> Awaitable[None]:
        """Close the client."""
        ...


class ClosableQueueRedis(Protocol):
    """Queue Redis client owned by the webapp lifespan."""

    def ping(self) -> Awaitable[object]: ...

    def aclose(self) -> Awaitable[None]: ...


class AuthRequest(BaseModel):
    """The only accepted request shape for Telegram initData authentication."""

    model_config = ConfigDict(extra="forbid")

    init_data: str = Field(min_length=1, max_length=8_192)


class AuthResponse(BaseModel):
    """Signed bearer session returned after successful Telegram authentication."""

    token: str


class MeResponse(BaseModel):
    """Minimal bootstrap state permitted to leave the users table."""

    timezone: str
    plan: str


def create_app(
    *,
    settings: WebAppSettings | None = None,
    pool: asyncpg.Pool | None = None,
    cache_redis: ClosableRedis | None = None,
    queue_redis: ClosableQueueRedis | None = None,
    llm_provider: LLMProvider | None = None,
    clock: Clock | None = None,
    metrics: WebAppMetrics | None = None,
) -> FastAPI:
    """Create the stateless Mini App API with injectable infrastructure for tests."""

    app_settings = settings if settings is not None else settings_from_env()
    runtime_pool = pool
    runtime_cache_redis = cache_redis
    runtime_queue_redis = queue_redis
    runtime_llm_provider = llm_provider
    app_clock = clock if clock is not None else lambda: datetime.now(UTC)
    app_metrics = metrics if metrics is not None else default_metrics
    managed_pool: asyncpg.Pool | None = None
    managed_redis: ClosableRedis | None = None
    managed_queue_redis: ClosableQueueRedis | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal runtime_pool, runtime_cache_redis, runtime_queue_redis
        nonlocal runtime_llm_provider, managed_pool, managed_redis, managed_queue_redis
        if runtime_pool is None:
            managed_pool = await create_pool(app_settings.database_url)
            runtime_pool = managed_pool
        if runtime_cache_redis is None:
            redis_client = Redis.from_url(app_settings.redis_cache_url, decode_responses=True)
            managed_redis = cast(ClosableRedis, redis_client)
            runtime_cache_redis = managed_redis
        if runtime_queue_redis is None:
            queue_client = Redis.from_url(app_settings.redis_queue_url, decode_responses=True)
            managed_queue_redis = cast(ClosableQueueRedis, queue_client)
            runtime_queue_redis = managed_queue_redis
        if runtime_llm_provider is None:
            runtime_llm_provider = _finance_provider_from_env(_queue(runtime_queue_redis))
        try:
            yield
        finally:
            if managed_queue_redis is not None:
                await managed_queue_redis.aclose()
            if managed_redis is not None:
                await managed_redis.aclose()
            if managed_pool is not None:
                await managed_pool.close()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def trace_and_metrics(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        trace_id = str(uuid4())
        trace_token = set_trace_id(trace_id)
        started = time.perf_counter()
        response: Response
        try:
            content_length = request.headers.get("content-length")
            request_size = (
                int(content_length) if content_length is not None else len(await request.body())
            )
            if request_size > app_settings.max_request_body_bytes:
                response = error_response(413, "request_too_large", "Слишком большой запрос.")
            else:
                response = await call_next(request)
        except WebAppError as exc:
            response = error_response(exc.status_code, exc.code, exc.message)
        except ValueError:
            response = error_response(400, "invalid_request", "Некорректный запрос.")
        finally:
            route = _normalized_route(request)
            app_metrics.requests.labels(route=route, status=str(response.status_code)).inc()
            app_metrics.request_duration.labels(route=route).observe(time.perf_counter() - started)
            reset_trace_id(trace_token)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(WebAppError)
    async def webapp_error_handler(_request: Request, exc: WebAppError) -> Response:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(InitDataValidationError)
    async def init_data_error_handler(_request: Request, exc: InitDataValidationError) -> Response:
        app_metrics.auth_failures.labels(reason=exc.reason).inc()
        return error_response(
            401,
            "invalid_init_data",
            "Данные Telegram недействительны или устарели.",
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request,
        _exc: RequestValidationError,
    ) -> Response:
        return error_response(422, "invalid_request", "Некорректный запрос.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            return error_response(404, "not_found", "Маршрут не найден.")
        return error_response(exc.status_code, "http_error", "Некорректный запрос.")

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> Response:
        logger.error(
            "unhandled Mini App request failure",
            extra={"error_type": type(_exc).__name__},
            exc_info=(_exc.__class__, _exc, _exc.__traceback__),
        )
        return error_response(500, "internal_error", "Внутренняя ошибка сервиса.")

    @app.get("/app/healthz")
    async def healthz() -> Response:
        try:
            async with _pool(runtime_pool).acquire() as connection:
                await connection.fetchval("SELECT 1")
            await _cache(runtime_cache_redis).ping()
        except Exception:
            logger.warning("Mini App health check failed", exc_info=True)
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/app/metrics")
    async def metrics_endpoint() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/app/api/auth", response_model=AuthResponse)
    async def authenticate(payload: AuthRequest) -> AuthResponse:
        telegram_user_id = validate_init_data(
            payload.init_data, app_settings.telegram_bot_token, now=app_clock()
        )
        user_id = await resolve_active_user(_pool(runtime_pool), telegram_user_id)
        app_metrics.app_opened.inc()
        return AuthResponse(token=create_session_token(user_id, app_settings.session_secret))

    @app.get("/app/api/me", response_model=MeResponse)
    async def me(authorization: str | None = Header(default=None)) -> MeResponse:
        user_id = bearer_subject(authorization, app_settings.session_secret)
        await require_webapp_rate_limit(_cache(runtime_cache_redis), user_id, app_metrics)
        async with active_request_user(_pool(runtime_pool), user_id) as user:
            return MeResponse(timezone=user.timezone, plan=user.plan)

    install_calendar_routes(
        app,
        pool=lambda: _pool(runtime_pool),
        cache=lambda: _cache(runtime_cache_redis),
        session_secret=app_settings.session_secret,
        metrics=app_metrics,
        clock=app_clock,
    )
    install_finance_routes(
        app,
        pool=lambda: _pool(runtime_pool),
        cache=lambda: _cache(runtime_cache_redis),
        summary_cache=lambda: cast(FinanceSummaryRedis, _cache(runtime_cache_redis)),
        queue=lambda: _queue(runtime_queue_redis),
        provider=lambda: runtime_llm_provider,
        session_secret=app_settings.session_secret,
        metrics=app_metrics,
        clock=app_clock,
    )

    _install_spa_routes(app)

    return app


def _pool(pool: asyncpg.Pool | None) -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("database pool is not initialized")
    return pool


def _cache(redis: ClosableRedis | None) -> ClosableRedis:
    if redis is None:
        raise RuntimeError("cache Redis is not initialized")
    return redis


def _queue(redis: ClosableQueueRedis | None) -> ClosableQueueRedis:
    if redis is None:
        raise RuntimeError("queue Redis is not initialized")
    return redis


def _finance_provider_from_env(queue_redis: ClosableQueueRedis) -> LLMProvider | None:
    """Mirror worker OpenRouter resilience wiring when a key is resolvable."""

    try:
        api_key = EnvSecretsProvider().get("OPENROUTER_API_KEY")
    except SecretNotFoundError:
        logger.info("finance AI summary disabled: no OpenRouter key")
        return None
    if not api_key.strip():
        logger.info("finance AI summary disabled: empty OpenRouter key")
        return None
    settings = openrouter_settings_from_env()
    fallback_model = os.getenv("OPENROUTER_FALLBACK_MODEL", "").strip()
    fallback_provider = (
        OpenRouterProvider(replace(settings, model=fallback_model)) if fallback_model else None
    )
    return ResilientLLMProvider(
        OpenRouterProvider(settings),
        cast(ResilientRedis, queue_redis),
        settings.model,
        fallback_provider=fallback_provider,
        fallback_model=fallback_model or None,
    )


def _normalized_route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def _install_spa_routes(app: FastAPI) -> None:
    """Install the generated Mini App static assets and its GET-only SPA fallback."""

    if not STATIC_DIRECTORY.joinpath("index.html").is_file():
        return
    app.mount(
        "/app/assets",
        ImmutableStaticFiles(directory=STATIC_DIRECTORY / "assets"),
        name="assets",
    )

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> Response:
        if path.startswith("api/"):
            raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(
            STATIC_DIRECTORY / "index.html",
            headers={"Cache-Control": "no-cache"},
        )
