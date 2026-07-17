"""Unit tests for the gateway: filtering, dedup invariant, webhook auth, health."""

import asyncio
from typing import Any

import httpx
import pytest

from core.envelope import UpdateEnvelope
from core.queue import QueueName, RedisSettings
from gateway.app import UNSUPPORTED_MESSAGE_TEXT, create_app, handle_update
from gateway.config import GatewayConfig

SECRET_PATH = "test-path"
SECRET_TOKEN = "test-token"


def _config() -> GatewayConfig:
    return GatewayConfig(
        webhook_secret_path=SECRET_PATH,
        telegram_webhook_secret_token=SECRET_TOKEN,
        redis=RedisSettings(queue_url="redis://unused:1", cache_url="redis://unused:1"),
        port=8000,
        public_url=None,
        admin_ids=(),
        rate_limit_per_minute=20,
        rate_limit_burst=5,
    )


class FakeQueueRedis:
    def __init__(self, *, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping

    async def ping(self) -> object:
        if self.fail_ping:
            raise ConnectionError("queue redis down")
        return True


class FakeCacheRedis:
    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        rate_limit_results: list[bool] | None = None,
    ) -> None:
        self.existing = existing if existing is not None else set()
        self.rate_limit_results = rate_limit_results if rate_limit_results is not None else []
        self.set_calls: list[tuple[str, str, int | None, bool]] = []
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []

    async def ping(self) -> object:
        return True

    async def exists(self, name: str) -> int:
        return int(name in self.existing)

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object:
        self.set_calls.append((name, value, ex, nx))
        if nx and name in self.existing:
            return False
        self.existing.add(name)
        return True

    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        self.eval_calls.append((script, numkeys, keys_and_args))
        if self.rate_limit_results:
            return [int(self.rate_limit_results.pop(0)), 0]
        return [1, 0]


class RecordingEnqueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.envelopes: list[tuple[QueueName, UpdateEnvelope]] = []

    async def __call__(
        self,
        redis: Any,
        queue: QueueName,
        envelope: UpdateEnvelope,
    ) -> str:
        if self.fail:
            raise ConnectionError("enqueue failed")
        self.envelopes.append((queue, envelope))
        return "1-0"


class RecordingUserSender:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def _text_update(update_id: int = 1, chat_type: str = "private") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "from": {"id": 42, "is_bot": False, "first_name": "T"},
            "chat": {"id": 42, "type": chat_type},
            "date": 1783000000,
            "text": "hello",
        },
    }


def _document_update(update_id: int = 2, *, caption: str | None = None) -> dict[str, Any]:
    update = _text_update(update_id)
    message = update["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["document"] = {
        "file_id": "pdf-file",
        "file_size": 123,
        "file_name": "notes.pdf",
        "mime_type": "application/pdf",
    }
    if caption is not None:
        message["caption"] = caption
    return update


def _sticker_update(update_id: int = 3, chat_type: str = "private") -> dict[str, Any]:
    update = _text_update(update_id, chat_type)
    message = update["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["sticker"] = {"file_id": "sticker-file"}
    return update


def _photo_update(update_id: int = 4, *, caption: str | None = None) -> dict[str, Any]:
    update = _text_update(update_id)
    message = update["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["photo"] = [
        {"file_id": "small-photo", "file_size": 100},
        {"file_id": "large-photo", "file_size": 200},
    ]
    if caption is not None:
        message["caption"] = caption
    return update


async def test_text_update_enqueued_and_dedup_key_set_after() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis()
    await handle_update(
        _text_update(),
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
    )
    assert len(enqueue.envelopes) == 1
    queue, envelope = enqueue.envelopes[0]
    assert queue == "interactive"
    assert envelope.kind == "text"
    assert envelope.payload == {"text": "hello"}
    assert cache.set_calls == [("dedup:1", "1", 86_400, True)]


async def test_document_update_is_enqueued_to_background() -> None:
    enqueue = RecordingEnqueue()
    await handle_update(
        _document_update(),
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=enqueue,
    )

    assert enqueue.envelopes[0][0] == "background"
    assert enqueue.envelopes[0][1].kind == "document"


async def test_document_caption_is_preserved_in_payload() -> None:
    enqueue = RecordingEnqueue()
    await handle_update(
        _document_update(caption="проверь файл"),
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=enqueue,
    )

    assert enqueue.envelopes[0][1].payload["caption"] == "проверь файл"


async def test_photo_uses_largest_size_with_caption_and_interactive_queue() -> None:
    enqueue = RecordingEnqueue()
    await handle_update(
        _photo_update(caption="это чек"),
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=enqueue,
    )

    queue, envelope = enqueue.envelopes[0]
    assert queue == "interactive"
    assert envelope.kind == "photo"
    assert envelope.payload == {"tg_file_id": "large-photo", "size": 200, "caption": "это чек"}


async def test_dedup_key_not_set_when_enqueue_fails() -> None:
    # Invariant (stage-2.2): the dedup key is set only AFTER a successful
    # enqueue, so a crash in between leads to redelivery, not to loss.
    enqueue = RecordingEnqueue(fail=True)
    cache = FakeCacheRedis()
    with pytest.raises(ConnectionError):
        await handle_update(
            _text_update(),
            queue_redis=FakeQueueRedis(),
            cache_redis=cache,
            enqueue_func=enqueue,
        )
    assert cache.set_calls == []


async def test_rate_limited_update_is_dropped_warned_and_deduped() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis(rate_limit_results=[False])
    sender = RecordingUserSender()

    await handle_update(
        _text_update(update_id=20),
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
        send_user_message=sender,
    )
    await asyncio.sleep(0)

    assert enqueue.envelopes == []
    assert sender.messages == [
        (
            42,
            "Слишком много сообщений — сделайте небольшую паузу, я отвечу на уже отправленные.",
        )
    ]
    assert cache.set_calls == [
        ("rl:warned:42", "1", 60, True),
        ("dedup:20", "1", 86_400, True),
    ]


async def test_rate_limited_update_is_silent_when_warning_flag_exists() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis(existing={"rl:warned:42"}, rate_limit_results=[False])
    sender = RecordingUserSender()

    await handle_update(
        _text_update(update_id=21),
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
        send_user_message=sender,
    )
    await asyncio.sleep(0)

    assert enqueue.envelopes == []
    assert sender.messages == []
    assert cache.set_calls == [
        ("rl:warned:42", "1", 60, True),
        ("dedup:21", "1", 86_400, True),
    ]


async def test_callback_update_counts_against_rate_limit() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis(rate_limit_results=[False])
    sender = RecordingUserSender()
    update = {
        "update_id": 22,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 42, "is_bot": False, "first_name": "T"},
            "data": "tz:Europe/Moscow",
            "message": {
                "message_id": 11,
                "chat": {"id": 42, "type": "private"},
                "date": 1783000000,
            },
        },
    }

    await handle_update(
        update,
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
        send_user_message=sender,
    )
    await asyncio.sleep(0)

    assert enqueue.envelopes == []
    assert sender.messages


