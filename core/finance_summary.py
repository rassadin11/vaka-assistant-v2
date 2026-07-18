"""Provider-neutral generation and Redis caching for finance AI summaries."""
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from core.finance_service import FinanceSummary, TopExpenseTransaction, money_text
from core.llm import LLMMessage, LLMProvider
from core.metrics import record_llm_cost
from core.spend import BudgetState, add_spend, budget_state, daily_budget_rub, get_spent_rub
from core.usage_recorder import UsageRecord, UsageRecordingProvider
from core.usage_store import save_usage

FINANCE_SUMMARY_SYSTEM_PROMPT = (
    "Ты — финансовый ассистент. По данным о тратах пользователя за период напиши "
    "2–4 предложения: главные статьи расходов, заметные изменения к прошлому периоду, "
    "превышения бюджетов. Только факты из данных, без выдуманных цифр, без советов "
    "купить финансовые продукты. Без приветствий"
)
POSITIVE_CACHE_TTL_SECONDS = 6 * 60 * 60
NEGATIVE_CACHE_TTL_SECONDS = 10 * 60
LOCK_TTL_SECONDS = 30
LOCK_WAIT_SECONDS = 3.0
LOCK_POLL_SECONDS = 0.1
DESCRIPTION_LIMIT = 80
LOGGER = logging.getLogger(__name__)

AiSummaryStatus = Literal["ready", "empty", "budget_exhausted", "unavailable"]
AiSummaryOutcome = Literal["generated", "cache_hit", "empty", "budget_exhausted", "unavailable"]
Sleep = Callable[[float], Awaitable[None]]
UsageSaver = Callable[
    [asyncpg.Pool, UUID, UUID, Literal["background"], Sequence[UsageRecord]], Awaitable[None]
]


class FinanceSummaryRedis(Protocol):
    """Ephemeral Redis operations used by generation and summary caching."""

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> Awaitable[Any]: ...

    def delete(self, *names: str) -> Awaitable[Any]: ...

    def incr(self, name: str) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True)
class AiSummaryResult:
    """Public status plus a bounded metric outcome for one summary request."""

    status: AiSummaryStatus
    summary: str | None = None
    outcome: AiSummaryOutcome = "unavailable"


def generation_key(user_id: UUID) -> str:
    return f"fin:gen:{user_id}"


def summary_cache_key(
    user_id: UUID,
    generation: int,
    start_date: str,
    end_date: str,
    local_date: str,
) -> str:
    return f"fin_summary:{user_id}:{generation}:{start_date}:{end_date}:{local_date}"


def summary_lock_key(user_id: UUID, generation: int, start_date: str, end_date: str) -> str:
    return f"fin:summary-lock:{user_id}:{generation}:{start_date}:{end_date}"


async def invalidate_finance_generation(redis: FinanceSummaryRedis, user_id: UUID) -> None:
    """Increment the user's cache generation without failing a committed mutation."""

    try:
        await redis.incr(generation_key(user_id))
    except Exception:
        LOGGER.warning("finance summary generation invalidation failed", exc_info=True)


