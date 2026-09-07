"""Deployment profile settings: one code base, two deployment configurations.

The ``prod`` profile keeps today's behaviour byte for byte; the ``small``
profile targets a 2 GB VPS serving a closed circle of 5-10 people
(``plan/small-profile.md``). This module owns the knobs and the per-profile
default table only. Call sites are migrated by the later tasks -- retrieval in
S4, metrics in S9, limits in S10, whitelist in S12 -- so importing this module
changes no behaviour by itself.

Resolution order for every value: the matching environment variable when it is
set and parses, otherwise the default of the selected profile. Environment
names deliberately mirror the constants they will replace, so a value can be
traced back to its call site by name alone.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum

LOGGER = logging.getLogger(__name__)

PROFILE_ENV_VAR = "DEPLOYMENT_PROFILE"
UNLIMITED_ENV_VALUES = frozenset({"none", "0", "off", "unlimited"})
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


class DeploymentProfile(StrEnum):
    """Which deployment configuration this process belongs to."""

    PROD = "prod"
    SMALL = "small"


class RetrievalBackend(StrEnum):
    """How facts and document chunks are searched (plan section 4.1)."""

    VECTOR = "vector"
    FTS = "fts"


class EmbeddingsBackend(StrEnum):
    """Whether the internal embeddings service is used at all."""

    SERVICE = "service"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    """Per-user allowances that differ between deployment profiles.

    ``None`` means "no limit"; it is spelled ``none`` (or ``0``) in the
    environment. Only the tariff axes of plan section 5A can be lifted that
    way -- the structural caps below them always carry a number.
    """

    daily_messages: int | None
    daily_voice_minutes: int | None
    monthly_pdf_pages: int | None
    active_scheduled_tasks: int
    active_reminders: int
    notes_per_user: int
    facts_per_user: int
    daily_budget_rub: Decimal
    tool_daily_limit_multiplier: int
    # Plan section 5C: driven by the 2 GB memory ceiling, so these move the
    # opposite way from everything above -- tighter, not looser.
    max_document_bytes: int
    max_photo_bytes: int
    max_ocr_pages: int
    ocr_render_dpi: int
    ocr_max_image_side: int | None
    ocr_thread_limit: int | None


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    """The full set of deployment knobs resolved for this process."""

    profile: DeploymentProfile
    retrieval_backend: RetrievalBackend
    embeddings_backend: EmbeddingsBackend
    metrics_export_enabled: bool
    whitelist_enabled: bool
    allowed_telegram_ids: frozenset[int]
    database_pool_max_size: int
    limits: ProfileLimits

    def telegram_id_allowed(self, telegram_id: int) -> bool:
        """Return whether this Telegram id may use the service (plan S12)."""

        if not self.whitelist_enabled:
            return True
        return telegram_id in self.allowed_telegram_ids

    def scaled_tool_daily_limit(self, base: int | None, *, scalable: bool = True) -> int | None:
        """Apply the profile multiplier to one tool's daily call allowance.

        ``scalable=False`` marks a tool whose limit is a property of the action
        rather than of the tariff (``set_timezone`` is a once-a-day operation in
        any profile), so its allowance is returned untouched.
        """

        if base is None or not scalable:
            return base
        return base * self.limits.tool_daily_limit_multiplier


PROD_LIMITS = ProfileLimits(
    daily_messages=100,
    daily_voice_minutes=10,
    monthly_pdf_pages=200,
    active_scheduled_tasks=10,
    active_reminders=25,
    notes_per_user=100,
    facts_per_user=500,
    daily_budget_rub=Decimal("15"),
    tool_daily_limit_multiplier=1,
    max_document_bytes=20 * 1024 * 1024,
    max_photo_bytes=10 * 1024 * 1024,
    max_ocr_pages=100,
    ocr_render_dpi=200,
    # No downscaling and no thread cap in prod: the 16 GB server absorbs both.
    ocr_max_image_side=None,
    ocr_thread_limit=None,
)

# Plan sections 5A and 5B. ``notes_per_user`` above 100 is only safe together
# with the capped ``list_notes`` output landed by S10: that cap is a context
# budget, not a storage budget.
SMALL_LIMITS = ProfileLimits(
    daily_messages=None,
    daily_voice_minutes=None,
    monthly_pdf_pages=None,
    active_scheduled_tasks=100,
    active_reminders=200,
    notes_per_user=300,
    facts_per_user=10_000,
    daily_budget_rub=Decimal("100"),
    tool_daily_limit_multiplier=5,
    max_document_bytes=10 * 1024 * 1024,
    max_photo_bytes=5 * 1024 * 1024,
    max_ocr_pages=30,
    # An A4 pixmap is ~6.5 MB at 150 dpi against ~11.6 MB at 200, and a phone
    # photo at 3000x4000 is ~36 MB in PIL before it is scaled down -- the single
    # largest allocation on the path (plan section 2).
    ocr_render_dpi=150,
    ocr_max_image_side=1600,
    ocr_thread_limit=1,
)

# The worker keeps exporting /metrics in both profiles: the container
# healthcheck hangs on that endpoint and it costs nothing (plan section 4.5).
# What the small profile drops is Prometheus, Grafana and blackbox -- separate
# containers removed in S9, not this flag.
PROFILE_DEFAULTS: Mapping[DeploymentProfile, ProfileSettings] = {
    DeploymentProfile.PROD: ProfileSettings(
        profile=DeploymentProfile.PROD,
        retrieval_backend=RetrievalBackend.VECTOR,
        embeddings_backend=EmbeddingsBackend.SERVICE,
        metrics_export_enabled=True,
        whitelist_enabled=False,
        allowed_telegram_ids=frozenset(),
        database_pool_max_size=10,
        limits=PROD_LIMITS,
    ),
    DeploymentProfile.SMALL: ProfileSettings(
        profile=DeploymentProfile.SMALL,
        retrieval_backend=RetrievalBackend.FTS,
        embeddings_backend=EmbeddingsBackend.NONE,
        metrics_export_enabled=True,
        whitelist_enabled=False,
        allowed_telegram_ids=frozenset(),
        # Postgres runs with max_connections = 20 and only the worker connects,
        # holding an app pool and a service pool. At the prod default of 10 each
        # that is the entire budget, leaving nothing for alembic during a deploy
        # or for psql (plan S6).
        database_pool_max_size=5,
        limits=SMALL_LIMITS,
    ),
}


def settings_from_env(environ: Mapping[str, str] | None = None) -> ProfileSettings:
    """Resolve deployment settings, falling back to the selected profile's defaults."""

    env = os.environ if environ is None else environ
    defaults = PROFILE_DEFAULTS[_profile_from_env(env)]
    whitelist_enabled, allowed_ids = _whitelist_from_env(env, defaults)
    return replace(
        defaults,
        retrieval_backend=_enum_from_env(
            env, "RETRIEVAL_BACKEND", RetrievalBackend, defaults.retrieval_backend
        ),
        embeddings_backend=_enum_from_env(
            env, "EMBEDDINGS_BACKEND", EmbeddingsBackend, defaults.embeddings_backend
        ),
        metrics_export_enabled=_bool_from_env(
            env, "METRICS_EXPORT_ENABLED", defaults.metrics_export_enabled
        ),
        database_pool_max_size=_positive_int_from_env(
            env, "DATABASE_POOL_MAX_SIZE", defaults.database_pool_max_size
        ),
        whitelist_enabled=whitelist_enabled,
        allowed_telegram_ids=allowed_ids,
        limits=_limits_from_env(env, defaults.limits),
    )


