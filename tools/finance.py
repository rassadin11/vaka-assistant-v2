"""Finance tool implementations backed by the user-scoped database role."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, cast
from zoneinfo import ZoneInfo

import asyncpg
import matplotlib
from pydantic import BaseModel, ConfigDict

from core.context import TaskContext
from core.db import user_transaction
from core.tools import RiskLevel, ToolRegistry, ToolResult, ToolSpec

matplotlib.use("Agg")

LOGGER = logging.getLogger(__name__)
MONEY_QUANTUM = Decimal("0.01")
# Keep strong references so the event loop cannot garbage-collect in-flight sends.
_CHART_TASKS: set[asyncio.Task[None]] = set()
SendPhoto = Callable[[int, bytes, str | None], Awaitable[None]]


class TxCategory(StrEnum):
    """Transaction categories supported by the v1 schema."""

    FOOD = "food"
    TRANSPORT = "transport"
    HOUSING = "housing"
    HEALTH = "health"
    ENTERTAINMENT = "entertainment"
    SHOPPING = "shopping"
    SUBSCRIPTIONS = "subscriptions"
    SALARY = "salary"
    OTHER = "other"


class AddTransactionArgs(BaseModel):
    """Arguments for recording one income or expense."""

    model_config = ConfigDict(extra="ignore")

    amount: float
    direction: Literal["expense", "income"]
    category: TxCategory = TxCategory.OTHER
    description: str = ""
    ts: str | None = None


class QueryTransactionsArgs(BaseModel):
    """Arguments for an aggregated transaction query."""

    model_config = ConfigDict(extra="ignore")

    period_start: str
    period_end: str
    category: TxCategory | None = None
    group_by: Literal["category", "day", "none"] = "category"


class SetBudgetArgs(BaseModel):
    """Arguments for creating or updating a monthly category budget."""

    model_config = ConfigDict(extra="ignore")

    category: TxCategory
    monthly_limit: float


class EmptyArgs(BaseModel):
    """Arguments for a tool without model-controlled values."""

    model_config = ConfigDict(extra="ignore")


def register_finance_tools(
    registry: ToolRegistry,
    app_pool: asyncpg.Pool,
    send_photo: SendPhoto | None,
) -> None:
    """Register the v1 finance tools with their worker-owned dependencies."""

    async def add_transaction(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _add_transaction(app_pool, ctx, cast(AddTransactionArgs, args))

    async def query_transactions(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _query_transactions(
            app_pool, send_photo, ctx, cast(QueryTransactionsArgs, args)
        )

    async def set_budget(ctx: TaskContext, args: BaseModel) -> ToolResult:
        return await _set_budget(app_pool, ctx, cast(SetBudgetArgs, args))

    async def get_budget_status(ctx: TaskContext, _args: BaseModel) -> ToolResult:
        return await _get_budget_status(app_pool, ctx)

    registry.register(
        ToolSpec(
            "add_transaction",
            "Record one income or expense transaction.",
            AddTransactionArgs,
            RiskLevel.MUTATING_INTERNAL,
            add_transaction,
        )
    )
    registry.register(
        ToolSpec(
            "query_transactions",
            "Aggregate transactions for an inclusive date range.",
            QueryTransactionsArgs,
            RiskLevel.READ_ONLY,
            query_transactions,
        )
    )
    registry.register(
        ToolSpec(
            "set_budget",
            "Create or update a monthly spending budget for a category.",
            SetBudgetArgs,
            RiskLevel.MUTATING_INTERNAL,
            set_budget,
        )
    )
    registry.register(
        ToolSpec(
            "get_budget_status",
            "Get all monthly budgets and their current spending.",
            EmptyArgs,
            RiskLevel.READ_ONLY,
            get_budget_status,
        )
    )


async def _add_transaction(
    pool: asyncpg.Pool,
    ctx: TaskContext,
    args: AddTransactionArgs,
) -> ToolResult:
    amount = _quantize_money(args.amount)
    if amount is None or amount <= 0:
        return ToolResult(status="error", error="Сумма должна быть больше нуля.", retryable=True)

    try:
        timestamp = _parse_timestamp(args.ts, ctx.timezone)
    except ValueError:
        return ToolResult(
            status="error", error="Укажите дату операции в формате ISO 8601.", retryable=True
        )
    now = datetime.now(UTC)
    if timestamp > now + timedelta(days=1):
        return ToolResult(
            status="error",
            error="Дата операции не может быть позже чем на один день в будущем.",
        )

    timezone = ZoneInfo(ctx.timezone)
    day_start, day_end = _day_bounds(timestamp, timezone)
    month_start, month_end = _month_bounds(timestamp, timezone)
    category = args.category.value
    async with user_transaction(pool, ctx.user_id) as connection:
        await connection.execute(
            """
            INSERT INTO transactions (
                user_id, amount, direction, category, currency, description, ts, created_at
            )
            VALUES ($1, $2, $3, $4, 'RUB', $5, $6, now())
            """,
            ctx.user_id,
            amount,
            args.direction,
            category,
            args.description,
            timestamp,
        )
        today_total = await connection.fetchval(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE category = $1 AND direction = $2 AND ts >= $3 AND ts < $4
            """,
            category,
            args.direction,
            day_start,
            day_end,
        )
        budget_limit = await connection.fetchval(
            "SELECT monthly_limit FROM budgets WHERE category = $1",
            category,
        )
        payload: dict[str, object] = {
            "category": category,
            "today_total": _money_text(today_total),
        }
        if budget_limit is not None:
            spent = await connection.fetchval(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM transactions
                WHERE category = $1 AND direction = 'expense' AND ts >= $2 AND ts < $3
                """,
                category,
                month_start,
                month_end,
            )
            limit = _as_decimal(budget_limit)
            monthly_spent = _as_decimal(spent)
            payload["budget_status"] = _budget_status(limit, monthly_spent)
            if args.direction == "expense":
                before = monthly_spent - amount
                alert = _budget_alert(before, monthly_spent, limit)
                if alert is not None:
                    payload["budget_alert"] = alert
    return ToolResult(status="ok", payload=payload)


async def _query_transactions(
    pool: asyncpg.Pool,
    send_photo: SendPhoto | None,
    ctx: TaskContext,
    args: QueryTransactionsArgs,
) -> ToolResult:
    try:
        start_date = date.fromisoformat(args.period_start)
        end_date = date.fromisoformat(args.period_end)
    except ValueError:
        return ToolResult(
            status="error", error="Укажите даты в формате ISO (YYYY-MM-DD).", retryable=True
        )
    if end_date < start_date:
        return ToolResult(status="error", error="Дата окончания не может быть раньше даты начала.")
    if (end_date - start_date).days > 366:
        return ToolResult(status="error", error="Период не может быть больше 366 дней.")

    timezone = ZoneInfo(ctx.timezone)
    start = _local_midnight(start_date, timezone)
    end = _local_midnight(end_date + timedelta(days=1), timezone)
    category = args.category.value if args.category is not None else None
    query, query_args = _aggregation_query(args.group_by, start, end, category, ctx.timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        records = await connection.fetch(query, *query_args)

    rows = [_aggregation_payload_row(args.group_by, record) for record in records[:100]]
    has_non_rub = any(str(record["currency"]) != "RUB" for record in records)
    if has_non_rub:
        for row, record in zip(rows, records, strict=False):
            row["currency"] = str(record["currency"])
    payload: dict[str, object] = {
        "group_by": args.group_by,
        "rows": rows,
        "truncated": len(records) > 100,
    }
    if send_photo is not None and args.group_by == "day" and (end_date - start_date).days + 1 >= 14:
        _schedule_chart(send_photo, ctx.chat_id, records)
    return ToolResult(status="ok", payload=payload)


async def _set_budget(pool: asyncpg.Pool, ctx: TaskContext, args: SetBudgetArgs) -> ToolResult:
    monthly_limit = _quantize_money(args.monthly_limit)
    if monthly_limit is None or monthly_limit <= 0:
        return ToolResult(
            status="error", error="Лимит бюджета должен быть больше нуля.", retryable=True
        )
    async with user_transaction(pool, ctx.user_id) as connection:
        await connection.execute(
            """
            INSERT INTO budgets (user_id, category, monthly_limit, created_at, updated_at)
            VALUES ($1, $2, $3, now(), now())
            ON CONFLICT (user_id, category)
            DO UPDATE SET monthly_limit = EXCLUDED.monthly_limit, updated_at = now()
            """,
            ctx.user_id,
            args.category.value,
            monthly_limit,
        )
    return ToolResult(
        status="ok",
        payload={"category": args.category.value, "monthly_limit": _money_text(monthly_limit)},
    )


async def _get_budget_status(pool: asyncpg.Pool, ctx: TaskContext) -> ToolResult:
    timezone = ZoneInfo(ctx.timezone)
    month_start, month_end = _month_bounds(datetime.now(UTC), timezone)
    async with user_transaction(pool, ctx.user_id) as connection:
        records = await connection.fetch(
            """
            SELECT b.category, b.monthly_limit, COALESCE(SUM(t.amount), 0) AS spent
            FROM budgets AS b
            LEFT JOIN transactions AS t
              ON t.category = b.category
             AND t.direction = 'expense'
             AND t.ts >= $1 AND t.ts < $2
            GROUP BY b.category, b.monthly_limit
            ORDER BY b.category
            """,
            month_start,
            month_end,
        )
    budgets = [
        {
            "category": str(record["category"]),
            **_budget_status(_as_decimal(record["monthly_limit"]), _as_decimal(record["spent"])),
        }
        for record in records
    ]
    return ToolResult(status="ok", payload={"budgets": budgets})


def _aggregation_query(
    group_by: str,
    start: datetime,
    end: datetime,
    category: str | None,
    timezone: str,
) -> tuple[str, tuple[object, ...]]:
    filter_sql = "WHERE ts >= $1 AND ts < $2 AND ($3::text IS NULL OR category = $3)"
    if group_by == "category":
        return (
            f"""
            SELECT category, currency, SUM(amount) AS total
            FROM transactions {filter_sql}
            GROUP BY category, currency
            ORDER BY category, currency
            LIMIT 101
            """,
            (start, end, category),
        )
    if group_by == "day":
        return (
            f"""
            SELECT (ts AT TIME ZONE $4)::date AS day, currency, SUM(amount) AS total
            FROM transactions {filter_sql}
            GROUP BY day, currency
            ORDER BY day, currency
            LIMIT 101
            """,
            (start, end, category, timezone),
        )
    return (
        f"""
        SELECT currency, SUM(amount) AS total
        FROM transactions {filter_sql}
        GROUP BY currency
        ORDER BY currency
        LIMIT 101
        """,
        (start, end, category),
    )


def _aggregation_payload_row(group_by: str, record: asyncpg.Record) -> dict[str, str]:
    row = {"total": _money_text(record["total"])}
    if group_by == "category":
        row["category"] = str(record["category"])
    elif group_by == "day":
        day = record["day"]
        row["day"] = day.isoformat() if isinstance(day, date) else str(day)
    return row


def _schedule_chart(send_photo: SendPhoto, chat_id: int, records: list[asyncpg.Record]) -> None:
    try:
        png = _render_chart(records)
    except Exception:
        LOGGER.warning("finance chart rendering failed", exc_info=True)
        return

    task: asyncio.Task[None] = asyncio.create_task(
        _send_chart(send_photo, chat_id, png, "График операций по дням.")
    )
    _CHART_TASKS.add(task)
    task.add_done_callback(_CHART_TASKS.discard)
    task.add_done_callback(_log_chart_send_failure)


async def _send_chart(send_photo: SendPhoto, chat_id: int, png: bytes, caption: str) -> None:
    await send_photo(chat_id, png, caption)


def _log_chart_send_failure(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except Exception:
        LOGGER.warning("finance chart send failed", exc_info=True)


def _render_chart(records: list[asyncpg.Record]) -> bytes:
    """Render a compact daily chart without involving a GUI backend."""

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    values_by_currency: dict[str, list[tuple[str, Decimal]]] = {}
    for record in records[:100]:
        day = record["day"]
        label = day.isoformat() if isinstance(day, date) else str(day)
        values_by_currency.setdefault(str(record["currency"]), []).append(
            (label, _as_decimal(record["total"]))
        )
    figure = Figure(figsize=(8, 4), tight_layout=True)
    axes = figure.subplots()
    for currency, values in values_by_currency.items():
        axes.plot(
            [item[0] for item in values],
            [float(item[1]) for item in values],
            marker="o",
            label=currency,
        )
    axes.set_title("Операции по дням")
    axes.set_ylabel("Сумма")
    if values_by_currency:
        axes.legend()
    axes.tick_params(axis="x", rotation=45)
    output = io.BytesIO()
    FigureCanvasAgg(figure).print_png(output)  # type: ignore[no-untyped-call]
    return output.getvalue()


def _parse_timestamp(raw: str | None, timezone_name: str) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def _day_bounds(timestamp: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    local_date = timestamp.astimezone(timezone).date()
    return (
        _local_midnight(local_date, timezone),
        _local_midnight(local_date + timedelta(days=1), timezone),
    )


def _month_bounds(timestamp: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    local = timestamp.astimezone(timezone)
    start = _local_midnight(local.date().replace(day=1), timezone)
    if local.month == 12:
        end_date = date(local.year + 1, 1, 1)
    else:
        end_date = date(local.year, local.month + 1, 1)
    return start, _local_midnight(end_date, timezone)


def _local_midnight(value: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone)


def _quantize_money(value: float) -> Decimal | None:
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _as_decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_text(value: object) -> str:
    return format(_as_decimal(value), ".2f")


def _budget_status(monthly_limit: Decimal, spent: Decimal) -> dict[str, str | float]:
    percent = (spent * Decimal("100") / monthly_limit).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return {
        "monthly_limit": _money_text(monthly_limit),
        "spent": _money_text(spent),
        "percent": float(percent),
    }


def _budget_alert(before: Decimal, after: Decimal, limit: Decimal) -> str | None:
    if before < limit and after >= limit:
        return "Бюджет по этой категории исчерпан."
    threshold = limit * Decimal("0.8")
    if before < threshold and after >= threshold:
        return "Расходы по этой категории достигли 80% бюджета."
    return None
