"""Thin FastAPI Telegram gateway."""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

from aiogram import Bot
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from redis.asyncio import Redis

from core.envelope import UpdateEnvelope
from core.queue import QueueName, enqueue
from core.rate_limit import (
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    RedisEvalArg,
    allow_user_update,
)
from core.telegram_sender import TelegramSender
from gateway.config import GatewayConfig, config_from_env, optional_telegram_bot_token

ALLOWED_UPDATES = ["message", "callback_query"]
DEDUP_TTL_SECONDS = 86_400
RATE_LIMIT_WARNING_TTL_SECONDS = 60
RATE_LIMIT_WARNING_TEXT = (
    "Слишком много сообщений — сделайте небольшую паузу, я отвечу на уже отправленные."
)

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

    def eval(self, script: str, numkeys: int, *keys_and_args: RedisEvalArg) -> Awaitable[object]:
        """Evaluate a Lua script."""
        ...


class ClosableRedis(Protocol):
    """Redis clients created by the app lifespan can be closed."""

    def aclose(self) -> Awaitable[None]:
        """Close the Redis client."""
        ...


# First parameter is the queue Redis client; typed as Any because redis-py's
# own signatures are wider than any useful protocol here.
EnqueueFunc = Callable[[Any, QueueName, UpdateEnvelope], Awaitable[str]]
SendUserMessage = Callable[[int, str], Awaitable[None]]


async def handle_update(
    update_dict: Mapping[str, Any],
    *,
    queue_redis: QueueRedis,
    cache_redis: CacheRedis,
    enqueue_func: EnqueueFunc = enqueue,
    send_user_message: SendUserMessage | None = None,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    rate_limit_burst: int = DEFAULT_RATE_LIMIT_BURST,
) -> None:
    """Process a Telegram update through the shared webhook/polling path."""

    envelope = _envelope_from_update(update_dict)
    if envelope is None:
        return

    dedup_key = f"dedup:{envelope.update_id}"
    if await cache_redis.exists(dedup_key):
        logger.debug("duplicate update ignored", extra={"update_id": envelope.update_id})
        return

    allowed = await allow_user_update(
        cache_redis,
        envelope.user_id,
        per_minute=rate_limit_per_minute,
        burst=rate_limit_burst,
    )
    if not allowed:
        await _handle_rate_limited_update(
            cache_redis,
            envelope,
            dedup_key=dedup_key,
            send_user_message=send_user_message,
        )
        return

    queue: QueueName = "background" if envelope.kind == "document" else "interactive"
    await enqueue_func(queue_redis, queue, envelope)
    await cache_redis.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)


def create_app(
    *,
    config: GatewayConfig | None = None,
    queue_redis: QueueRedis | None = None,
    cache_redis: CacheRedis | None = None,
    enqueue_func: EnqueueFunc = enqueue,
    send_user_message: SendUserMessage | None = None,
) -> FastAPI:
    """Create the FastAPI gateway app."""

    gateway_config = config if config is not None else config_from_env()
    managed_clients: list[ClosableRedis] = []
    managed_bots: list[Bot] = []
    runtime_queue_redis = queue_redis
    runtime_cache_redis = cache_redis
    runtime_send_user_message = send_user_message

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal runtime_queue_redis, runtime_cache_redis, runtime_send_user_message
        if runtime_queue_redis is None:
            queue_client = Redis.from_url(gateway_config.redis.queue_url, decode_responses=True)
            runtime_queue_redis = cast(QueueRedis, queue_client)
            managed_clients.append(cast(ClosableRedis, queue_client))
        if runtime_cache_redis is None:
            cache_client = Redis.from_url(gateway_config.redis.cache_url, decode_responses=True)
            runtime_cache_redis = cast(CacheRedis, cache_client)
            managed_clients.append(cast(ClosableRedis, cache_client))
        if runtime_send_user_message is None:
            token = optional_telegram_bot_token()
            if token is not None:
                bot = Bot(token)
                sender = TelegramSender(bot, admin_chat_ids=gateway_config.admin_ids)
                runtime_send_user_message = sender.send_message
                managed_bots.append(bot)
        try:
            yield
        finally:
            for client in managed_clients:
                await client.aclose()
            for bot in managed_bots:
                await bot.session.close()

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
            send_user_message=runtime_send_user_message,
            rate_limit_per_minute=gateway_config.rate_limit_per_minute,
            rate_limit_burst=gateway_config.rate_limit_burst,
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


async def _handle_rate_limited_update(
    cache_redis: CacheRedis,
    envelope: UpdateEnvelope,
    *,
    dedup_key: str,
    send_user_message: SendUserMessage | None,
) -> None:
    warned_key = f"rl:warned:{envelope.user_id}"
    should_warn = await cache_redis.set(
        warned_key,
        "1",
        nx=True,
        ex=RATE_LIMIT_WARNING_TTL_SECONDS,
    )
    if bool(should_warn):
        if send_user_message is None:
            logger.info(
                "rate limit warning skipped because Telegram sender is unavailable",
                extra={"update_id": envelope.update_id, "user_id": envelope.user_id},
            )
        else:
            task = asyncio.create_task(
                _send_user_message_safely(
                    send_user_message,
                    envelope.chat_id,
                    RATE_LIMIT_WARNING_TEXT,
                )
            )
            task.add_done_callback(_log_background_task_error)
    await cache_redis.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)


async def _send_user_message_safely(
    send_user_message: SendUserMessage,
    chat_id: int,
    text: str,
) -> None:
    try:
        await send_user_message(chat_id, text)
    except Exception:
        logger.exception("failed to send rate limit warning", extra={"chat_id": chat_id})


def _log_background_task_error(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("rate limit warning task failed")


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
    callback_query_id = callback_query.get("id")
    data = callback_query.get("data")
    if (
        user_id is None
        or chat_id is None
        or message_id is None
        or not isinstance(callback_query_id, str)
        or not isinstance(data, str)
    ):
        logger.debug("callback without required fields ignored", extra={"update_id": update_id})
        return None

    return UpdateEnvelope(
        update_id=update_id,
        user_id=user_id,
        chat_id=chat_id,
        kind="callback",
        payload={"data": data, "message_id": message_id, "callback_query_id": callback_query_id},
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
