"""Unit coverage for the gateway's webhook management commands (S17)."""

from __future__ import annotations

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
