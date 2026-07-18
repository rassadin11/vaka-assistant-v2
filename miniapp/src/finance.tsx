import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import {
  ApiError,
  deleteTransaction,
  fetchFinanceAiSummary,
  fetchFinanceSummary,
  fetchTransactions,
  type FinanceCategory,
  type FinanceAiSummary,
  type FinanceSummary,
  type FinanceTransaction,
  type TransactionsPage
} from "./api";
import { todayInTimezone } from "./calendar";

export interface FinanceClient {
  summary(token: string, from: string, to: string): Promise<FinanceSummary>;
  aiSummary(token: string, from: string, to: string): Promise<FinanceAiSummary>;
  transactions(
    token: string,
    from: string,
    to: string,
    category?: FinanceCategory,
    cursor?: string
  ): Promise<TransactionsPage>;
  delete(token: string, id: number): Promise<void>;
}

const defaultClient: FinanceClient = {
  summary: fetchFinanceSummary,
  aiSummary: fetchFinanceAiSummary,
  transactions: fetchTransactions,
  delete: deleteTransaction
};

const categoryMeta: Record<FinanceCategory, { emoji: string; label: string }> = {
  food: { emoji: "🍔", label: "Еда" },
  transport: { emoji: "🚕", label: "Транспорт" },
  housing: { emoji: "🏠", label: "Жильё" },
  health: { emoji: "💊", label: "Здоровье" },
  entertainment: { emoji: "🎭", label: "Развлечения" },
  shopping: { emoji: "🛍", label: "Покупки" },
  subscriptions: { emoji: "📱", label: "Подписки" },
  salary: { emoji: "💼", label: "Зарплата" },
  other: { emoji: "📦", label: "Прочее" }
};

type PeriodKind = "today" | "week" | "month" | "previous" | "custom";
type ViewState = "loading" | "content" | "empty" | "offline" | "unauthorized" | "error";
type AiCardState = "loading" | "ready" | "empty" | "budget_exhausted" | "hidden";
type DirectionFilter = "all" | "expense" | "income";
type SortOrder = "newest" | "oldest";

interface DateRange {
  from: string;
  to: string;
}

export function currentMonthRange(timezone: string, now = new Date()): DateRange {
  const today = todayInTimezone(timezone, now);
  const [year, month] = today.split("-").map(Number);
  return monthRange(year, month);
}

export function periodRange(kind: Exclude<PeriodKind, "custom">, timezone: string): DateRange {
  const today = todayInTimezone(timezone);
  const [year, month, day] = today.split("-").map(Number);
  if (kind === "today") return { from: today, to: today };
  if (kind === "week") {
    const value = new Date(Date.UTC(year, month - 1, day));
    const mondayOffset = (value.getUTCDay() + 6) % 7;
    value.setUTCDate(value.getUTCDate() - mondayOffset);
    return { from: isoDate(value), to: today };
  }
  if (kind === "previous") return monthRange(year, month - 1);
  return monthRange(year, month);
}

