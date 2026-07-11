"""Shared Redis key date formatting helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def local_date_key(timezone: str, current: datetime | None = None) -> str:
    """Return a YYYYMMDD key component for the user's local calendar date."""

    local_current = current or datetime.now(ZoneInfo(timezone))
    return f"{local_current.astimezone(ZoneInfo(timezone)):%Y%m%d}"
