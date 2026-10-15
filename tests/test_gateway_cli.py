"""Unit coverage for the gateway CLI: webhook commands (S17), heartbeat (S9)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from gateway import __main__ as cli


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeWebhookInfo:
    def __init__(self, url: str, pending: int = 0) -> None:
        self.url = url
        self.pending_update_count = pending


class FakeBot:
    def __init__(self, token: str, *, url_after_delete: str = "") -> None:
        self.token = token
        self.session = FakeSession()
        self.delete_calls: list[bool] = []
        self._url_after_delete = url_after_delete

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        self.delete_calls.append(drop_pending_updates)
        return True

    async def get_webhook_info(self) -> FakeWebhookInfo:
        return FakeWebhookInfo(self._url_after_delete)


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> list[FakeBot]:
    created: list[FakeBot] = []

    def factory(token: str, **_kwargs: Any) -> FakeBot:
        bot = FakeBot(token)
        created.append(bot)
        return bot

    monkeypatch.setattr(cli, "Bot", factory)
    monkeypatch.setattr(cli, "telegram_bot_token", lambda: "111:test")
    return created


async def test_delete_webhook_keeps_pending_updates_by_default(fake_bot: list[FakeBot]) -> None:
    """Telegram holds what it could not deliver; dropping it loses real messages."""

    await cli._delete_webhook()

    assert fake_bot[0].delete_calls == [False]
    assert fake_bot[0].session.closed is True


async def test_delete_webhook_can_drop_pending_on_request(fake_bot: list[FakeBot]) -> None:
    await cli._delete_webhook(drop_pending=True)

    assert fake_bot[0].delete_calls == [True]


async def test_delete_webhook_fails_loudly_if_the_hook_is_still_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling would answer 409 forever; a silent success here would hide that."""

    def factory(token: str, **_kwargs: Any) -> FakeBot:
        return FakeBot(token, url_after_delete="https://example.com/webhook/x")

    monkeypatch.setattr(cli, "Bot", factory)
    monkeypatch.setattr(cli, "telegram_bot_token", lambda: "111:test")

    with pytest.raises(RuntimeError, match="still registered"):
        await cli._delete_webhook()


class HeartbeatBot:
    def __init__(self, *, url: str = "", fail: bool = False) -> None:
        self.url = url
        self.fail = fail
        self.calls = 0

    async def get_webhook_info(self) -> FakeWebhookInfo:
        self.calls += 1
        if self.fail:
            raise RuntimeError("telegram is unreachable")
        return FakeWebhookInfo(self.url)


class RecordingRedis:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int | None]] = []

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.writes.append((key, ex))


async def _run_one_heartbeat_cycle(bot: Any, redis: Any) -> None:
    """Run the endless heartbeat loop for exactly one iteration."""

    task = asyncio.create_task(cli._publish_polling_heartbeat(bot, redis))
    for _ in range(50):
        await asyncio.sleep(0)
        if bot.calls:
            break
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_heartbeat_is_written_while_polling_can_receive() -> None:
    bot = HeartbeatBot()
    redis = RecordingRedis()

    await _run_one_heartbeat_cycle(bot, redis)

    assert redis.writes == [(cli.POLLING_HEARTBEAT_KEY, cli.POLLING_HEARTBEAT_TTL_SECONDS)]


async def test_heartbeat_stops_while_a_webhook_is_still_registered() -> None:
    """getUpdates answers 409 in this state, so the container must go unhealthy."""

    bot = HeartbeatBot(url="https://example.com/webhook/x")
    redis = RecordingRedis()

    await _run_one_heartbeat_cycle(bot, redis)

    assert redis.writes == []


async def test_heartbeat_survives_a_failing_telegram_call() -> None:
    """The key expires on its own; the gateway must not crash on a hiccup."""

    bot = HeartbeatBot(fail=True)
    redis = RecordingRedis()

    await _run_one_heartbeat_cycle(bot, redis)

    assert redis.writes == []
    assert bot.calls == 1


async def test_polling_dispatcher_forwards_every_allowed_update_type() -> None:
    """Polling must deliver every allowed update to the shared path, intact.

    Regression for the first live run of the small profile: a handler on the raw
    update observer was shadowed by aiogram's own one ("not handled"), and once
    reached, aiogram field names (``from_user``) made every message sender-less.
    """

    from aiogram import Bot
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.types import Update

    from core.envelope import UpdateEnvelope
    from gateway.app import ALLOWED_UPDATES, _envelope_from_update

    user = {"id": 5, "is_bot": False, "first_name": "u"}
    chat = {"id": 5, "type": "private"}
    message = {"message_id": 1, "date": 0, "chat": chat, "from": user, "text": "hi"}
    samples = {
        "message": message,
        "callback_query": {
            "id": "c",
            "from": user,
            "chat_instance": "i",
            "message": message,
            "data": "noop",
        },
    }
    assert set(samples) == set(ALLOWED_UPDATES)

    received: list[dict[str, Any]] = []

    async def on_update(update_dict: dict[str, Any]) -> None:
        received.append(update_dict)

    dispatcher = cli.build_polling_dispatcher(on_update)
    bot = Bot("42:TEST")
    try:
        for update_id, update_type in enumerate(ALLOWED_UPDATES, start=1):
            payload = {"update_id": update_id, update_type: samples[update_type]}
            update = Update.model_validate(payload)
            assert await dispatcher.feed_update(bot, update) is not UNHANDLED
    finally:
        await bot.session.close()

    assert [update["update_id"] for update in received] == list(range(1, len(ALLOWED_UPDATES) + 1))
    # Bot API shape, not aiogram field names: the shared path must accept it.
    for update in received:
        assert isinstance(_envelope_from_update(update), UpdateEnvelope)
