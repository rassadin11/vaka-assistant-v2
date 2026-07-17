"""Pure Telegram initData validation and signed Mini App session helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

INIT_DATA_MAX_AGE_SECONDS = 60 * 60
INIT_DATA_MAX_FUTURE_SKEW_SECONDS = 60
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
SESSION_SALT = "telegram-webapp-session-v1"
SESSION_VERSION = 1
_REQUIRED_INIT_DATA_FIELDS = frozenset({"hash", "auth_date", "user"})


class InitDataValidationError(ValueError):
    """Raised when Telegram initData cannot establish a trusted identity."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SessionValidationError(ValueError):
    """Raised when a Mini App bearer session is malformed, expired, or invalid."""


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: datetime | None = None,
) -> int:
    """Validate Telegram initData and return only its authenticated Telegram user id."""

    pairs = _parse_init_data(init_data)
    values = _required_values(pairs)
    expected_hash = _telegram_hash(pairs, bot_token)
    if not hmac.compare_digest(values["hash"], expected_hash):
        raise InitDataValidationError("invalid_hash")

    auth_date = _parse_auth_date(values["auth_date"])
    current_time = datetime.now(UTC) if now is None else _as_utc(now)
    age_seconds = (current_time - auth_date).total_seconds()
    if age_seconds < -INIT_DATA_MAX_FUTURE_SKEW_SECONDS:
        raise InitDataValidationError("future_auth_date")
    if age_seconds > INIT_DATA_MAX_AGE_SECONDS:
        raise InitDataValidationError("expired_auth_date")

    return _telegram_user_id(values["user"])


def create_session_token(user_id: UUID, session_secret: str) -> str:
    """Create a minimal signed bearer token for one internal user UUID."""

    serializer = URLSafeTimedSerializer(session_secret, salt=SESSION_SALT)
    return serializer.dumps({"v": SESSION_VERSION, "sub": str(user_id)})


def validate_session_token(
    token: str,
    session_secret: str,
    *,
    max_age_seconds: int = SESSION_MAX_AGE_SECONDS,
) -> UUID:
    """Return the authenticated user UUID from a valid, unexpired bearer token."""

    serializer = URLSafeTimedSerializer(session_secret, salt=SESSION_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (BadSignature, SignatureExpired) as exc:
        raise SessionValidationError("invalid_session") from exc
    return _session_subject(payload)


def _parse_init_data(init_data: str) -> list[tuple[str, str]]:
    if not init_data:
        raise InitDataValidationError("missing_init_data")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataValidationError("malformed_init_data") from exc
    if not pairs:
        raise InitDataValidationError("missing_init_data")
    return pairs


def _required_values(pairs: list[tuple[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in _REQUIRED_INIT_DATA_FIELDS:
            if key in values:
                raise InitDataValidationError("duplicate_required_field")
            if not value:
                raise InitDataValidationError(f"missing_{key}")
            values[key] = value
    for key in _REQUIRED_INIT_DATA_FIELDS:
        if key not in values:
            raise InitDataValidationError(f"missing_{key}")
    return values


def _telegram_hash(pairs: list[tuple[str, str]], bot_token: str) -> str:
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pair for pair in pairs if pair[0] != "hash")
    )
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_auth_date(raw_value: str) -> datetime:
    try:
        timestamp = int(raw_value)
    except ValueError as exc:
        raise InitDataValidationError("invalid_auth_date") from exc
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise InitDataValidationError("invalid_auth_date") from exc


def _telegram_user_id(raw_value: str) -> int:
    try:
        payload: Any = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise InitDataValidationError("invalid_user") from exc
    if not isinstance(payload, Mapping):
        raise InitDataValidationError("invalid_user")
    user_id = payload.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise InitDataValidationError("invalid_user")
    return user_id


def _session_subject(payload: object) -> UUID:
    if not isinstance(payload, dict) or set(payload) != {"v", "sub"}:
        raise SessionValidationError("invalid_session")
    if payload.get("v") != SESSION_VERSION or not isinstance(payload.get("sub"), str):
        raise SessionValidationError("invalid_session")
    try:
        return UUID(payload["sub"])
    except ValueError as exc:
        raise SessionValidationError("invalid_session") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return value.astimezone(UTC)
