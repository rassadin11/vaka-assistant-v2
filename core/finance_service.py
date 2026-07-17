"""Shared finance domain helpers for tools and the Mini App."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

MONEY_QUANTUM = Decimal("0.01")
PAGE_LIMIT = 50
MAX_CURSOR_LENGTH = 512
TX_CATEGORIES = frozenset(
    {
        "food",
        "transport",
        "housing",
        "health",
        "entertainment",
        "shopping",
        "subscriptions",
        "salary",
        "other",
    }
)
BucketGranularity = Literal["day", "week", "month"]


class FinanceConnection(Protocol):
    """Database operations used by the finance domain service."""

    async def fetch(self, query: str, *args: object) -> list[Any]: ...

    async def fetchrow(self, query: str, *args: object) -> Any | None: ...

    async def execute(self, query: str, *args: object) -> str: ...


class InvalidFinanceRange(ValueError):
    """The requested local date range is outside the dashboard contract."""


class InvalidFinanceCursor(ValueError):
    """The pagination cursor is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class FinanceTotals:
    """Direction-separated RUB totals."""

    expense: Decimal
    income: Decimal


@dataclass(frozen=True, slots=True)
class PreviousPeriod:
    """Previous same-length period expense."""

    expense: Decimal


@dataclass(frozen=True, slots=True)
class CategoryAggregate:
    """Expense aggregate for one category."""

    category: str
    expense: Decimal
    share: float


@dataclass(frozen=True, slots=True)
class BucketAggregate:
    """Expense aggregate for one local time bucket."""

    bucket: date
    expense: Decimal


@dataclass(frozen=True, slots=True)
class BudgetAggregate:
    """Monthly budget and its RUB expense usage."""

    category: str
    limit: Decimal
    spent: Decimal
    ratio: float


@dataclass(frozen=True, slots=True)
class FinanceSummary:
    """All SQL-derived dashboard aggregates for one local date range."""

    start_date: date
    end_date: date
    totals: FinanceTotals
    previous_period: PreviousPeriod | None
    by_category: tuple[CategoryAggregate, ...]
    by_bucket: tuple[BucketAggregate, ...]
    budgets: tuple[BudgetAggregate, ...]


@dataclass(frozen=True, slots=True)
class TransactionItem:
    """One visible transaction serialized in the user's timezone."""

    id: int
    ts_local: datetime
    amount: Decimal
    direction: Literal["expense", "income"]
    category: str
    description: str


