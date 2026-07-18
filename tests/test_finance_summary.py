"""Unit coverage for the current finance AI-summary service."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from core.finance_service import (
    BucketAggregate,
    BudgetAggregate,
    CategoryAggregate,
    FinanceSummary,
    FinanceTotals,
    PreviousPeriod,
    TopExpenseTransaction,
)
from core.finance_summary import (
    NEGATIVE_CACHE_TTL_SECONDS,
    AiSummaryResult,
    generation_key,
    invalidate_finance_generation,
    orchestrate_finance_summary,
    summary_cache_key,
)
from core.llm import LLMProviderError
from core.llm_mock import MockLLMProvider, mock_text_response
from core.usage_recorder import UsageRecord


class MemoryRedis:
    def __init__(self, *, lock_held: bool = False, spent: str | None = None) -> None:
        self.values: dict[str, str] = {}
        self.sets: list[tuple[str, str, int | None, bool]] = []
        self.incrs: list[str] = []
        self.incrbyfloats: list[tuple[str, float]] = []
        self.expires: list[tuple[str, int]] = []
        self.lock_held = lock_held
        self.spent = spent

    async def get(self, name: str) -> str | None:
        if name.startswith("spend_rub:"):
            return self.spent
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and self.lock_held and name.startswith("fin:summary-lock:"):
            return False
        if nx and name in self.values:
            return False
        self.values[name] = value
        self.sets.append((name, value, ex, nx))
        return True

    async def delete(self, *names: str) -> None:
        for name in names:
            self.values.pop(name, None)

    async def incr(self, name: str) -> int:
        self.incrs.append(name)
        next_value = int(self.values.get(name, "0")) + 1
        self.values[name] = str(next_value)
        return next_value

    async def incrbyfloat(self, name: str, amount: float) -> float:
        self.incrbyfloats.append((name, amount))
        next_value = float(self.values.get(name, "0")) + amount
        self.values[name] = str(next_value)
        return next_value

    async def expire(self, name: str, seconds: int) -> None:
        self.expires.append((name, seconds))


async def no_sleep(_seconds: float) -> None:
    return None


async def usage_saver(
    _pool: object,
    user_id: UUID,
    trace_id: UUID,
    queue: Literal["background"],
    records: Sequence[UsageRecord],
) -> None:
    saved_usage.append((user_id, trace_id, queue, tuple(records)))


saved_usage: list[tuple[UUID, UUID, str, tuple[UsageRecord, ...]]] = []


async def test_empty_and_budget_exhausted_do_not_call_provider() -> None:
    user_id = uuid4()
    provider = MockLLMProvider.scripted([mock_text_response("unused")])

    empty = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=MemoryRedis(),
        queue_redis=MemoryRedis(),
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=uuid4(),
        finance=_summary(expense="0", income="0", categories=()),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )
    exhausted = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=MemoryRedis(),
        queue_redis=MemoryRedis(spent="15"),
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=uuid4(),
        finance=_summary(),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )

    assert empty == AiSummaryResult(status="empty", outcome="empty")
    assert exhausted == AiSummaryResult(status="budget_exhausted", outcome="budget_exhausted")
    assert provider.calls == []


async def test_cold_cache_generates_records_usage_spend_and_cache_then_warm_cache_hits() -> None:
    saved_usage.clear()
    user_id = uuid4()
    trace_id = uuid4()
    cache = MemoryRedis()
    queue = MemoryRedis()
    provider = MockLLMProvider.scripted([mock_text_response("Краткое резюме.")])

    cold = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=cache,
        queue_redis=queue,
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=trace_id,
        finance=_summary(),
        top_transactions=(TopExpenseTransaction(Decimal("50"), "food", "Обед " * 30),),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )
    warm = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=cache,
        queue_redis=queue,
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=uuid4(),
        finance=_summary(),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )

    assert cold == AiSummaryResult(status="ready", summary="Краткое резюме.", outcome="generated")
    assert warm == AiSummaryResult(status="ready", summary="Краткое резюме.", outcome="cache_hit")
    assert len(provider.calls) == 1
    assert provider.calls[0].temperature == 0.3
    assert provider.calls[0].max_tokens == 600
    assert saved_usage[0][0:3] == (user_id, trace_id, "background")
    assert saved_usage[0][3]
    assert queue.incrbyfloats and queue.expires
    assert any(name.startswith("fin_summary:") for name, *_ in cache.sets)


async def test_truncated_summary_records_usage_but_is_not_positive_cached() -> None:
    saved_usage.clear()
    user_id = uuid4()
    trace_id = uuid4()
    cache = MemoryRedis()
    queue = MemoryRedis()
    provider = MockLLMProvider.scripted([mock_text_response("Одно", finish_reason="length")])

    truncated = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=cache,
        queue_redis=queue,
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=trace_id,
        finance=_summary(),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )

    # The provider really ran, so token usage/spend are still accounted for...
    assert truncated == AiSummaryResult(status="unavailable", outcome="unavailable")
    assert len(provider.calls) == 1
    assert saved_usage and saved_usage[0][0:3] == (user_id, trace_id, "background")
    assert queue.incrbyfloats and queue.expires
    # ...but the truncated text is never positive-cached, and a warm read regenerates.
    summary_writes = [entry for entry in cache.sets if entry[0].startswith("fin_summary:")]
    assert summary_writes, "expected a negative cache entry"
    _name, value, ttl, _nx = summary_writes[-1]
    assert '"status":"unavailable"' in value
    assert "Одно" not in value
    assert ttl == NEGATIVE_CACHE_TTL_SECONDS


async def test_provider_error_writes_negative_cache_and_stampede_lock_skips_provider() -> None:
    user_id = uuid4()
    cache = MemoryRedis()
    provider = MockLLMProvider.scripted([LLMProviderError("down")])

    failed = await orchestrate_finance_summary(
        provider=provider,
        cache_redis=cache,
        queue_redis=MemoryRedis(),
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=uuid4(),
        finance=_summary(),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )
    locked_provider = MockLLMProvider.scripted([mock_text_response("must not run")])
    locked = await orchestrate_finance_summary(
        provider=locked_provider,
        cache_redis=MemoryRedis(lock_held=True),
        queue_redis=MemoryRedis(),
        pool=object(),  # type: ignore[arg-type]
        user_id=user_id,
        timezone="UTC",
        plan="trial",
        trace_id=uuid4(),
        finance=_summary(),
        top_transactions=(),
        now=datetime(2026, 7, 17, tzinfo=UTC),
        sleep=no_sleep,
        usage_saver=usage_saver,  # type: ignore[arg-type]
    )

    assert failed == AiSummaryResult(status="unavailable", outcome="unavailable")
    assert any('"unavailable"' in value for _name, value, _ex, _nx in cache.sets)
    assert locked == AiSummaryResult(status="unavailable", outcome="unavailable")
    assert locked_provider.calls == []


async def test_generation_bump_makes_new_key_miss_old_cache() -> None:
    user_id = uuid4()
    cache = MemoryRedis()
    today = "2026-07-17"
    old_key = summary_cache_key(user_id, 0, "2026-07-01", "2026-07-31", today)
    cache.values[old_key] = '{"status":"ready","summary":"old"}'

    await invalidate_finance_generation(cache, user_id)

    new_key = summary_cache_key(user_id, 1, "2026-07-01", "2026-07-31", today)
    assert cache.values[generation_key(user_id)] == "1"
    assert old_key != new_key
    assert new_key not in cache.values


def _summary(
    *,
    expense: str = "100",
    income: str = "0",
    categories: tuple[CategoryAggregate, ...] | None = None,
) -> FinanceSummary:
    by_category = (
        (CategoryAggregate("food", Decimal(expense), 1.0),)
        if categories is None and Decimal(expense) > 0
        else categories or ()
    )
    return FinanceSummary(
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
        totals=FinanceTotals(Decimal(expense), Decimal(income)),
        previous_period=PreviousPeriod(Decimal("50")),
        by_category=by_category,
        by_bucket=(BucketAggregate(date(2026, 7, 17), Decimal(expense)),)
        if Decimal(expense) > 0
        else (),
        budgets=(BudgetAggregate("food", Decimal("80"), Decimal(expense), 1.25),)
        if Decimal(expense) > 0
        else (),
    )