export function FinanceScreen({
  token,
  timezone,
  refreshSession,
  client = defaultClient
}: {
  token: string;
  timezone: string;
  refreshSession: () => Promise<string>;
  client?: FinanceClient;
}) {
  const initial = currentMonthRange(timezone);
  const [periodKind, setPeriodKind] = useState<PeriodKind>("month");
  const [range, setRange] = useState<DateRange>(initial);
  const [customFrom, setCustomFrom] = useState(initial.from);
  const [customTo, setCustomTo] = useState(initial.to);
  const [summary, setSummary] = useState<FinanceSummary | null>(null);
  const [items, setItems] = useState<FinanceTransaction[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [category, setCategory] = useState<FinanceCategory | undefined>();
  const [directionFilter, setDirectionFilter] = useState<DirectionFilter>("all");
  const [sortOrder, setSortOrder] = useState<SortOrder>("newest");
  const [state, setState] = useState<ViewState>("loading");
  const [message, setMessage] = useState("");
  const [notice, setNotice] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [aiState, setAiState] = useState<AiCardState>("loading");
  const [aiText, setAiText] = useState<string | null>(null);
  const tokenRef = useRef(token);
  const deletingRef = useRef(false);
  const requestVersion = useRef(0);
  const aiRequestVersion = useRef(0);

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  async function load(retriedAfterRefresh = false) {
    if (!navigator.onLine) {
      setState("offline");
      return;
    }
    const version = ++requestVersion.current;
    setState("loading");
    setNotice("");
    try {
      const [nextSummary, page] = await Promise.all([
        client.summary(tokenRef.current, range.from, range.to),
        client.transactions(tokenRef.current, range.from, range.to, category)
      ]);
      if (version !== requestVersion.current) return;
      setSummary(nextSummary);
      setItems(uniqueTransactions(page.items));
      setNextCursor(page.next_cursor);
      setState(page.items.length === 0 ? "empty" : "content");
    } catch (error) {
      if (version !== requestVersion.current) return;
      if (error instanceof ApiError && error.status === 401 && !retriedAfterRefresh) {
        try {
          tokenRef.current = await refreshSession();
          await load(true);
        } catch {
          setState("unauthorized");
        }
      } else if (error instanceof ApiError && error.status === 401) {
        setState("unauthorized");
      } else {
        setMessage(error instanceof Error ? error.message : "Не удалось загрузить финансы.");
        setState("error");
      }
    }
  }

  useEffect(() => {
    void load();
  }, [range.from, range.to, category]);

  async function loadAiSummary(retriedAfterRefresh = false) {
    const version = ++aiRequestVersion.current;
    setAiState("loading");
    try {
      const result = await client.aiSummary(tokenRef.current, range.from, range.to);
      if (version !== aiRequestVersion.current) return;
      if (result.status === "ready") {
        setAiText(result.summary ?? null);
        setAiState("ready");
      } else if (result.status === "empty" || result.status === "budget_exhausted") {
        setAiText(null);
        setAiState(result.status);
      } else {
        setAiText(null);
        setAiState("hidden");
      }
    } catch (error) {
      if (version !== aiRequestVersion.current) return;
      if (error instanceof ApiError && error.status === 401 && !retriedAfterRefresh) {
        try {
          tokenRef.current = await refreshSession();
          await loadAiSummary(true);
          return;
        } catch {
          setAiState("hidden");
        }
      } else {
        setAiState("hidden");
      }
    }
  }

  useEffect(() => {
    void loadAiSummary();
  }, [range.from, range.to]);

  function choosePeriod(kind: Exclude<PeriodKind, "custom">) {
    setPeriodKind(kind);
    setRange(periodRange(kind, timezone));
  }

  function applyCustom() {
    const days = inclusiveDays(customFrom, customTo);
    if (days < 1 || days > 366) {
      setMessage("Период должен содержать от 1 до 366 дней.");
      setState("error");
      return;
    }
    setPeriodKind("custom");
    setRange({ from: customFrom, to: customTo });
  }

  async function loadMore(retriedAfterRefresh = false) {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await client.transactions(
        tokenRef.current,
        range.from,
        range.to,
        category,
        nextCursor
      );
      setItems((current) => uniqueTransactions([...current, ...page.items]));
      setNextCursor(page.next_cursor);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && !retriedAfterRefresh) {
        try {
          tokenRef.current = await refreshSession();
          setLoadingMore(false);
          await loadMore(true);
          return;
        } catch {
          setState("unauthorized");
        }
      } else if (error instanceof ApiError && error.status === 401) {
        setState("unauthorized");
      } else {
        setMessage(error instanceof Error ? error.message : "Не удалось загрузить ещё.");
        setState("error");
      }
    } finally {
      setLoadingMore(false);
    }
  }

  async function remove(item: FinanceTransaction) {
    if (deletingRef.current || !window.confirm(deleteConfirmation(item))) return;
    deletingRef.current = true;
    setDeleting(item.id);
    setNotice("");
    try {
      await client.delete(tokenRef.current, item.id);
      await load();
      await loadAiSummary();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        try {
          tokenRef.current = await refreshSession();
          setNotice("Сессия обновлена. Нажмите «Удалить» ещё раз.");
        } catch {
          setState("unauthorized");
        }
      } else {
        setMessage(error instanceof Error ? error.message : "Не удалось удалить транзакцию.");
        setState("error");
      }
    } finally {
      deletingRef.current = false;
      setDeleting(null);
    }
  }

  const visibleItems = useMemo(() => {
    const filtered = directionFilter === "all"
      ? items
      : items.filter((item) => item.direction === directionFilter);
    const factor = sortOrder === "newest" ? -1 : 1;
    return [...filtered].sort((left, right) => factor * left.ts_local.localeCompare(right.ts_local));
  }, [items, directionFilter, sortOrder]);

  return (
    <section class="finance" aria-label="Финансовый дашборд">
      <PeriodSelector
        kind={periodKind}
        customFrom={customFrom}
        customTo={customTo}
        setCustomFrom={setCustomFrom}
        setCustomTo={setCustomTo}
        choose={choosePeriod}
        openCustom={() => setPeriodKind("custom")}
        applyCustom={applyCustom}
      />
      {state === "loading" ? <FinanceSkeleton /> : null}
      {state === "offline" ? <FinanceState title="Нет соединения" message="Проверьте интернет и попробуйте снова." retry={load} /> : null}
      {state === "unauthorized" ? <FinanceState title="Сессия закончилась" message="Закройте и заново откройте Mini App." /> : null}
      {state === "error" ? <FinanceState title="Не удалось загрузить" message={message} retry={load} /> : null}
      {(state === "content" || state === "empty") && summary ? (
        <>
          <SummaryCard data={summary} />
          {summary.by_category.length > 0 ? (
            <CategoryDonut data={summary} selected={category} onSelect={setCategory} />
          ) : null}
          {category ? (
            <button class="filter-chip" type="button" onClick={() => setCategory(undefined)}>
              × {categoryMeta[category].label}
            </button>
          ) : null}
          {summary.by_bucket.length > 1 ? <BucketChart data={summary} /> : null}
          <AiSummaryCard state={aiState} text={aiText} />
          {summary.budgets.length > 0 ? <Budgets data={summary} /> : null}
          {notice ? <p class="session-notice" role="status">{notice}</p> : null}
          {state === "empty" ? (
            <p class="finance-empty">Пока нет трат за этот период</p>
          ) : (
            <>
              <TransactionControls
                direction={directionFilter}
                onDirection={setDirectionFilter}
                sort={sortOrder}
                onSort={setSortOrder}
                category={category}
                onCategory={setCategory}
              />
              {visibleItems.length === 0 ? (
                <p class="finance-empty">Нет транзакций по выбранному фильтру</p>
              ) : (
                <TransactionList items={visibleItems} deleting={deleting} remove={remove} />
              )}
            </>
          )}
          {nextCursor ? (
            <button class="load-more" type="button" disabled={loadingMore} onClick={() => void loadMore()}>
              {loadingMore ? "Загружаем…" : "Показать ещё"}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function PeriodSelector({
  kind,
  customFrom,
  customTo,
  setCustomFrom,
  setCustomTo,
  choose,
  openCustom,
  applyCustom
}: {
  kind: PeriodKind;
  customFrom: string;
  customTo: string;
  setCustomFrom: (value: string) => void;
  setCustomTo: (value: string) => void;
  choose: (kind: Exclude<PeriodKind, "custom">) => void;
  openCustom: () => void;
  applyCustom: () => void;
}) {
  const chips: Array<[Exclude<PeriodKind, "custom">, string]> = [
    ["today", "Сегодня"], ["week", "Неделя"], ["month", "Месяц"], ["previous", "Прошлый месяц"]
  ];
  return <section class="period-selector" aria-label="Период">
    <div class="period-chips">
      {chips.map(([value, label]) => <button type="button" aria-pressed={kind === value} onClick={() => choose(value)}>{label}</button>)}
      <button type="button" aria-pressed={kind === "custom"} onClick={openCustom}>Период…</button>
    </div>
    {kind === "custom" ? <div class="custom-period">
      <label>С<input aria-label="Начало периода" type="date" value={customFrom} onInput={(event) => setCustomFrom(event.currentTarget.value)} /></label>
      <label>По<input aria-label="Конец периода" type="date" value={customTo} onInput={(event) => setCustomTo(event.currentTarget.value)} /></label>
      <button type="button" onClick={applyCustom}>Показать</button>
    </div> : null}
  </section>;
}

function SummaryCard({ data }: { data: FinanceSummary }) {
  const current = Number(data.totals.expense);
  const previous = data.prev_period ? Number(data.prev_period.expense) : null;
  const comparison = previous !== null && previous > 0 ? ((current - previous) / previous) * 100 : null;
  return <section class="finance-card totals-card" aria-labelledby="totals-title">
    <h2 id="totals-title">За период</h2>
    <strong class="expense-total">{rubles(data.totals.expense)}</strong>
    <span>Расходы</span>
    {Number(data.totals.income) > 0 ? <p class="income-total">Доходы: {rubles(data.totals.income)}</p> : null}
    {comparison !== null ? <p class={comparison <= 0 ? "comparison-good" : "comparison-bad"}>
      {comparison <= 0 ? "↓" : "↑"} {Math.abs(comparison).toFixed(0)}% к прошлому периоду
    </p> : null}
  </section>;
}

function AiSummaryCard({ state, text }: { state: AiCardState; text: string | null }) {
  if (state === "hidden") return null;
  return <section class="finance-card ai-summary-card" aria-labelledby="ai-summary-title" aria-live="polite">
    <h2 id="ai-summary-title">AI-резюме</h2>
    {state === "loading" ? <div class="ai-summary-skeleton" aria-label="Загружаем AI-резюме" /> : null}
    {state === "ready" && text ? <p>{text}</p> : null}
    {state === "empty" ? <p>Резюме появится после первых трат</p> : null}
    {state === "budget_exhausted" ? <p>Дневной лимит ассистента исчерпан — резюме будет завтра</p> : null}
  </section>;
}

function CategoryDonut({ data, selected, onSelect }: { data: FinanceSummary; selected?: FinanceCategory; onSelect: (value: FinanceCategory) => void }) {
  let offset = 0;
  return <section class="finance-card category-card" aria-labelledby="category-title">
    <h2 id="category-title">По категориям</h2>
    <div class="donut-wrap">
      <svg class="donut" viewBox="0 0 120 120" role="img" aria-label="Диаграмма расходов по категориям">
        <title>Расходы по категориям</title>
        <circle class="donut-base" cx="60" cy="60" r="45" />
        {data.by_category.map((item) => {
          const length = item.share * 282.743;
          const segment = <circle
            class={`donut-segment${selected && selected !== item.category ? " donut-muted" : ""}`}
            cx="60" cy="60" r="45"
            stroke={`var(--category-${item.category})`}
            stroke-dasharray={`${length} ${282.743 - length}`}
            stroke-dashoffset={-offset}
          />;
          offset += length;
          return segment;
        })}
      </svg>
      <div class="donut-center" aria-hidden="true">
        <span>Расходы</span>
        <strong>{rubles(data.totals.expense)}</strong>
      </div>
    </div>
    <ul class="category-legend">
      {data.by_category.map((item) => <li>
        <button type="button" aria-pressed={selected === item.category} onClick={() => onSelect(item.category)}>
          <span>{categoryMeta[item.category].emoji} {categoryMeta[item.category].label}</span>
          <strong>{rubles(item.expense)} · {(item.share * 100).toFixed(0)}%</strong>
        </button>
      </li>)}
    </ul>
  </section>;
}

function BucketChart({ data }: { data: FinanceSummary }) {
  const buckets = data.by_bucket;
  const count = buckets.length;
  const maximum = Math.max(...buckets.map((item) => Number(item.expense)), 1);
  const width = 340;
  const height = 170;
  const padX = 26;
  const baseY = 132;
  const topY = 24;
  const plotWidth = width - padX * 2;
  const pointX = (index: number) => (count === 1 ? width / 2 : padX + (plotWidth * index) / (count - 1));
  const pointY = (value: number) => baseY - (value / maximum) * (baseY - topY);
  const points = buckets.map((item, index) => ({
    x: pointX(index),
    y: pointY(Number(item.expense)),
    item
  }));
  const line = points.map((point) => `${round(point.x)},${round(point.y)}`).join(" ");
  const area = count > 1
    ? `M ${round(points[0].x)},${baseY} ${points.map((point) => `L ${round(point.x)},${round(point.y)}`).join(" ")} L ${round(points[count - 1].x)},${baseY} Z`
    : "";
  const labelStep = Math.ceil(count / 6);
  const showValues = count <= 2;
  return <section class="finance-card bucket-card" aria-labelledby="bucket-title">
    <h2 id="bucket-title">Динамика расходов</h2>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="График динамики расходов" preserveAspectRatio="xMidYMid meet">
      <title>Расходы по времени</title>
      <defs>
        <linearGradient id="bucket-area-gradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.25" />
          <stop offset="100%" stop-color="var(--accent)" stop-opacity="0" />
        </linearGradient>
      </defs>
      {area ? <path class="bucket-area" d={area} /> : null}
      {count > 1 ? <polyline class="bucket-line" points={line} /> : null}
      {points.map((point, index) => <g>
        <circle class="bucket-dot" cx={round(point.x)} cy={round(point.y)} r="3.4"><title>{point.item.bucket}: {rubles(point.item.expense)}</title></circle>
        {showValues ? <text class="bucket-value" x={round(point.x)} y={round(point.y) - 11} text-anchor="middle">{rubles(point.item.expense)}</text> : null}
        {index % labelStep === 0 || index === count - 1 ? <text x={round(point.x)} y={height - 8} text-anchor="middle">{shortBucket(point.item.bucket)}</text> : null}
      </g>)}
    </svg>
    <ul class="visually-hidden" aria-label="Данные графика расходов">
      {buckets.map((item) => (
        <li>{item.bucket}: {rubles(item.expense)}</li>
      ))}
    </ul>
  </section>;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

function Budgets({ data }: { data: FinanceSummary }) {
  return <section class="finance-card budgets" aria-labelledby="budgets-title">
    <h2 id="budgets-title">Бюджеты месяца</h2>
    {data.budgets.map((budget) => <div class={`budget budget-${budget.ratio >= 1 ? "over" : budget.ratio >= 0.8 ? "warning" : "normal"}`}>
      <p><span>{categoryMeta[budget.category].emoji} {categoryMeta[budget.category].label}</span><strong>{Math.round(budget.ratio * 100)}%</strong></p>
      <div class="budget-track" role="progressbar" aria-label={`Бюджет: ${categoryMeta[budget.category].label}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(budget.ratio * 100)}><i style={{ width: `${Math.min(budget.ratio * 100, 100)}%` }} /></div>
      <small>Потрачено {rubles(budget.spent)} из {rubles(budget.limit)}</small>
    </div>)}
  </section>;
}

function TransactionControls({
  direction,
  onDirection,
  sort,
  onSort,
  category,
  onCategory
}: {
  direction: DirectionFilter;
  onDirection: (value: DirectionFilter) => void;
  sort: SortOrder;
  onSort: (value: SortOrder) => void;
  category: FinanceCategory | undefined;
  onCategory: (value: FinanceCategory | undefined) => void;
}) {
  const directions: Array<[DirectionFilter, string]> = [
    ["all", "Все"], ["expense", "Расходы"], ["income", "Доходы"]
  ];
  const categories = Object.keys(categoryMeta) as FinanceCategory[];
  return <section class="transaction-controls" aria-label="Фильтры транзакций">
    <div class="direction-chips" role="group" aria-label="Тип операции">
      {directions.map(([value, label]) => (
        <button type="button" aria-pressed={direction === value} onClick={() => onDirection(value)}>{label}</button>
      ))}
    </div>
    <div class="control-row">
      <label class="control-select">
        <span class="visually-hidden">Категория</span>
        <select
          value={category ?? ""}
          onChange={(event) => {
            const value = event.currentTarget.value;
            onCategory(value === "" ? undefined : (value as FinanceCategory));
          }}
        >
          <option value="">Все категории</option>
          {categories.map((value) => (
            <option value={value}>{categoryMeta[value].emoji} {categoryMeta[value].label}</option>
          ))}
        </select>
        <span class="select-chevron" aria-hidden="true"><ChevronDownIcon /></span>
      </label>
      <button
        type="button"
        class="sort-toggle"
        aria-label={sort === "newest" ? "Сортировка: сначала новые" : "Сортировка: сначала старые"}
        onClick={() => onSort(sort === "newest" ? "oldest" : "newest")}
      >
        {sort === "newest" ? "↓ Сначала новые" : "↑ Сначала старые"}
      </button>
    </div>
  </section>;
}

function TransactionList({ items, deleting, remove }: { items: FinanceTransaction[]; deleting: number | null; remove: (item: FinanceTransaction) => Promise<void> }) {
  return <section class="transaction-list" aria-labelledby="transactions-title">
    <h2 id="transactions-title">Транзакции</h2>
    <ul>{items.map((item) => <li>
      <div class="transaction-category" aria-hidden="true">{categoryMeta[item.category].emoji}</div>
      <div class="transaction-copy"><strong>{item.description || categoryMeta[item.category].label}</strong><span>{localDateTime(item.ts_local)} · {categoryMeta[item.category].label}</span></div>
      <span class={`transaction-amount amount-${item.direction}`}>{item.direction === "expense" ? "−" : "+"}{rubles(item.amount)}</span>
      <button type="button" class="transaction-delete" aria-label="Удалить транзакцию" aria-busy={deleting === item.id} disabled={deleting !== null} onClick={() => void remove(item)}><TrashIcon /></button>
    </li>)}</ul>
  </section>;
}

function FinanceState({ title, message, retry }: { title: string; message?: string; retry?: () => Promise<void> }) {
  return <section class="finance-state" aria-live="polite"><h2>{title}</h2>{message ? <p>{message}</p> : null}{retry ? <button type="button" onClick={() => void retry()}>Повторить</button> : null}</section>;
}

function FinanceSkeleton() {
  return <div class="finance-skeleton" aria-busy="true" aria-label="Загружаем финансы">
    <section class="finance-card">
      <span class="skeleton skeleton-h2" />
      <span class="skeleton skeleton-total" />
    </section>
    <section class="finance-card">
      <span class="skeleton skeleton-h2" />
      <span class="skeleton skeleton-donut" />
    </section>
    <section class="finance-card">
      <span class="skeleton skeleton-h2" />
      <span class="skeleton skeleton-chart" />
    </section>
    <section class="finance-card">
      <span class="skeleton skeleton-h2" />
      <span class="skeleton skeleton-row" />
      <span class="skeleton skeleton-row" />
      <span class="skeleton skeleton-row" />
    </section>
  </div>;
}

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  );
}

function ChevronDownIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function uniqueTransactions(items: FinanceTransaction[]): FinanceTransaction[] {
  return [...new Map(items.map((item) => [item.id, item])).values()];
}

function deleteConfirmation(item: FinanceTransaction): string {
  const noun = item.direction === "expense" ? "трату" : "доход";
  const description = item.description || categoryMeta[item.category].label.toLowerCase();
  const date = localShortDate(item.ts_local);
  return `Удалить ${noun} ${Number(item.amount).toLocaleString("ru-RU")} ₽, ${description}, ${date}?`;
}

function monthRange(year: number, month: number): DateRange {
  const first = new Date(Date.UTC(year, month - 1, 1));
  const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0));
  return { from: isoDate(first), to: isoDate(last) };
}

function inclusiveDays(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000) + 1;
}

function isoDate(value: Date): string {
  return `${value.getUTCFullYear()}-${String(value.getUTCMonth() + 1).padStart(2, "0")}-${String(value.getUTCDate()).padStart(2, "0")}`;
}

function rubles(value: string): string {
  return `${Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽`;
}

function localDateTime(value: string): string {
  const [datePart, timePart = ""] = value.split("T");
  const [year, month, day] = datePart.split("-");
  return `${day}.${month}.${year}, ${timePart.slice(0, 5)}`;
}

function localShortDate(value: string): string {
  const [, month, day] = value.slice(0, 10).split("-");
  return `${day}.${month}`;
}

function shortBucket(value: string): string {
  const [, month, day] = value.split("-");
  return `${day}.${month}`;
}
