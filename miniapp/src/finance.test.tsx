import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type FinanceAiSummary, type FinanceSummary, type FinanceTransaction } from "./api";
import { FinanceScreen, currentMonthRange, type FinanceClient } from "./finance";

afterEach(() => {
  render(null, document.body);
  vi.restoreAllMocks();
});

const transaction: FinanceTransaction = {
  id: 1,
  ts_local: "2026-07-17T12:00:00+03:00",
  amount: "500.00",
  direction: "expense",
  category: "food",
  description: "Обед"
};

const summary: FinanceSummary = {
  period: { from: "2026-07-01", to: "2026-07-31" },
  totals: { expense: "500.00", income: "1000.00" },
  prev_period: { expense: "600.00" },
  by_category: [{ category: "food", expense: "500.00", share: 1 }],
  by_bucket: [{ bucket: "2026-07-17", expense: "500.00" }],
  budgets: [{ category: "food", limit: "600.00", spent: "500.00", ratio: 0.8333 }]
};

function client(overrides: Partial<FinanceClient> = {}): FinanceClient {
  return {
    summary: vi.fn().mockResolvedValue(summary),
    aiSummary: vi.fn().mockResolvedValue({ status: "ready", summary: "Итоги за период." } satisfies FinanceAiSummary),
    transactions: vi.fn().mockResolvedValue({ items: [transaction], next_cursor: null }),
    delete: vi.fn(),
    ...overrides
  };
}

