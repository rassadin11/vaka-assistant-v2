export interface Me {
  timezone: string;
  plan: string;
}

export interface CalendarOccurrence {
  id: number;
  kind: "reminder" | "agent_task";
  text: string;
  time_local: string;
  occurs_at: string;
  recurring: boolean;
  repeat_human: string | null;
  status: "active" | "done";
  truncated: boolean;
}

export interface CalendarData {
  days: Record<string, CalendarOccurrence[]>;
}

export interface CreatedReminder {
  id: number;
  text: string;
  next_run_at: string;
  status: "active";
}

export const financeCategories = [
  "food",
  "transport",
  "housing",
  "health",
  "entertainment",
  "shopping",
  "subscriptions",
  "salary",
  "other"
] as const;

export type FinanceCategory = (typeof financeCategories)[number];
export type TransactionDirection = "expense" | "income";

export interface FinanceSummary {
  period: { from: string; to: string };
  totals: { expense: string; income: string };
  prev_period: { expense: string } | null;
  by_category: Array<{ category: FinanceCategory; expense: string; share: number }>;
  by_bucket: Array<{ bucket: string; expense: string }>;
  budgets: Array<{
    category: FinanceCategory;
    limit: string;
    spent: string;
    ratio: number;
  }>;
}

export interface FinanceTransaction {
  id: number;
  ts_local: string;
  amount: string;
  direction: TransactionDirection;
  category: FinanceCategory;
  description: string;
}

export interface TransactionsPage {
  items: FinanceTransaction[];
  next_cursor: string | null;
}

export interface FinanceAiSummary {
  status: "ready" | "empty" | "budget_exhausted" | "unavailable";
  summary?: string | null;
  cached?: boolean;
}

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string
  ) {
    super(message);
  }
}

export async function authenticate(initData: string): Promise<string> {
  const response = await fetch("/app/api/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: initData })
  });
  const payload = (await response.json()) as { token?: unknown } & ErrorEnvelope;
  if (!response.ok || typeof payload.token !== "string") {
    throw apiError(response.status, payload);
  }
  return payload.token;
}

export async function fetchMe(token: string): Promise<Me> {
  const response = await fetch("/app/api/me", {
    headers: { Authorization: `Bearer ${token}` }
  });
  const payload = (await response.json()) as Partial<Me> & ErrorEnvelope;
  if (!response.ok || typeof payload.timezone !== "string" || typeof payload.plan !== "string") {
    throw apiError(response.status, payload);
  }
  return { timezone: payload.timezone, plan: payload.plan };
}

export async function fetchCalendar(
  token: string,
  from: string,
  to: string
): Promise<CalendarData> {
  const params = new URLSearchParams({ from, to });
  const response = await fetch(`/app/api/calendar?${params}`, {
    headers: authorization(token)
  });
  const payload = (await response.json()) as Partial<CalendarData> & ErrorEnvelope;
  if (!response.ok || !payload.days || typeof payload.days !== "object") {
    throw apiError(response.status, payload);
  }
  return { days: payload.days };
}

export async function createReminder(
  token: string,
  text: string,
  remindAtLocal: string
): Promise<CreatedReminder> {
  const response = await fetch("/app/api/reminders", {
    method: "POST",
    headers: { ...authorization(token), "Content-Type": "application/json" },
    body: JSON.stringify({ text, remind_at_local: remindAtLocal })
  });
  const payload = (await response.json()) as Partial<CreatedReminder> & ErrorEnvelope;
  if (
    !response.ok ||
    typeof payload.id !== "number" ||
    typeof payload.text !== "string" ||
    typeof payload.next_run_at !== "string"
  ) {
    throw apiError(response.status, payload);
  }
  return {
    id: payload.id,
    text: payload.text,
    next_run_at: payload.next_run_at,
    status: "active"
  };
}

export async function cancelScheduled(token: string, id: number): Promise<void> {
  const response = await fetch(`/app/api/scheduled/${id}`, {
    method: "DELETE",
    headers: authorization(token)
  });
  const payload = (await response.json()) as { status?: unknown } & ErrorEnvelope;
  if (!response.ok || payload.status !== "cancelled") {
    throw apiError(response.status, payload);
  }
}

export async function fetchFinanceSummary(
  token: string,
  from: string,
  to: string
): Promise<FinanceSummary> {
  const params = new URLSearchParams({ from, to });
  const response = await fetch(`/app/api/finance/summary?${params}`, {
    headers: authorization(token)
  });
  const payload: unknown = await response.json();
  if (!response.ok || !isFinanceSummary(payload)) {
    throw apiError(response.status, asErrorEnvelope(payload));
  }
  return payload;
}

