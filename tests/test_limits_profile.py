"""Limits under the small profile: lifted allowances and tightened memory caps (S10)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from PIL import Image

from core.limits import (
    daily_message_limit,
    daily_voice_minutes_limit,
    limits_snapshot,
    message_limit_reached,
    monthly_pdf_pages_limit,
)
from core.profile import profile_limits, settings_from_env
from core.spend import daily_budget_rub
from tools.timezone import register_timezone_tools
from worker.documents import downscaled

USER_ID = UUID("018f0000-0000-7000-8000-000000000001")


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def incr(self, name: str) -> int:
        value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(value)
        return value

    async def expire(self, name: str, seconds: int) -> bool:
        del name, seconds
        return True

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        del value, ex, nx
        self.values[name] = "1"
        return True


@pytest.fixture
def small_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "small")


async def test_a_lifted_message_limit_never_reports_exhausted(small_profile: None) -> None:
    """A huge counter must not block anyone once the profile removed the limit."""

    redis = FakeRedis({f"msg:{USER_ID}:19700101": "999999"})

    assert daily_message_limit("trial") is None
    assert await message_limit_reached(redis, USER_ID, "trial", "Europe/Moscow") is False


async def test_snapshot_reports_lifted_axes_as_none(small_profile: None) -> None:
    """Anything reading the snapshot has to cope with 'no limit', not with a number."""

    snapshot = await limits_snapshot(FakeRedis(), USER_ID, "trial", "Europe/Moscow")

    assert snapshot.messages.limit is None
    assert snapshot.voice_minutes.limit is None
    assert snapshot.pdf_pages.limit is None
    # The budget is a runaway-spend fuse, not a tariff: raised, never removed.
    assert snapshot.budget_rub.limit == Decimal("100")


def test_the_lifted_axes_are_exactly_the_tariff_ones(small_profile: None) -> None:
    assert daily_voice_minutes_limit() is None
    assert monthly_pdf_pages_limit() is None
    assert daily_budget_rub("trial") == Decimal("100")
    assert profile_limits().active_reminders == 200
    assert profile_limits().notes_per_user == 300
    assert profile_limits().facts_per_user == 10_000


def test_tool_allowances_scale_except_where_the_action_is_inherently_rare(
    small_profile: None,
) -> None:
    settings = settings_from_env()
    registry_specs: dict[str, object] = {}

    class _Registry:
        def register(self, spec: object) -> None:
            registry_specs[spec.name] = spec

    register_timezone_tools(_Registry(), None)  # type: ignore[arg-type]
    set_timezone = registry_specs["set_timezone"]

    assert set_timezone.daily_limit == 3
    assert set_timezone.daily_limit_scalable is False
    assert settings.scaled_tool_daily_limit(50) == 250
    assert (
        settings.scaled_tool_daily_limit(
            set_timezone.daily_limit,
            scalable=set_timezone.daily_limit_scalable,
        )
        == 3
    )


def test_memory_caps_tighten_in_the_small_profile(small_profile: None) -> None:
    limits = profile_limits()

    assert limits.max_document_bytes == 10 * 1024 * 1024
    assert limits.max_photo_bytes == 5 * 1024 * 1024
    assert limits.max_ocr_pages == 30
    assert limits.ocr_render_dpi == 150


def test_downscaling_bounds_the_largest_allocation_on_the_ocr_path(
    small_profile: None,
) -> None:
    """A phone photo is ~36 MB in PIL before this; the cap is what keeps OCR inside 640m."""

    limits = profile_limits()
    photo = Image.new("RGB", (3000, 4000))

    resized = downscaled(photo, limits)

    assert max(resized.size) == limits.ocr_max_image_side
    assert resized.size == (1200, 1600)


def test_downscaling_leaves_a_small_image_and_the_prod_profile_alone() -> None:
    small_image = Image.new("RGB", (800, 600))
    large_image = Image.new("RGB", (3000, 4000))

    prod_limits = profile_limits({})

    assert downscaled(small_image, profile_limits({"DEPLOYMENT_PROFILE": "small"})) is small_image
    assert downscaled(large_image, prod_limits) is large_image