describe("FinanceScreen", () => {
  it("loads the current month by default and renders accessible charts and budgets", async () => {
    const api = client();
    mount(api);
    await flushEffects();

    const expected = currentMonthRange("UTC");
    expect(api.summary).toHaveBeenCalledWith("token", expected.from, expected.to);
    expect(document.body.textContent).toContain("Доходы");
    expect(document.body.textContent).toContain("Итоги за период.");
    expect(document.body.textContent).toContain("Бюджеты месяца");
    expect(document.querySelectorAll('svg[role="img"]')).toHaveLength(2);
    expect(document.querySelector('[role="progressbar"]')).not.toBeNull();
    const bucketSummary = document.querySelector('[aria-label="Данные графика расходов"]');
    expect(bucketSummary?.textContent).toContain("2026-07-17");
    expect(bucketSummary?.textContent).toContain("500,00");
  });

  it("shows the required empty-state copy", async () => {
    const api = client({
      transactions: vi.fn().mockResolvedValue({ items: [], next_cursor: null })
    });
    mount(api);
    await flushEffects();

    expect(document.body.textContent).toContain("Пока нет трат за этот период");
  });

  it("renders AI summary independently while totals and charts stay visible", async () => {
    let resolveAi: ((value: FinanceAiSummary) => void) | undefined;
    const aiSummary = vi.fn().mockReturnValue(new Promise<FinanceAiSummary>((resolve) => {
      resolveAi = resolve;
    }));
    const api = client({ aiSummary });
    mount(api);
    await flushEffects();

    expect(document.body.textContent).toContain("500,00 ₽");
    expect(document.querySelectorAll('svg[role="img"]')).toHaveLength(2);
    expect(document.querySelector(".ai-summary-skeleton")).not.toBeNull();

      resolveAi?.({ status: "ready", summary: "Итоги за период." });
    await flushEffects();

    expect(document.body.textContent).toContain("Итоги за период.");
  });

  it("shows AI empty and budget-exhausted copy", async () => {
    const api = client({ aiSummary: vi.fn().mockResolvedValue({ status: "empty" }) });
    mount(api);
    await flushEffects();
    expect(document.body.textContent).toContain("Резюме появится после первых трат");

    render(null, document.body);
    const budgetApi = client({ aiSummary: vi.fn().mockResolvedValue({ status: "budget_exhausted" }) });
    mount(budgetApi);
    await flushEffects();
    expect(document.body.textContent).toContain("Дневной лимит ассистента исчерпан — резюме будет завтра");
  });

  it("hides unavailable AI summary without hiding totals", async () => {
    const api = client({ aiSummary: vi.fn().mockResolvedValue({ status: "unavailable" }) });
    mount(api);
    await flushEffects();

    expect(document.body.textContent).toContain("500,00 ₽");
    expect(document.body.textContent).not.toContain("AI-резюме");
  });

  it("refreshes and retries AI summary once after 401", async () => {
    const aiSummary = vi.fn()
      .mockRejectedValueOnce(new ApiError(401, "invalid_session", "Сессия истекла"))
      .mockResolvedValueOnce({ status: "ready", summary: "После обновления." });
    const api = client({ aiSummary });
    const refreshSession = vi.fn().mockResolvedValue("renewed");
    mount(api, refreshSession, "expired");
    await flushEffects();
    await flushEffects();

    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(aiSummary).toHaveBeenCalledTimes(2);
    expect(aiSummary.mock.calls.map((call) => call[0])).toEqual(["expired", "renewed"]);
    expect(document.body.textContent).toContain("После обновления.");
  });

  it("applies a valid custom range and filters by a donut category", async () => {
    const api = client();
    mount(api);
    await flushEffects();

    button("Период…").click();
    await flushEffects();
    input("Начало периода", "2026-01-01");
    input("Конец периода", "2026-01-15");
    await flushEffects();
    button("Показать").click();
    await flushEffects();
    await flushEffects();
    button("🍔 Еда").click();
    await flushEffects();
    await flushEffects();

    expect(api.summary).toHaveBeenCalledWith("token", "2026-01-01", "2026-01-15");
    const calls = vi.mocked(api.transactions).mock.calls;
    expect(calls.at(-1)?.[3]).toBe("food");
    expect(document.body.textContent).toContain("× Еда");
  });

  it("filters transactions by direction and toggles sort order client-side", async () => {
    const income: FinanceTransaction = {
      id: 2,
      ts_local: "2026-07-10T09:00:00+03:00",
      amount: "1000.00",
      direction: "income",
      category: "salary",
      description: "Аванс"
    };
    const transactions = vi.fn().mockResolvedValue({ items: [transaction, income], next_cursor: null });
    const api = client({ transactions });
    mount(api);
    await flushEffects();

    let rows = [...document.querySelectorAll(".transaction-list li")];
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Обед");
    expect(rows[1]?.textContent).toContain("Аванс");

    button("Сначала новые").click();
    await flushEffects();
    rows = [...document.querySelectorAll(".transaction-list li")];
    expect(rows[0]?.textContent).toContain("Аванс");

    button("Доходы").click();
    await flushEffects();
    rows = [...document.querySelectorAll(".transaction-list li")];
    expect(rows).toHaveLength(1);
    expect(rows[0]?.textContent).toContain("Аванс");
    expect(transactions).toHaveBeenCalledTimes(1);
  });

  it("appends pages without duplicate transactions", async () => {
    const second = { ...transaction, id: 2, description: "Такси", category: "transport" as const };
    const transactions = vi.fn()
      .mockResolvedValueOnce({ items: [transaction], next_cursor: "next" })
      .mockResolvedValueOnce({ items: [transaction, second], next_cursor: null });
    const api = client({ transactions });
    mount(api);
    await flushEffects();

    button("Показать ещё").click();
    await flushEffects();

    expect(document.querySelectorAll(".transaction-list li")).toHaveLength(2);
    expect(document.body.textContent).toContain("Такси");
  });

  it("guards delete double-click and reloads after success", async () => {
    let finish: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const remove = vi.fn().mockReturnValue(pending);
    const api = client({ delete: remove });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(api);
    await flushEffects();

    const removeButton = button("Удалить");
    removeButton.click();
    removeButton.click();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(window.confirm).toHaveBeenCalledWith("Удалить трату 500 ₽, Обед, 17.07?");
    finish?.();
    await act(async () => { await pending; await flushEffects(); });
    expect(api.summary).toHaveBeenCalledTimes(2);
  });

  it("refreshes and retries GET once, but never replays DELETE after 401", async () => {
    const getClient = client({
      summary: vi.fn()
        .mockRejectedValueOnce(new ApiError(401, "invalid_session", "Сессия истекла"))
        .mockResolvedValueOnce(summary)
    });
    const refreshGet = vi.fn().mockResolvedValue("renewed");
    mount(getClient, refreshGet, "expired");
    await flushEffects();
    expect(getClient.summary).toHaveBeenCalledTimes(2);
    expect(vi.mocked(getClient.summary).mock.calls.map((call) => call[0])).toEqual(["expired", "renewed"]);

    render(null, document.body);
    const remove = vi.fn().mockRejectedValue(new ApiError(401, "invalid_session", "Сессия истекла"));
    const deleteClient = client({ delete: remove });
    const refreshDelete = vi.fn().mockResolvedValue("renewed");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(deleteClient, refreshDelete, "expired");
    await flushEffects();
    button("Удалить").click();
    await flushEffects();

    expect(remove).toHaveBeenCalledTimes(1);
    expect(refreshDelete).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Нажмите «Удалить» ещё раз");
  });
});

function mount(
  api: FinanceClient,
  refreshSession = vi.fn().mockResolvedValue("renewed"),
  token = "token"
): void {
  act(() => {
    render(
      <FinanceScreen
        token={token}
        timezone="UTC"
        client={api}
        refreshSession={refreshSession}
      />,
      document.body
    );
  });
}

function button(text: string): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!(found instanceof HTMLButtonElement)) throw new Error(`button not found: ${text}`);
  return found;
}

function input(label: string, value: string): void {
  const found = document.querySelector(`input[aria-label="${label}"]`);
  if (!(found instanceof HTMLInputElement)) throw new Error(`input not found: ${label}`);
  found.value = value;
  found.dispatchEvent(new Event("input", { bubbles: true }));
}

async function flushEffects(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await Promise.resolve();
  });
}