export async function fetchTransactions(
  token: string,
  from: string,
  to: string,
  category?: FinanceCategory,
  cursor?: string
): Promise<TransactionsPage> {
  const params = new URLSearchParams({ from, to });
  if (category !== undefined) params.set("category", category);
  if (cursor !== undefined) params.set("cursor", cursor);
  const response = await fetch(`/app/api/finance/transactions?${params}`, {
    headers: authorization(token)
  });
  const payload: unknown = await response.json();
  if (!response.ok || !isTransactionsPage(payload)) {
    throw apiError(response.status, asErrorEnvelope(payload));
  }
  return payload;
}

export async function fetchFinanceAiSummary(
  token: string,
  from: string,
  to: string
): Promise<FinanceAiSummary> {
  const params = new URLSearchParams({ from, to });
  const response = await fetch(`/app/api/finance/ai-summary?${params}`, {
    headers: authorization(token)
  });
  const payload: unknown = await response.json();
  if (!response.ok || !isFinanceAiSummary(payload)) {
    throw apiError(response.status, asErrorEnvelope(payload));
  }
  return payload;
}

export async function deleteTransaction(token: string, id: number): Promise<void> {
  const response = await fetch(`/app/api/finance/transactions/${id}`, {
    method: "DELETE",
    headers: authorization(token)
  });
  if (response.status === 204) return;
  let payload: unknown = {};
  try {
    payload = await response.json();
  } catch {
    // A non-204 response without JSON is still represented as a public API error.
  }
  throw apiError(response.status, asErrorEnvelope(payload));
}

function authorization(token: string): { Authorization: string } {
  return { Authorization: `Bearer ${token}` };
}

function apiError(status: number, payload: ErrorEnvelope): ApiError {
  return new ApiError(
    status,
    payload.error?.code ?? "unexpected_response",
    payload.error?.message ?? "Сервис вернул некорректный ответ."
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asErrorEnvelope(value: unknown): ErrorEnvelope {
  return isRecord(value) ? (value as ErrorEnvelope) : {};
}

function isMoney(value: unknown): value is string {
  return typeof value === "string" && /^\d+\.\d{2}$/.test(value);
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) === value;
}

function isOffsetDateTime(value: unknown): value is string {
  return typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
    !Number.isNaN(Date.parse(value));
}

function isCategory(value: unknown): value is FinanceCategory {
  return typeof value === "string" && (financeCategories as readonly string[]).includes(value);
}

function isFinanceSummary(value: unknown): value is FinanceSummary {
  if (!isRecord(value) || !isRecord(value.period) || !isRecord(value.totals)) return false;
  if (!isIsoDate(value.period.from) || !isIsoDate(value.period.to)) return false;
  if (!isMoney(value.totals.expense) || !isMoney(value.totals.income)) return false;
  if (
    value.prev_period !== null &&
    (!isRecord(value.prev_period) || !isMoney(value.prev_period.expense))
  ) return false;
  if (!Array.isArray(value.by_category) || !Array.isArray(value.by_bucket) || !Array.isArray(value.budgets)) return false;
  return value.by_category.every((item) =>
    isRecord(item) && isCategory(item.category) && isMoney(item.expense) &&
    typeof item.share === "number" && Number.isFinite(item.share) &&
    item.share >= 0 && item.share <= 1
  ) && value.by_bucket.every((item) =>
    isRecord(item) && isIsoDate(item.bucket) && isMoney(item.expense)
  ) && value.budgets.every((item) =>
    isRecord(item) && isCategory(item.category) && isMoney(item.limit) &&
    isMoney(item.spent) && typeof item.ratio === "number" &&
    Number.isFinite(item.ratio) && item.ratio >= 0
  );
}

function isTransactionsPage(value: unknown): value is TransactionsPage {
  if (!isRecord(value) || !Array.isArray(value.items)) return false;
  if (value.next_cursor !== null && typeof value.next_cursor !== "string") return false;
  return value.items.every((item) =>
    isRecord(item) && Number.isSafeInteger(item.id) && Number(item.id) > 0 &&
    isOffsetDateTime(item.ts_local) &&
    isMoney(item.amount) && (item.direction === "expense" || item.direction === "income") &&
    isCategory(item.category) && typeof item.description === "string"
  );
}

function isFinanceAiSummary(value: unknown): value is FinanceAiSummary {
  if (!isRecord(value) || typeof value.status !== "string") return false;
  if (value.status === "ready") {
    return (
      typeof value.summary === "string" &&
      value.summary.trim().length > 0 &&
      (value.cached === undefined || typeof value.cached === "boolean")
    );
  }
  if (
    value.status === "empty" ||
    value.status === "budget_exhausted" ||
    value.status === "unavailable"
  ) {
    return (
      (value.summary === undefined || value.summary === null) &&
      (value.cached === undefined || typeof value.cached === "boolean")
    );
  }
  return false;
}