@dataclass(frozen=True, slots=True)
class TransactionsPage:
    """One keyset page of transactions."""

    items: tuple[TransactionItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TopExpenseTransaction:
    """One of the largest RUB expenses used by the aggregate AI summary."""

    amount: Decimal
    category: str
    description: str


def quantize_money(value: object) -> Decimal | None:
    """Quantize user-controlled numeric input to schema money precision."""

    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def as_decimal(value: object) -> Decimal:
    """Convert a database numeric value to money precision."""

    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def money_text(value: object) -> str:
    """Serialize money with exactly two decimal places."""

    return format(as_decimal(value), ".2f")


def parse_timestamp(raw: str | None, timezone_name: str) -> datetime:
    """Parse an optional model timestamp, treating naive values as user-local."""

    if raw is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def local_midnight(value: date, timezone: ZoneInfo) -> datetime:
    """Return midnight for a local calendar date."""

    return datetime.combine(value, time.min, tzinfo=timezone)


def day_bounds(timestamp: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the half-open local day containing a timestamp."""

    local_date = timestamp.astimezone(timezone).date()
    return (
        local_midnight(local_date, timezone),
        local_midnight(local_date + timedelta(days=1), timezone),
    )


def month_bounds(timestamp: datetime, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the half-open local calendar month containing a timestamp."""

    local = timestamp.astimezone(timezone)
    start_date = local.date().replace(day=1)
    if local.month == 12:
        end_date = date(local.year + 1, 1, 1)
    else:
        end_date = date(local.year, local.month + 1, 1)
    return local_midnight(start_date, timezone), local_midnight(end_date, timezone)


def inclusive_date_bounds(
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime, int]:
    """Convert inclusive local dates to a half-open UTC interval."""

    days = (end_date - start_date).days + 1
    if days < 1 or days > 366:
        raise InvalidFinanceRange
    return (
        local_midnight(start_date, timezone).astimezone(UTC),
        local_midnight(end_date + timedelta(days=1), timezone).astimezone(UTC),
        days,
    )


def previous_same_length_dates(start_date: date, end_date: date) -> tuple[date, date]:
    """Return the adjacent previous inclusive local date range."""

    days = (end_date - start_date).days + 1
    if days < 1:
        raise InvalidFinanceRange
    return start_date - timedelta(days=days), start_date - timedelta(days=1)


def previous_same_length_bounds(
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return the previous same-length local period as half-open UTC bounds."""

    previous_start, previous_end = previous_same_length_dates(start_date, end_date)
    start, end, _ = inclusive_date_bounds(previous_start, previous_end, timezone)
    return start, end


def aggregation_query(
    group_by: str,
    start: datetime,
    end: datetime,
    category: str | None,
    timezone: str,
) -> tuple[str, tuple[object, ...]]:
    """Build the legacy tool aggregation SQL without changing its result shape."""

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


def aggregation_payload_row(group_by: str, record: Any) -> dict[str, str]:
    """Build one legacy finance tool payload row."""

    row = {"total": money_text(record["total"])}
    if group_by == "category":
        row["category"] = str(record["category"])
    elif group_by == "day":
        value = record["day"]
        row["day"] = value.isoformat() if isinstance(value, date) else str(value)
    return row


async def fetch_summary(
    connection: FinanceConnection,
    timezone_name: str,
    start_date: date,
    end_date: date,
) -> FinanceSummary:
    """Fetch dashboard aggregates using SQL aggregation only."""

    timezone = ZoneInfo(timezone_name)
    start, end, days = inclusive_date_bounds(start_date, end_date, timezone)
    previous_start, previous_end = previous_same_length_bounds(start_date, end_date, timezone)

    total_rows = await connection.fetch(
        """
        SELECT direction, COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE ts >= $1 AND ts < $2 AND currency = 'RUB'
        GROUP BY direction
        """,
        start,
        end,
    )
    totals = {"expense": Decimal("0.00"), "income": Decimal("0.00")}
    for row in total_rows:
        direction = str(row["direction"])
        if direction in totals:
            totals[direction] = as_decimal(row["total"])

    previous_row = await connection.fetchrow(
        """
        SELECT COUNT(*) AS transaction_count,
               COALESCE(SUM(amount) FILTER (WHERE direction = 'expense'), 0) AS expense
        FROM transactions
        WHERE ts >= $1 AND ts < $2 AND currency = 'RUB'
        """,
        previous_start,
        previous_end,
    )
    previous_period = None
    if previous_row is not None and int(previous_row["transaction_count"]) > 0:
        previous_period = PreviousPeriod(expense=as_decimal(previous_row["expense"]))

    category_rows = await connection.fetch(
        """
        SELECT category, SUM(amount) AS expense
        FROM transactions
        WHERE ts >= $1 AND ts < $2
          AND currency = 'RUB' AND direction = 'expense'
        GROUP BY category
        ORDER BY expense DESC, category
        """,
        start,
        end,
    )
    expense_total = totals["expense"]
    categories = tuple(
        CategoryAggregate(
            category=str(row["category"]),
            expense=as_decimal(row["expense"]),
            share=(
                0.0 if expense_total == 0 else float(as_decimal(row["expense"]) / expense_total)
            ),
        )
        for row in category_rows
    )

    bucket_rows = await connection.fetch(
        bucket_query(bucket_granularity(days)), start, end, timezone_name
    )
    buckets = tuple(
        BucketAggregate(bucket=_record_date(row["bucket"]), expense=as_decimal(row["expense"]))
        for row in bucket_rows
    )

    budgets: tuple[BudgetAggregate, ...] = ()
    if is_exact_calendar_month(start_date, end_date):
        budget_rows = await connection.fetch(
            """
            SELECT b.category, b.monthly_limit,
                   COALESCE(SUM(t.amount) FILTER (WHERE t.direction = 'expense'), 0) AS spent
            FROM budgets AS b
            LEFT JOIN transactions AS t
              ON t.category = b.category
             AND t.currency = 'RUB'
             AND t.ts >= $1 AND t.ts < $2
            GROUP BY b.category, b.monthly_limit
            ORDER BY b.category
            """,
            start,
            end,
        )
        budgets = tuple(_budget_aggregate(row) for row in budget_rows)

    return FinanceSummary(
        start_date=start_date,
        end_date=end_date,
        totals=FinanceTotals(expense=totals["expense"], income=totals["income"]),
        previous_period=previous_period,
        by_category=categories,
        by_bucket=buckets,
        budgets=budgets,
    )


async def fetch_transactions_page(
    connection: FinanceConnection,
    timezone_name: str,
    start_date: date,
    end_date: date,
    *,
    category: str | None = None,
    cursor: str | None = None,
    limit: int = PAGE_LIMIT,
) -> TransactionsPage:
    """Fetch one keyset-paginated transaction page for the dashboard."""

    if isinstance(limit, bool) or limit < 1 or limit > PAGE_LIMIT:
        raise ValueError("finance page limit must be between 1 and 50")
    if category is not None and category not in TX_CATEGORIES:
        raise ValueError("unknown transaction category")
    timezone = ZoneInfo(timezone_name)
    start, end, _ = inclusive_date_bounds(start_date, end_date, timezone)
    cursor_value = decode_cursor(cursor) if cursor is not None else None
    rows = await connection.fetch(
        """
        SELECT id, ts, amount, direction, category, description
        FROM transactions
        WHERE ts >= $1 AND ts < $2
          AND currency = 'RUB'
          AND ($3::text IS NULL OR category = $3)
          AND ($4::timestamptz IS NULL OR (ts, id) < ($4::timestamptz, $5::bigint))
        ORDER BY ts DESC, id DESC
        LIMIT $6
        """,
        start,
        end,
        category,
        cursor_value[0] if cursor_value else None,
        cursor_value[1] if cursor_value else None,
        limit + 1,
    )
    visible_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = visible_rows[-1]
        next_cursor = encode_cursor(last["ts"], int(last["id"]))
    return TransactionsPage(
        items=tuple(_transaction_item(row, timezone) for row in visible_rows),
        next_cursor=next_cursor,
    )


async def fetch_top_transactions(
    connection: FinanceConnection,
    timezone_name: str,
    start_date: date,
    end_date: date,
    *,
    limit: int = 5,
) -> tuple[TopExpenseTransaction, ...]:
    """Fetch the largest RUB expenses for the aggregate-only AI input."""

    if isinstance(limit, bool) or limit < 1 or limit > 5:
        raise ValueError("finance top transaction limit must be between 1 and 5")
    timezone = ZoneInfo(timezone_name)
    start, end, _ = inclusive_date_bounds(start_date, end_date, timezone)
    rows = await connection.fetch(
        """
        SELECT amount, category, description
        FROM transactions
        WHERE ts >= $1 AND ts < $2
          AND currency = 'RUB' AND direction = 'expense'
        ORDER BY amount DESC, id DESC
        LIMIT $3
        """,
        start,
        end,
        limit,
    )
    return tuple(
        TopExpenseTransaction(
            amount=as_decimal(row["amount"]),
            category=str(row["category"]),
            description=str(row["description"]),
        )
        for row in rows
    )


async def delete_transaction(connection: FinanceConnection, transaction_id: int) -> bool:
    """Delete one visible transaction under the caller's RLS transaction."""

    result = await connection.execute("DELETE FROM transactions WHERE id = $1", transaction_id)
    return result == "DELETE 1"


def bucket_granularity(days: int) -> BucketGranularity:
    """Choose local day, ISO Monday week, or calendar month buckets."""

    if days < 1 or days > 366:
        raise InvalidFinanceRange
    if days <= 31:
        return "day"
    if days <= 180:
        return "week"
    return "month"


def bucket_query(kind: BucketGranularity) -> str:
    """Return the expense-only SQL aggregate for a bucket granularity."""

    if kind == "day":
        expression = "(ts AT TIME ZONE $3)::date"
    elif kind == "week":
        expression = "date_trunc('week', ts AT TIME ZONE $3)::date"
    else:
        expression = "date_trunc('month', ts AT TIME ZONE $3)::date"
    return f"""
        SELECT {expression} AS bucket, SUM(amount) AS expense
        FROM transactions
        WHERE ts >= $1 AND ts < $2
          AND currency = 'RUB' AND direction = 'expense'
        GROUP BY bucket
        ORDER BY bucket
        """


def is_exact_calendar_month(start_date: date, end_date: date) -> bool:
    """Return whether an inclusive range covers exactly one calendar month."""

    if start_date.day != 1:
        return False
    if start_date.month == 12:
        next_month = date(start_date.year + 1, 1, 1)
    else:
        next_month = date(start_date.year, start_date.month + 1, 1)
    return end_date == next_month - timedelta(days=1)


def encode_cursor(timestamp: datetime, transaction_id: int) -> str:
    """Encode a versioned URL-safe cursor without user identity."""

    if timestamp.tzinfo is None or isinstance(transaction_id, bool) or transaction_id <= 0:
        raise InvalidFinanceCursor
    utc_timestamp = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    payload = json.dumps(
        {"v": 1, "ts": utc_timestamp, "id": transaction_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(raw: str) -> tuple[datetime, int]:
    """Decode and strictly validate a finance cursor."""

    allowed_characters = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    if (
        not raw
        or len(raw) > MAX_CURSOR_LENGTH
        or not raw.isascii()
        or any(character not in allowed_characters for character in raw)
    ):
        raise InvalidFinanceCursor
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = base64.b64decode(padded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(payload).decode().rstrip("=") != raw:
            raise InvalidFinanceCursor
        data = json.loads(payload.decode("utf-8"))
    except (binascii.Error, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidFinanceCursor from exc
    if not isinstance(data, dict) or set(data) != {"v", "ts", "id"}:
        raise InvalidFinanceCursor
    if isinstance(data["v"], bool) or not isinstance(data["v"], int) or data["v"] != 1:
        raise InvalidFinanceCursor
    raw_id, raw_timestamp = data["id"], data["ts"]
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
        raise InvalidFinanceCursor
    if not isinstance(raw_timestamp, str) or not raw_timestamp.endswith("Z"):
        raise InvalidFinanceCursor
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise InvalidFinanceCursor from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise InvalidFinanceCursor
    if timestamp.isoformat().replace("+00:00", "Z") != raw_timestamp:
        raise InvalidFinanceCursor
    return timestamp.astimezone(UTC), raw_id


def _budget_aggregate(row: Any) -> BudgetAggregate:
    limit = as_decimal(row["monthly_limit"])
    spent = as_decimal(row["spent"])
    return BudgetAggregate(
        category=str(row["category"]),
        limit=limit,
        spent=spent,
        ratio=float(spent / limit),
    )


def _record_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _transaction_item(row: Any, timezone: ZoneInfo) -> TransactionItem:
    direction = str(row["direction"])
    if direction not in {"expense", "income"}:
        raise ValueError("unknown transaction direction")
    return TransactionItem(
        id=int(row["id"]),
        ts_local=row["ts"].astimezone(timezone),
        amount=as_decimal(row["amount"]),
        direction=direction,  # type: ignore[arg-type]
        category=str(row["category"]),
        description=str(row["description"]),
    )
