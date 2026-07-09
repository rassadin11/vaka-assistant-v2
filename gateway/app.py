"""Thin FastAPI Telegram gateway."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis

from core.envelope import UpdateEnvelope
from core.queue import QueueName, enqueue
from gateway.config import GatewayConfig, config_from_env

ALLOWED_UPDATES = ["message", "callback_query"]
DEDUP_TTL_SECONDS = 86_400

logger = logging.getLogger(__name__)


class QueueRedis(Protocol):
    """Subset of Redis queue methods used directly by the gateway app.

    redis-py async methods are sync ``def``s returning awaitables, so the
    protocol mirrors that shape instead of using ``async def``.
    """

    def ping(self) -> Awaitable[object]:
        """Ping Redis."""
        ...


class CacheRedis(Protocol):
    """Subset of Redis cache methods used by the gateway app."""

    def ping(self) -> Awaitable[object]:
        """Ping Redis."""
        ...

    def exists(self, name: str) -> Awaitable[int]:
        """Return whether a cache key exists."""
        ...

    def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = ...,
        nx: bool = ...,
    ) -> Awaitable[object]:
        """Set a cache key."""
        ...


class ClosableRedis(Protocol):
    """Redis clients created by the app lifespan can be closed."""

    def aclose(self) -> Awaitable[None]:
        """Close the Redis client."""
        ...


# First parameter is the queue Redis client; typed as Any because redis-py's
# own signatures are wider than any useful protocol here.
EnqueueFunc = Callable[[Any, QueueName, UpdateEnvelope], Awaitable[str]]


async def handle_update(
    update_dict: Mapping[str, Any],
    *,
    queue_redis: QueueRedis,
    cache_redis: CacheRedis,
    enqueue_func: EnqueueFunc = enqueue,
) -> None:
    """Process a Telegram update through the shared webhook/polling path."""

    envelope = _envelope_from_update(update_dict)
    if envelope is None:
        return

    dedup_key = f"dedup:{envelope.update_id}"
    if await cache_redis.exists(dedup_key):
        logger.debug("duplicate update ignored", extra={"update_id": envelope.update_id})
        return

    # TODO(stage-2.7): enforce per-user token bucket rate limit before enqueue.

    await enqueue_func(queue_redis, "interactive", envelope)
    await cache_redis.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)


def create_app(
    *,
    config: GatewayConfig | None = None,
    queue_redis: QueueRedis | None = None,
    cache_redis: CacheRedis | None = None,
    enqueue_func: EnqueueFunc = enqueue,
) -> FastAPI:
    """Create the FastAPI gateway app."""

    gateway_config = config if config is not None else config_from_env()
    managed_clients: list[ClosableRedis] = []
    runtime_queue_redis = queue_redis
    runtime_cache_redis = cache_redis

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal runtime_queue_redis, runtime_cache_redis
        if runtime_queue_redis is None:
            queue_client = Redis.from_url(gateway_config.redis.queue_url, decode_responses=True)
            runtime_queue_redis = cast(QueueRedis, queue_client)
            managed_clients.append(cast(ClosableRedis, queue_client))
        if runtime_cache_redis is None:
            cache_client = Redis.from_url(gateway_config.redis.cache_url, decode_responses=True)
            runtime_cache_redis = cast(CacheRedis, cache_client)
            managed_clients.append(cast(ClosableRedis, cache_client))
        try:
            yield
        finally:
            for client in managed_clients:
                await client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> Response:
        try:
            await _queue_client(runtime_queue_redis).ping()
            await _cache_client(runtime_cache_redis).ping()
        except Exception as exc:
            logger.warning("gateway health check failed: %s", exc)
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(status_code=status.HTTP_200_OK)

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse("# metrics placeholder for stage 6\n")

    @app.post("/webhook/{secret_path}")
    async def webhook(
        secret_path: str,
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        if not _webhook_auth_ok(
            secret_path=secret_path,
            header_token=x_telegram_bot_api_secret_token,
            config=gateway_config,
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
        await handle_update(
            payload,
            queue_redis=_queue_client(runtime_queue_redis),
            cache_redis=_cache_client(runtime_cache_redis),
            enqueue_func=enqueue_func,
        )
        return {"ok": True}

    return app


def _queue_client(client: QueueRedis | None) -> QueueRedis:
    if client is None:
        raise RuntimeError("queue Redis client is not initialized.")
    return client


def _cache_client(client: CacheRedis | None) -> CacheRedis:
    if client is None:
        raise RuntimeError("cache Redis client is not initialized.")
    return client


def _webhook_auth_ok(
    *,
    secret_path: str,
    header_token: str | None,
    config: GatewayConfig,
) -> bool:
    if header_token is None:
        return False
    return hmac.compare_digest(secret_path, config.webhook_secret_path) and hmac.compare_digest(
        header_token,
        config.telegram_webhook_secret_token,
    )


def _envelope_from_update(update_dict: Mapping[str, Any]) -> UpdateEnvelope | None:
    update_id = update_dict.get("update_id")
    if not isinstance(update_id, int):
        logger.debug("update without integer update_id ignored")
        return None

    message = update_dict.get("message")
    if isinstance(message, Mapping):
        return _message_envelope(update_id, message)

    callback_query = update_dict.get("callback_query")
    if isinstance(callback_query, Mapping):
        return _callback_envelope(update_id, callback_query)

    logger.debug("unsupported update kind ignored", extra={"update_id": update_id})
    return None


def _message_envelope(update_id: int, message: Mapping[str, Any]) -> UpdateEnvelope | None:
    chat = message.get("chat")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        logger.debug("non-private message ignored", extra={"update_id": update_id})
        return None

    user = message.get("from")
    user_id = _int_field(user, "id")
    chat_id = _int_field(chat, "id")
    if user_id is None or chat_id is None:
        logger.debug("message without user/chat id ignored", extra={"update_id": update_id})
        return None

    text = message.get("text")
    if isinstance(text, str):
        return UpdateEnvelope(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            kind="text",
            payload={"text": text},
        )

    voice = message.get("voice")
    if isinstance(voice, Mapping):
        payload = _file_payload(voice)
        if payload is None:
            return None
        duration = voice.get("duration")
        if isinstance(duration, int):
            payload["duration"] = duration
        return UpdateEnvelope(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            kind="voice",
            payload=payload,
        )

    document = message.get("document")
    if isinstance(document, Mapping):
        payload = _file_payload(document)
        if payload is None:
            return None
        file_name = document.get("file_name")
        mime_type = document.get("mime_type")
        if isinstance(file_name, str):
            payload["file_name"] = file_name
        if isinstance(mime_type, str):
            payload["mime_type"] = mime_type
        return UpdateEnvelope(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            kind="document",
            payload=payload,
        )

    logger.debug("unsupported message kind ignored", extra={"update_id": update_id})
    return None


def _callback_envelope(
    update_id: int,
    callback_query: Mapping[str, Any],
) -> UpdateEnvelope | None:
    message = callback_query.get("message")
    if not isinstance(message, Mapping):
        logger.debug("callback without chat message ignored", extra={"update_id": update_id})
        return None

    chat = message.get("chat")
    if not isinstance(chat, Mapping) or chat.get("type") != "private":
        logger.debug("non-private callback ignored", extra={"update_id": update_id})
        return None

    user = callback_query.get("from")
    user_id = _int_field(user, "id")
    chat_id = _int_field(chat, "id")
    message_id = _int_field(message, "message_id")
    data = callback_query.get("data")
    if user_id is None or chat_id is None or message_id is None or not isinstance(data, str):
        logger.debug("callback without required fields ignored", extra={"update_id": update_id})
        return None

    return UpdateEnvelope(
        update_id=update_id,
        user_id=user_id,
        chat_id=chat_id,
        kind="callback",
        payload={"data": data, "message_id": message_id},
    )


def _file_payload(file_info: Mapping[str, Any]) -> dict[str, Any] | None:
    file_id = file_info.get("file_id")
    if not isinstance(file_id, str):
        return None
    payload: dict[str, Any] = {"tg_file_id": file_id}
    file_size = file_info.get("file_size")
    if isinstance(file_size, int):
        payload["size"] = file_size
    return payload


def _int_field(value: object, field_name: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get(field_name)
    if isinstance(raw, int):
        return raw
    return None
