"""Unit tests for Telegram Mini App initData and session validation."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from itsdangerous import URLSafeTimedSerializer

from webapp.auth import (
    InitDataValidationError,
    SessionValidationError,
    create_session_token,
    validate_init_data,
    validate_session_token,
)

BOT_TOKEN = "test-bot-token"
NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


def _init_data(
    *,
    auth_date: int | None = None,
    user: str | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> str:
    pairs = [
        ("auth_date", str(int(NOW.timestamp()) if auth_date is None else auth_date)),
        ("query_id", "AAEAA"),
        ("user", user if user is not None else json.dumps({"id": 42})),
    ]
    if extra is not None:
        pairs.extend(extra)
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs.append(("hash", hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()))
    return urlencode(pairs)


def test_valid_init_data_returns_authenticated_telegram_user_id() -> None:
    assert validate_init_data(_init_data(), BOT_TOKEN, now=NOW) == 42


@pytest.mark.parametrize(
    "init_data",
    [
        "auth_date=1&user=%7B%22id%22%3A42%7D",
        "auth_date=1&hash=abc",
        "user=%7B%22id%22%3A42%7D&hash=abc",
        "auth_date=1&auth_date=1&user=%7B%22id%22%3A42%7D&hash=abc",
        "auth_date=1&user=not-json&hash=abc",
    ],
)
def test_missing_or_malformed_init_data_is_rejected(init_data: str) -> None:
    with pytest.raises(InitDataValidationError):
        validate_init_data(init_data, BOT_TOKEN, now=NOW)


def test_tampered_init_data_is_rejected() -> None:
    init_data = _init_data().replace("query_id=AAEAA", "query_id=BBBBB")
    with pytest.raises(InitDataValidationError, match="invalid_hash"):
        validate_init_data(init_data, BOT_TOKEN, now=NOW)


def test_expired_and_far_future_init_data_are_rejected() -> None:
    expired = _init_data(auth_date=int((NOW - timedelta(hours=1, seconds=1)).timestamp()))
    future = _init_data(auth_date=int((NOW + timedelta(seconds=61)).timestamp()))
    with pytest.raises(InitDataValidationError, match="expired_auth_date"):
        validate_init_data(expired, BOT_TOKEN, now=NOW)
    with pytest.raises(InitDataValidationError, match="future_auth_date"):
        validate_init_data(future, BOT_TOKEN, now=NOW)


def test_session_round_trip_rejects_tampering_wrong_version_and_expiration() -> None:
    user_id = uuid4()
    token = create_session_token(user_id, "session-secret")
    assert validate_session_token(token, "session-secret") == user_id

    with pytest.raises(SessionValidationError):
        validate_session_token(f"{token}x", "session-secret")
    with pytest.raises(SessionValidationError):
        validate_session_token(token, "session-secret", max_age_seconds=-1)
    wrong_version = URLSafeTimedSerializer(
        "session-secret", salt="telegram-webapp-session-v1"
    ).dumps({"v": 2, "sub": str(user_id)})
    with pytest.raises(SessionValidationError):
        validate_session_token(wrong_version, "session-secret")