def _profile_from_env(env: Mapping[str, str]) -> DeploymentProfile:
    return _enum_from_env(env, PROFILE_ENV_VAR, DeploymentProfile, DeploymentProfile.PROD)


def profile_limits(environ: Mapping[str, str] | None = None) -> ProfileLimits:
    """Read the active limits at the point of use.

    Call sites read this instead of taking the settings as an argument: the cost
    is parsing a dozen environment variables, which is nothing next to the
    database or LLM call around it, and tests keep working through
    ``monkeypatch.setenv``.
    """

    return settings_from_env(environ).limits


def _limits_from_env(env: Mapping[str, str], defaults: ProfileLimits) -> ProfileLimits:
    return ProfileLimits(
        daily_messages=_optional_int_from_env(
            env, "DAILY_MESSAGE_LIMIT_DEFAULT", defaults.daily_messages
        ),
        daily_voice_minutes=_optional_int_from_env(
            env, "DAILY_VOICE_MINUTES_LIMIT", defaults.daily_voice_minutes
        ),
        monthly_pdf_pages=_optional_int_from_env(
            env, "MONTHLY_PDF_PAGES_LIMIT", defaults.monthly_pdf_pages
        ),
        active_scheduled_tasks=_positive_int_from_env(
            env, "MAX_ACTIVE_SCHEDULED_TASKS", defaults.active_scheduled_tasks
        ),
        active_reminders=_positive_int_from_env(
            env, "MAX_ACTIVE_REMINDERS", defaults.active_reminders
        ),
        notes_per_user=_positive_int_from_env(env, "MAX_NOTES_PER_USER", defaults.notes_per_user),
        facts_per_user=_positive_int_from_env(env, "MAX_FACTS_PER_USER", defaults.facts_per_user),
        daily_budget_rub=_decimal_from_env(
            env, "DAILY_BUDGET_RUB_DEFAULT", defaults.daily_budget_rub
        ),
        tool_daily_limit_multiplier=_positive_int_from_env(
            env, "TOOL_DAILY_LIMIT_MULTIPLIER", defaults.tool_daily_limit_multiplier
        ),
        max_document_bytes=_positive_int_from_env(
            env, "MAX_DOCUMENT_BYTES", defaults.max_document_bytes
        ),
        max_photo_bytes=_positive_int_from_env(env, "MAX_PHOTO_BYTES", defaults.max_photo_bytes),
        max_ocr_pages=_positive_int_from_env(env, "MAX_OCR_PAGES", defaults.max_ocr_pages),
        ocr_render_dpi=_positive_int_from_env(env, "OCR_RENDER_DPI", defaults.ocr_render_dpi),
        ocr_max_image_side=_optional_int_from_env(
            env, "OCR_MAX_IMAGE_SIDE", defaults.ocr_max_image_side
        ),
        ocr_thread_limit=_optional_int_from_env(env, "OCR_THREAD_LIMIT", defaults.ocr_thread_limit),
    )