async def test_duplicate_update_skipped() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis(existing={"dedup:1"})
    await handle_update(
        _text_update(update_id=1),
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
    )
    assert enqueue.envelopes == []
    assert cache.set_calls == []


async def test_non_private_chat_dropped_before_enqueue() -> None:
    enqueue = RecordingEnqueue()
    cache = FakeCacheRedis()
    await handle_update(
        _text_update(chat_type="group"),
        queue_redis=FakeQueueRedis(),
        cache_redis=cache,
        enqueue_func=enqueue,
    )
    assert enqueue.envelopes == []
    assert cache.set_calls == []


async def test_unsupported_update_kind_dropped() -> None:
    enqueue = RecordingEnqueue()
    await handle_update(
        {"update_id": 5, "edited_message": {"chat": {"id": 1, "type": "private"}}},
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=enqueue,
    )
    assert enqueue.envelopes == []


async def test_callback_update_mapped() -> None:
    enqueue = RecordingEnqueue()
    update = {
        "update_id": 7,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 42, "is_bot": False, "first_name": "T"},
            "data": "tz:Europe/Moscow",
            "message": {
                "message_id": 11,
                "chat": {"id": 42, "type": "private"},
                "date": 1783000000,
            },
        },
    }
    await handle_update(
        update,
        queue_redis=FakeQueueRedis(),
        cache_redis=FakeCacheRedis(),
        enqueue_func=enqueue,
    )
    assert len(enqueue.envelopes) == 1
    _, envelope = enqueue.envelopes[0]
    assert envelope.kind == "callback"
    assert envelope.payload == {
        "data": "tz:Europe/Moscow",
        "message_id": 11,
        "callback_query_id": "cb1",
    }


def _client(
    *,
    queue_redis: FakeQueueRedis | None = None,
    cache_redis: FakeCacheRedis | None = None,
    enqueue: RecordingEnqueue | None = None,
    sender: RecordingUserSender | None = None,
) -> httpx.AsyncClient:
    app = create_app(
        config=_config(),
        queue_redis=queue_redis if queue_redis is not None else FakeQueueRedis(),
        cache_redis=cache_redis if cache_redis is not None else FakeCacheRedis(),
        enqueue_func=enqueue if enqueue is not None else RecordingEnqueue(),
        send_user_message=sender,
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://gateway.test")


async def test_webhook_valid_secret_accepted() -> None:
    enqueue = RecordingEnqueue()
    async with _client(enqueue=enqueue) as client:
        response = await client.post(
            f"/webhook/{SECRET_PATH}",
            json=_text_update(),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET_TOKEN},
        )
    assert response.status_code == 200
    assert len(enqueue.envelopes) == 1


async def test_webhook_unsupported_private_message_sends_deduplicated_stub() -> None:
    cache = FakeCacheRedis()
    sender = RecordingUserSender()
    async with _client(cache_redis=cache, sender=sender) as client:
        for _ in range(2):
            response = await client.post(
                f"/webhook/{SECRET_PATH}",
                json=_sticker_update(update_id=31),
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET_TOKEN},
            )
            assert response.status_code == 200

    assert sender.messages == [(42, UNSUPPORTED_MESSAGE_TEXT)]
    assert "dedup:31" in cache.existing


async def test_webhook_unsupported_group_message_does_not_send_stub() -> None:
    sender = RecordingUserSender()
    async with _client(sender=sender) as client:
        response = await client.post(
            f"/webhook/{SECRET_PATH}",
            json=_sticker_update(update_id=32, chat_type="group"),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET_TOKEN},
        )

    assert response.status_code == 200
    assert sender.messages == []


@pytest.mark.parametrize(
    ("path", "token"),
    [
        (SECRET_PATH, "wrong-token"),
        ("wrong-path", SECRET_TOKEN),
        (SECRET_PATH, None),
    ],
)
async def test_webhook_bad_auth_rejected(path: str, token: str | None) -> None:
    enqueue = RecordingEnqueue()
    headers = {} if token is None else {"X-Telegram-Bot-Api-Secret-Token": token}
    async with _client(enqueue=enqueue) as client:
        response = await client.post(f"/webhook/{path}", json=_text_update(), headers=headers)
    assert response.status_code == 403
    assert enqueue.envelopes == []


async def test_healthz_ok() -> None:
    async with _client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200


async def test_healthz_reports_redis_failure() -> None:
    async with _client(queue_redis=FakeQueueRedis(fail_ping=True)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
