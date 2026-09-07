"""Unit tests for deployment profile resolution and its default table."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal

import pytest

from core.profile import (
    PROD_LIMITS,
    PROFILE_DEFAULTS,
    SMALL_LIMITS,
    DeploymentProfile,
    EmbeddingsBackend,
    RetrievalBackend,
    settings_from_env,
)
from core.spend import DEFAULT_DAILY_BUDGET_RUB, PRO_DAILY_BUDGET_RUB, daily_budget_rub

SMALL_ENV = {"DEPLOYMENT_PROFILE": "small"}


class _ListHandler(logging.Handler):
    """Collect formatted records straight off the module logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def profile_warnings() -> Iterator[list[str]]:
    """Capture warnings without relying on the root logger.

    ``caplog`` attaches to the root logger, and ``core.logging_setup`` replaces
    root handlers process-wide -- these tests would then pass alone and fail in
    a full run. Listening on ``core.profile`` itself is immune to that.

    ``dateparser.utils.setup_logging`` (pulled in by the date-parsing tools) runs
    ``dictConfig`` with ``disable_existing_loggers=True`` whenever the root
    logger still has no handler, which flips ``disabled`` on every logger that
    already exists -- ``core.profile`` included. Hence the reset below: without
    it these tests pass alone and fail after any module that parses dates.
    """

    logger = logging.getLogger("core.profile")
    handler = _ListHandler()
    previous_level = logger.level
    previous_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


def test_prod_limits_are_exactly_what_shipped_before_the_profile_existed() -> None:
    """The prod column is production behaviour, spelled out so it cannot drift.

    Until S10 these numbers lived as constants next to their call sites and this
    test compared the two. Now the profile is the only source, so the guard is
    the literal table: changing any of it changes what production does.
    """

    assert PROD_LIMITS.daily_messages == 100
    assert PROD_LIMITS.daily_voice_minutes == 10
    assert PROD_LIMITS.monthly_pdf_pages == 200
    assert PROD_LIMITS.active_scheduled_tasks == 10
    assert PROD_LIMITS.active_reminders == 25
    assert PROD_LIMITS.notes_per_user == 100
    assert PROD_LIMITS.facts_per_user == 500
    assert PROD_LIMITS.daily_budget_rub == DEFAULT_DAILY_BUDGET_RUB == Decimal("15")
    assert PROD_LIMITS.tool_daily_limit_multiplier == 1
    assert PROD_LIMITS.max_document_bytes == 20 * 1024 * 1024
    assert PROD_LIMITS.max_photo_bytes == 10 * 1024 * 1024
    assert PROD_LIMITS.max_ocr_pages == 100
    assert PROD_LIMITS.ocr_render_dpi == 200
    assert PROD_LIMITS.ocr_max_image_side is None
    assert PROD_LIMITS.ocr_thread_limit is None


def test_small_profile_tightens_only_the_memory_driven_caps() -> None:
    """Plan section 5C runs opposite to 5A: these get smaller, not larger."""

    assert SMALL_LIMITS.max_document_bytes == 10 * 1024 * 1024
    assert SMALL_LIMITS.max_photo_bytes == 5 * 1024 * 1024
    assert SMALL_LIMITS.max_ocr_pages == 30
    assert SMALL_LIMITS.ocr_render_dpi == 150
    assert SMALL_LIMITS.ocr_max_image_side == 1600
    assert SMALL_LIMITS.ocr_thread_limit == 1


def test_pro_plan_never_drops_below_the_profile_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The small profile raises the base budget past the prod pro tier."""

    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    assert daily_budget_rub("pro") == PRO_DAILY_BUDGET_RUB

    monkeypatch.setenv("DEPLOYMENT_PROFILE", "small")
    assert daily_budget_rub("pro") == SMALL_LIMITS.daily_budget_rub
    assert daily_budget_rub("trial") == SMALL_LIMITS.daily_budget_rub


def test_empty_environment_resolves_to_prod() -> None:
    assert settings_from_env({}) == PROFILE_DEFAULTS[DeploymentProfile.PROD]


def test_unknown_profile_falls_back_to_prod(profile_warnings: list[str]) -> None:
    settings = settings_from_env({"DEPLOYMENT_PROFILE": "tiny"})

    assert settings == PROFILE_DEFAULTS[DeploymentProfile.PROD]
    assert any("DEPLOYMENT_PROFILE" in message for message in profile_warnings)


def test_small_profile_defaults_follow_the_plan() -> None:
    settings = settings_from_env(SMALL_ENV)

    assert settings.profile is DeploymentProfile.SMALL
    assert settings.retrieval_backend is RetrievalBackend.FTS
    assert settings.embeddings_backend is EmbeddingsBackend.NONE
    assert settings.limits == SMALL_LIMITS
    assert settings.limits.daily_messages is None
    assert settings.limits.daily_voice_minutes is None
    assert settings.limits.monthly_pdf_pages is None
    assert settings.limits.facts_per_user == 10_000
    assert settings.limits.daily_budget_rub == Decimal("100")
    assert settings.limits.tool_daily_limit_multiplier == 5


def test_pool_ceiling_leaves_room_for_maintenance_connections() -> None:
    """Two worker pools must not consume the small server's whole max_connections."""

    prod = settings_from_env({})
    small = settings_from_env(SMALL_ENV)

    assert prod.database_pool_max_size == 10
    # max_connections = 20 on the small profile, and the worker holds an app pool
    # and a service pool: alembic and psql need what is left (plan S6).
    assert small.database_pool_max_size * 2 < 20
    assert settings_from_env({"DATABASE_POOL_MAX_SIZE": "4"}).database_pool_max_size == 4