def _whitelist_from_env(
    env: Mapping[str, str], defaults: ProfileSettings
) -> tuple[bool, frozenset[int]]:
    """Parse the access whitelist, failing closed on unusable entries.

    A configured but unparsable list must not silently reopen the service, so
    the whitelist stays enabled whenever the variable carries any value and only
    the entries that do parse are admitted.
    """

    raw = env.get("ALLOWED_TELEGRAM_IDS")
    if raw is None or not raw.strip():
        return defaults.whitelist_enabled, defaults.allowed_telegram_ids
    ids: set[int] = set()
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            ids.add(int(candidate))
        except ValueError:
            LOGGER.warning("invalid ALLOWED_TELEGRAM_IDS entry %r; skipped", candidate)
    return True, frozenset(ids)


def _enum_from_env[EnumT: StrEnum](
    env: Mapping[str, str],
    name: str,
    enum_type: type[EnumT],
    default: EnumT,
) -> EnumT:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return enum_type(raw.strip().lower())
    except ValueError:
        LOGGER.warning("invalid %s=%r; using %s", name, raw, default)
        return default


def _bool_from_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_ENV_VALUES:
        return True
    if value in _FALSE_ENV_VALUES:
        return False
    LOGGER.warning("invalid %s=%r; using %s", name, raw, default)
    return default


def _optional_int_from_env(env: Mapping[str, str], name: str, default: int | None) -> int | None:
    """Parse a limit that the small profile is allowed to lift entirely."""

    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    if raw.strip().lower() in UNLIMITED_ENV_VALUES:
        return None
    return _parse_positive_int(name, raw, default)


def _positive_int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse a structural cap, which always carries a number."""

    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    return _parse_positive_int(name, raw, default)


def _parse_positive_int[DefaultT: int | None](
    name: str, raw: str, default: DefaultT
) -> int | DefaultT:
    try:
        parsed = int(raw)
    except ValueError:
        LOGGER.warning("invalid %s=%r; using %s", name, raw, default)
        return default
    if parsed <= 0:
        LOGGER.warning("non-positive %s=%r; using %s", name, raw, default)
        return default
    return parsed


def _decimal_from_env(env: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        LOGGER.warning("invalid %s=%r; using %s", name, raw, default)
        return default
    if parsed <= 0:
        LOGGER.warning("non-positive %s=%r; using %s", name, raw, default)
        return default
    return parsed
