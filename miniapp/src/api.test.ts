import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  authenticate,
  cancelScheduled,
  createNote,
  createReminder,
  deleteNote,
  deleteTransaction,
  fetchCalendar,
  fetchFinanceSummary,
  fetchFinanceAiSummary,
  fetchMe,
  fetchNote,
  fetchNotes,
  fetchTransactions,
  updateNote
} from "./api";

describe("Mini App API adapter", () => {
  it("uses snake_case init_data and a bearer token", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: "signed-token" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ timezone: "UTC", plan: "trial" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const token = await authenticate("telegram-init-data");
    const me = await fetchMe(token);

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ init_data: "telegram-init-data" });
    expect(fetchMock.mock.calls[1][1]?.headers).toEqual({ Authorization: "Bearer signed-token" });
    expect(me).toEqual({ timezone: "UTC", plan: "trial" });
  });

  it("surfaces only the public API error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "invalid_session", message: "Сессия истекла" } }), { status: 401 })));

    await expect(fetchMe("expired")).rejects.toEqual(new ApiError(401, "invalid_session", "Сессия истекла"));
  });

  it("maps calendar queries and never retries mutations", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ days: {} }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 7, text: "Позвонить", next_run_at: "2026-07-18T10:00:00+03:00", status: "active" }), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "scheduled_state_conflict", message: "Уже отменено" } }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchCalendar("token", "2026-07-01", "2026-07-31");
    await createReminder("token", "Позвонить", "2026-07-18T10:00");
    await expect(cancelScheduled("token", 7)).rejects.toEqual(new ApiError(409, "scheduled_state_conflict", "Уже отменено"));

    expect(fetchMock.mock.calls[0][0]).toBe("/app/api/calendar?from=2026-07-01&to=2026-07-31");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ text: "Позвонить", remind_at_local: "2026-07-18T10:00" });
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("validates finance responses and maps optional transaction query arguments", async () => {
    const summary = {
      period: { from: "2026-07-01", to: "2026-07-31" },
      totals: { expense: "20.00", income: "100.00" },
      prev_period: null,
      by_category: [{ category: "food", expense: "20.00", share: 1 }],
      by_bucket: [{ bucket: "2026-07-01", expense: "20.00" }],
      budgets: [{ category: "food", limit: "50.00", spent: "20.00", ratio: 0.4 }]
    };
    const page = {
      items: [{ id: 7, ts_local: "2026-07-17T12:00:00+03:00", amount: "20.00", direction: "expense", category: "food", description: "Обед" }],
      next_cursor: "opaque"
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(summary), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(page), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchFinanceSummary("token", "2026-07-01", "2026-07-31")).resolves.toEqual(summary);
    await expect(fetchTransactions("token", "2026-07-01", "2026-07-31", "food", "opaque")).resolves.toEqual(page);
    await expect(deleteTransaction("token", 7)).resolves.toBeUndefined();

    expect(fetchMock.mock.calls[1][0]).toBe("/app/api/finance/transactions?from=2026-07-01&to=2026-07-31&category=food&cursor=opaque");
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
  });

  it("fetches AI summary and normalizes the current backend contract", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "ready", summary: "Траты в норме.", cached: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchFinanceAiSummary("token", "2026-07-01", "2026-07-31"))
      .resolves.toEqual({ status: "ready", summary: "Траты в норме.", cached: true });

    expect(fetchMock.mock.calls[0][0]).toBe("/app/api/finance/ai-summary?from=2026-07-01&to=2026-07-31");
    expect(fetchMock.mock.calls[0][1]?.headers).toEqual({ Authorization: "Bearer token" });
  });

  it("rejects malformed money and does not parse a successful DELETE body", async () => {
    const malformed = {
      period: { from: "2026-07-01", to: "2026-07-31" },
      totals: { expense: 20, income: "0.00" },
      prev_period: null,
      by_category: [],
      by_bucket: [],
      budgets: []
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(malformed), { status: 200 })));

    await expect(fetchFinanceSummary("token", "2026-07-01", "2026-07-31"))
      .rejects.toEqual(new ApiError(200, "unexpected_response", "Сервис вернул некорректный ответ."));
  });

  it("parses public error envelopes for every finance request", async () => {
    const error = { error: { code: "finance_unavailable", message: "Финансы недоступны" } };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify(error), { status: 503 })
    )));

    await expect(fetchFinanceSummary("token", "2026-07-01", "2026-07-31"))
      .rejects.toEqual(new ApiError(503, "finance_unavailable", "Финансы недоступны"));
    await expect(fetchTransactions("token", "2026-07-01", "2026-07-31"))
      .rejects.toEqual(new ApiError(503, "finance_unavailable", "Финансы недоступны"));
    await expect(fetchFinanceAiSummary("token", "2026-07-01", "2026-07-31"))
      .rejects.toEqual(new ApiError(503, "finance_unavailable", "Финансы недоступны"));
    await expect(deleteTransaction("token", 7))
      .rejects.toEqual(new ApiError(503, "finance_unavailable", "Финансы недоступны"));
  });
  it("maps the notes endpoints and surfaces the stale and title_taken codes", async () => {
    const note = {
      id: 12, title: "Пробежки", content: "текст", chars: 5,
      created_at: "2026-10-01T09:00:00+03:00", updated_at: "2026-10-15T09:12:00+03:00"
    };
    const list = {
      notes: [{ id: 12, title: "Пробежки", chars: 5, updated_at: "2026-10-15T09:12:00+03:00" }],
      notes_total: 1,
      notes_limit: 100
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(list), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(note), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(note), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "stale", message: "изменена" }, ...note }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "title_taken", message: "занято" } }), { status: 409 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "not_found", message: "нет" } }), { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchNotes("token")).resolves.toEqual(list);
    await expect(fetchNote("token", 12)).resolves.toEqual(note);
    await expect(createNote("token", "Пробежки", "текст")).resolves.toEqual(note);
    await expect(updateNote("token", 12, { content: "x", expected_updated_at: note.updated_at }))
      .rejects.toEqual(new ApiError(409, "stale", "изменена"));
    await expect(updateNote("token", 12, { title: "Книги" }))
      .rejects.toEqual(new ApiError(409, "title_taken", "занято"));
    await expect(deleteNote("token", 12)).resolves.toBeUndefined();
    await expect(deleteNote("token", 12)).rejects.toEqual(new ApiError(404, "not_found", "нет"));

    expect(fetchMock.mock.calls[0][0]).toBe("/app/api/notes");
    expect(fetchMock.mock.calls[1][0]).toBe("/app/api/notes/12");
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({ title: "Пробежки", content: "текст" });
    expect(fetchMock.mock.calls[3][1]?.method).toBe("PATCH");
    expect(JSON.parse(String(fetchMock.mock.calls[3][1]?.body))).toEqual({ content: "x", expected_updated_at: note.updated_at });
    expect(JSON.parse(String(fetchMock.mock.calls[4][1]?.body))).toEqual({ title: "Книги" });
    expect(fetchMock.mock.calls[5][1]?.method).toBe("DELETE");
  });
});