def test_metrics_export_stays_on_in_both_profiles() -> None:
    """Plan section 4.5: the small profile drops Prometheus, not /metrics."""

    assert settings_from_env({}).metrics_export_enabled is True
    assert settings_from_env(SMALL_ENV).metrics_export_enabled is True
    assert settings_from_env({"METRICS_EXPORT_ENABLED": "false"}).metrics_export_enabled is False


@pytest.mark.parametrize("raw", ["none", "0", "off", "UNLIMITED"])
def test_tariff_limits_can_be_lifted(raw: str) -> None:
    settings = settings_from_env({"DAILY_MESSAGE_LIMIT_DEFAULT": raw})

    assert settings.limits.daily_messages is None


def test_environment_overrides_profile_defaults() -> None:
    settings = settings_from_env(
        {
            "DEPLOYMENT_PROFILE": "small",
            "RETRIEVAL_BACKEND": "vector",
            "EMBEDDINGS_BACKEND": "service",
            "MAX_NOTES_PER_USER": "42",
            "DAILY_BUDGET_RUB_DEFAULT": "7.50",
            "TOOL_DAILY_LIMIT_MULTIPLIER": "3",
        }
    )

    assert settings.profile is DeploymentProfile.SMALL
    assert settings.retrieval_backend is RetrievalBackend.VECTOR
    assert settings.embeddings_backend is EmbeddingsBackend.SERVICE
    assert settings.limits.notes_per_user == 42
    assert settings.limits.daily_budget_rub == Decimal("7.50")
    assert settings.limits.tool_daily_limit_multiplier == 3
    assert settings.limits.active_reminders == SMALL_LIMITS.active_reminders


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-5"])
def test_invalid_structural_cap_keeps_the_profile_default(raw: str) -> None:
    settings = settings_from_env({"MAX_ACTIVE_REMINDERS": raw})

    assert settings.limits.active_reminders == PROD_LIMITS.active_reminders


def test_structural_cap_cannot_be_lifted() -> None:
    """``none`` is meaningless for a cap that exists to bound memory or context."""

    settings = settings_from_env({"MAX_NOTES_PER_USER": "none"})

    assert settings.limits.notes_per_user == PROD_LIMITS.notes_per_user


def test_invalid_budget_keeps_the_profile_default() -> None:
    settings = settings_from_env({"DEPLOYMENT_PROFILE": "small", "DAILY_BUDGET_RUB_DEFAULT": "abc"})

    assert settings.limits.daily_budget_rub == SMALL_LIMITS.daily_budget_rub


def test_whitelist_is_disabled_when_unset() -> None:
    settings = settings_from_env({})

    assert settings.whitelist_enabled is False
    assert settings.allowed_telegram_ids == frozenset()
    assert settings.telegram_id_allowed(587963165) is True


def test_whitelist_admits_only_listed_ids() -> None:
    settings = settings_from_env({"ALLOWED_TELEGRAM_IDS": " 1, 2 ,3, "})

    assert settings.whitelist_enabled is True
    assert settings.allowed_telegram_ids == frozenset({1, 2, 3})
    assert settings.telegram_id_allowed(2) is True
    assert settings.telegram_id_allowed(4) is False


def test_unparsable_whitelist_fails_closed(profile_warnings: list[str]) -> None:
    """A configured but broken list must not reopen the service to everyone."""

    settings = settings_from_env({"ALLOWED_TELEGRAM_IDS": "oops"})

    assert settings.whitelist_enabled is True
    assert settings.allowed_telegram_ids == frozenset()
    assert settings.telegram_id_allowed(587963165) is False
    assert any("ALLOWED_TELEGRAM_IDS" in message for message in profile_warnings)


def test_blank_whitelist_leaves_it_disabled() -> None:
    settings = settings_from_env({"ALLOWED_TELEGRAM_IDS": "   "})

    assert settings.whitelist_enabled is False
    assert settings.telegram_id_allowed(587963165) is True


def test_tool_daily_limits_are_untouched_in_prod() -> None:
    settings = settings_from_env({})

    assert settings.scaled_tool_daily_limit(30) == 30
    assert settings.scaled_tool_daily_limit(None) is None


def test_tool_daily_limits_scale_in_the_small_profile() -> None:
    settings = settings_from_env(SMALL_ENV)

    assert settings.scaled_tool_daily_limit(30) == 150
    assert settings.scaled_tool_daily_limit(None) is None
    assert settings.scaled_tool_daily_limit(3, scalable=False) == 3
