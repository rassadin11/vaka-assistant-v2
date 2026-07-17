"""Finance dashboard endpoints for the Telegram Mini App."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, FastAPI, Header, Query, Response, status
from pydantic import BaseModel

from core.finance_service import (
    TX_CATEGORIES,
    FinanceConnection,
    InvalidFinanceCursor,
    InvalidFinanceRange,
    delete_transaction,
    fetch_summary,
    fetch_top_transactions,
    fetch_transactions_page,
    money_text,
)
from core.finance_summary import (
    FinanceSummaryRedis,
    invalidate_finance_generation,
    orchestrate_finance_summary,
)
from core.llm import LLMProvider
from core.rate_limit import RateLimitRedis
from core.tracing import current_trace_id
from webapp.dependencies import active_request_user, bearer_subject, require_webapp_rate_limit
from webapp.errors import WebAppError
from webapp.metrics import WebAppMetrics

PoolGetter = Callable[[], asyncpg.Pool]
CacheGetter = Callable[[], RateLimitRedis]
SummaryCacheGetter = Callable[[], FinanceSummaryRedis]
QueueGetter = Callable[[], Any]
ProviderGetter = Callable[[], LLMProvider | None]
Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)


class PeriodResponse(BaseModel):
    """Inclusive local dashboard period."""

    from_date: str
    to_date: str


class TotalsResponse(BaseModel):
    """Direction-separated totals."""

    expense: str
    income: str


class PreviousPeriodResponse(BaseModel):
    """Previous period expense."""

    expense: str


class CategoryResponse(BaseModel):
    """One category aggregate."""

    category: str
    expense: str
    share: float


class BucketResponse(BaseModel):
    """One local date bucket aggregate."""

    bucket: str
    expense: str


class BudgetResponse(BaseModel):
    """One monthly budget aggregate."""

    category: str
    limit: str
    spent: str
    ratio: float


class FinanceSummaryResponse(BaseModel):
    """Finance dashboard response excluding transaction details."""

    period: dict[str, str]
    totals: TotalsResponse
    prev_period: PreviousPeriodResponse | None
    by_category: list[CategoryResponse]
    by_bucket: list[BucketResponse]
    budgets: list[BudgetResponse]


class TransactionResponse(BaseModel):
    """One transaction in local time."""

    id: int
    ts_local: str
    amount: str
    direction: Literal["expense", "income"]
    category: str
    description: str


class TransactionsResponse(BaseModel):
    """One keyset page of transactions."""

    items: list[TransactionResponse]
    next_cursor: str | None


class AiSummaryResponse(BaseModel):
    """One of the stable finance AI card states."""

    status: Literal["ready", "empty", "budget_exhausted", "unavailable"]
    summary: str | None = None


def install_finance_routes(
    app: FastAPI,
    *,
    pool: PoolGetter,
    cache: CacheGetter,
    summary_cache: SummaryCacheGetter,
    queue: QueueGetter,
    provider: ProviderGetter,
    session_secret: str,
    metrics: WebAppMetrics,
    clock: Clock,
) -> None:
    """Attach finance routes with injectable runtime dependencies."""

    router = APIRouter(prefix="/app/api/finance")

    @router.get("/summary", response_model=FinanceSummaryResponse)
    async def summary(
        from_date: Annotated[date, Query(alias="from")],
        to_date: Annotated[date, Query(alias="to")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> FinanceSummaryResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        _validate_range(from_date, to_date)
        async with active_request_user(pool(), user_id) as user:
            result = await fetch_summary(
                cast(FinanceConnection, user.connection),
                user.timezone,
                from_date,
                to_date,
            )
        return FinanceSummaryResponse(
            period={"from": result.start_date.isoformat(), "to": result.end_date.isoformat()},
            totals=TotalsResponse(
                expense=money_text(result.totals.expense),
                income=money_text(result.totals.income),
            ),
            prev_period=(
                PreviousPeriodResponse(expense=money_text(result.previous_period.expense))
                if result.previous_period is not None
                else None
            ),
            by_category=[
                CategoryResponse(
                    category=item.category,
                    expense=money_text(item.expense),
                    share=item.share,
                )
                for item in result.by_category
            ],
            by_bucket=[
                BucketResponse(bucket=item.bucket.isoformat(), expense=money_text(item.expense))
                for item in result.by_bucket
            ],
            budgets=[
                BudgetResponse(
                    category=item.category,
                    limit=money_text(item.limit),
                    spent=money_text(item.spent),
                    ratio=item.ratio,
                )
                for item in result.budgets
            ],
        )

    @router.get("/transactions", response_model=TransactionsResponse)
    async def transactions(
        from_date: Annotated[date, Query(alias="from")],
        to_date: Annotated[date, Query(alias="to")],
        category: Annotated[str | None, Query()] = None,
        cursor: Annotated[str | None, Query()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> TransactionsResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        _validate_range(from_date, to_date)
        if category is not None and category not in TX_CATEGORIES:
            raise WebAppError(400, "invalid_finance_category", "Неизвестная категория.")
        async with active_request_user(pool(), user_id) as user:
            try:
                page = await fetch_transactions_page(
                    cast(FinanceConnection, user.connection),
                    user.timezone,
                    from_date,
                    to_date,
                    category=category,
                    cursor=cursor,
                )
            except InvalidFinanceCursor as exc:
                raise WebAppError(400, "invalid_cursor", "Некорректный курсор.") from exc
        return TransactionsResponse(
            items=[
                TransactionResponse(
                    id=item.id,
                    ts_local=item.ts_local.isoformat(),
                    amount=money_text(item.amount),
                    direction=item.direction,
                    category=item.category,
                    description=item.description,
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )

    @router.get("/ai-summary", response_model=AiSummaryResponse)
    async def ai_summary(
        from_date: Annotated[date, Query(alias="from")],
        to_date: Annotated[date, Query(alias="to")],
        authorization: Annotated[str | None, Header()] = None,
    ) -> AiSummaryResponse:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        _validate_range(from_date, to_date)
        async with active_request_user(pool(), user_id) as user:
            finance = await fetch_summary(
                cast(FinanceConnection, user.connection),
                user.timezone,
                from_date,
                to_date,
            )
            top_transactions = await fetch_top_transactions(
                cast(FinanceConnection, user.connection),
                user.timezone,
                from_date,
                to_date,
            )
            timezone = user.timezone
            plan = user.plan
        result = await orchestrate_finance_summary(
            provider=provider(),
            cache_redis=summary_cache(),
            queue_redis=queue(),
            pool=pool(),
            user_id=user_id,
            timezone=timezone,
            plan=plan,
            trace_id=_trace_uuid(),
            finance=finance,
            top_transactions=top_transactions,
            now=clock(),
        )
        metrics.ai_summary.labels(outcome=result.outcome).inc()
        return AiSummaryResponse(status=result.status, summary=result.summary)

    @router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove_transaction(
        transaction_id: int,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        user_id = bearer_subject(authorization, session_secret)
        await require_webapp_rate_limit(cache(), user_id, metrics)
        async with active_request_user(pool(), user_id) as user:
            deleted = await delete_transaction(
                cast(FinanceConnection, user.connection), transaction_id
            )
        if not deleted:
            raise WebAppError(404, "transaction_not_found", "Транзакция не найдена.")
        await invalidate_finance_generation(summary_cache(), user_id)
        metrics.transactions_deleted.inc()
        LOGGER.info("Mini App transaction deleted")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.include_router(router)


def _trace_uuid() -> UUID:
    raw = current_trace_id()
    try:
        return UUID(raw) if raw is not None else uuid4()
    except ValueError:
        return uuid4()


def _validate_range(start_date: date, end_date: date) -> None:
    try:
        days = (end_date - start_date).days + 1
        if days < 1 or days > 366:
            raise InvalidFinanceRange
    except InvalidFinanceRange as exc:
        raise WebAppError(
            400,
            "invalid_finance_range",
            "Диапазон финансов должен быть от 1 до 366 дней.",
        ) from exc