def build_finance_summary_json(
    finance: FinanceSummary,
    top_transactions: Sequence[TopExpenseTransaction],
) -> str:
    """Build compact aggregate-only JSON with stable money strings."""

    payload = {
        "period": {"from": finance.start_date.isoformat(), "to": finance.end_date.isoformat()},
        "totals": {
            "expense": money_text(finance.totals.expense),
            "income": money_text(finance.totals.income),
        },
        "categories": [
            {"category": item.category, "expense": money_text(item.expense)}
            for item in finance.by_category
        ],
        "buckets": [
            {"bucket": item.bucket.isoformat(), "expense": money_text(item.expense)}
            for item in finance.by_bucket
        ],
        "previous_period": (
            None
            if finance.previous_period is None
            else {"expense": money_text(finance.previous_period.expense)}
        ),
        "top_expenses": [
            {
                "amount": money_text(item.amount),
                "category": item.category,
                "description": item.description[:DESCRIPTION_LIMIT],
            }
            for item in top_transactions[:5]
        ],
        "budgets": [
            {
                "category": item.category,
                "limit": money_text(item.limit),
                "spent": money_text(item.spent),
                "status": _budget_status_label(item.ratio),
            }
            for item in finance.budgets
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def generate_finance_summary(
    provider: LLMProvider,
    finance: FinanceSummary,
    top_transactions: Sequence[TopExpenseTransaction],
) -> tuple[str, str | None, tuple[UsageRecord, ...]]:
    """Make the single direct provider call required by the finance summary."""

    recorder = UsageRecordingProvider(provider)
    response = await recorder.generate(
        [
            LLMMessage(role="system", content=FINANCE_SUMMARY_SYSTEM_PROMPT),
            LLMMessage(role="user", content=build_finance_summary_json(finance, top_transactions)),
        ],
        temperature=0.3,
        max_tokens=600,
    )
    text = (response.message.content or "").strip()
    if not text:
        raise RuntimeError("finance summary provider returned no text")
    return text, response.finish_reason, tuple(recorder.records)


async def orchestrate_finance_summary(
    *,
    provider: LLMProvider | None,
    cache_redis: FinanceSummaryRedis,
    queue_redis: Any,
    pool: asyncpg.Pool,
    user_id: UUID,
    timezone: str,
    plan: str,
    trace_id: UUID,
    finance: FinanceSummary,
    top_transactions: Sequence[TopExpenseTransaction],
    now: datetime,
    sleep: Sleep = asyncio.sleep,
    usage_saver: UsageSaver = save_usage,
) -> AiSummaryResult:
    """Apply empty/budget/cache/lock gates and account one provider call.

    The caller supplies already-fetched aggregates, so no database transaction is
    held while this function waits for Redis or the LLM provider.
    """

    if finance.totals.expense == 0 and finance.totals.income == 0:
        return AiSummaryResult(status="empty", outcome="empty")

    spent = await get_spent_rub(queue_redis, user_id, timezone)
    if budget_state(spent, daily_budget_rub(plan)) is not BudgetState.OK:
        return AiSummaryResult(status="budget_exhausted", outcome="budget_exhausted")

    generation = await _read_generation(cache_redis, user_id)
    local_date = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    start_date = finance.start_date.isoformat()
    end_date = finance.end_date.isoformat()
    cache_key = summary_cache_key(user_id, generation, start_date, end_date, local_date)
    cached = await _read_cached(cache_redis, cache_key)
    if cached is not None:
        return cached

    lock_key = summary_lock_key(user_id, generation, start_date, end_date)
    acquired = await _acquire_lock(cache_redis, lock_key)
    if not acquired:
        waited = await _poll_cached(cache_redis, cache_key, sleep)
        return waited or AiSummaryResult(status="unavailable", outcome="unavailable")

    try:
        if provider is None:
            result = AiSummaryResult(status="unavailable", outcome="unavailable")
            await _write_cached(cache_redis, cache_key, result, NEGATIVE_CACHE_TTL_SECONDS)
            return result
        try:
            text, finish_reason, records = await generate_finance_summary(
                provider, finance, top_transactions
            )
            await usage_saver(pool, user_id, trace_id, "background", records)
            total_cost = sum((record.cost_usd for record in records), Decimal(0))
            await add_spend(queue_redis, user_id, timezone, total_cost)
            record_llm_cost(total_cost, "background")
        except Exception:
            LOGGER.warning("finance AI summary generation failed", exc_info=True)
            result = AiSummaryResult(status="unavailable", outcome="unavailable")
            await _write_cached(cache_redis, cache_key, result, NEGATIVE_CACHE_TTL_SECONDS)
            return result
        if finish_reason == "length":
            # The provider hit the token limit mid-sentence. Do not positive-cache a
            # truncated summary for hours; negative-cache briefly so it regenerates soon.
            LOGGER.warning(
                "finance AI summary truncated by token limit; not caching",
                extra={"user_id": str(user_id), "trace_id": str(trace_id)},
            )
            result = AiSummaryResult(status="unavailable", outcome="unavailable")
            await _write_cached(cache_redis, cache_key, result, NEGATIVE_CACHE_TTL_SECONDS)
            return result
        result = AiSummaryResult(status="ready", summary=text, outcome="generated")
        await _write_cached(cache_redis, cache_key, result, POSITIVE_CACHE_TTL_SECONDS)
        return result
    finally:
        await _release_lock(cache_redis, lock_key)


def _budget_status_label(ratio: float) -> str:
    if ratio >= 1:
        return "exceeded"
    if ratio >= 0.8:
        return "warning"
    return "ok"


async def _read_generation(redis: FinanceSummaryRedis, user_id: UUID) -> int:
    try:
        raw = await redis.get(generation_key(user_id))
        if isinstance(raw, bytes):
            raw = raw.decode("ascii")
        return max(0, int(raw or 0))
    except Exception:
        LOGGER.warning("finance summary generation read failed", exc_info=True)
        return 0


async def _read_cached(redis: FinanceSummaryRedis, key: str) -> AiSummaryResult | None:
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if data == {"status": "unavailable"}:
            return AiSummaryResult(status="unavailable", outcome="unavailable")
        if (
            isinstance(data, dict)
            and data.get("status") == "ready"
            and isinstance(data.get("summary"), str)
        ):
            return AiSummaryResult(status="ready", summary=data["summary"], outcome="cache_hit")
    except Exception:
        LOGGER.warning("finance summary cache read failed", exc_info=True)
    return None


async def _write_cached(
    redis: FinanceSummaryRedis, key: str, result: AiSummaryResult, ttl: int
) -> None:
    payload: dict[str, str] = {"status": result.status}
    if result.summary is not None:
        payload["summary"] = result.summary
    try:
        await redis.set(key, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    except Exception:
        LOGGER.warning("finance summary cache write failed", exc_info=True)


async def _acquire_lock(redis: FinanceSummaryRedis, key: str) -> bool:
    try:
        return bool(await redis.set(key, "1", ex=LOCK_TTL_SECONDS, nx=True))
    except Exception:
        LOGGER.warning("finance summary lock acquisition failed", exc_info=True)
        return True


async def _release_lock(redis: FinanceSummaryRedis, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:
        LOGGER.warning("finance summary lock release failed", exc_info=True)


async def _poll_cached(
    redis: FinanceSummaryRedis, key: str, sleep: Sleep
) -> AiSummaryResult | None:
    attempts = int(LOCK_WAIT_SECONDS / LOCK_POLL_SECONDS)
    for _ in range(attempts):
        await sleep(LOCK_POLL_SECONDS)
        cached = await _read_cached(redis, key)
        if cached is not None:
            return cached
    return None
