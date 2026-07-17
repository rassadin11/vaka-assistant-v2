import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { CalendarScreen, monthGrid, todayInTimezone, type CalendarClient } from "./calendar";

afterEach(() => {
  render(null, document.body);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("calendar helpers", () => {
  it("builds a Monday-first month with week tails", () => {
    const cells = monthGrid(2026, 7);

    expect(cells).toHaveLength(35);
    expect(cells[0]).toEqual({ iso: "2026-06-29", day: 29, inMonth: false });
    expect(cells[34]).toEqual({ iso: "2026-08-02", day: 2, inMonth: false });
  });

  it("uses the configured user timezone for today", () => {
    const now = new Date("2026-07-17T23:30:00Z");
    expect(todayInTimezone("Europe/Moscow", now)).toBe("2026-07-18");
    expect(todayInTimezone("America/New_York", now)).toBe("2026-07-17");
  });
});

describe("CalendarScreen", () => {
  it("renders month markers, recurring description, done state and empty selected day", async () => {
    const today = todayInTimezone("UTC");
    const client: CalendarClient = {
      fetch: vi.fn().mockResolvedValue({
        days: {
          [today]: [
            { id: 1, kind: "reminder", text: "Вода", time_local: "09:00", occurs_at: `${today}T09:00:00+00:00`, recurring: true, repeat_human: "каждый день в 09:00", status: "active", truncated: false },
            { id: 2, kind: "agent_task", text: "Сводка", time_local: "10:00", occurs_at: `${today}T10:00:00+00:00`, recurring: false, repeat_human: null, status: "done", truncated: false }
          ]
        }
      }),
      create: vi.fn(),
      cancel: vi.fn()
    };

    act(() => {
      render(<CalendarScreen token="token" timezone="UTC" client={client} />, document.body);
    });
    await act(async () => {
      await flushEffects();
    });

    expect(document.body.textContent).toContain("каждый день в 09:00");
    expect(document.body.textContent).toContain("Выполнено");
    expect(document.querySelectorAll(".marker")).toHaveLength(2);
  });

  it("guards cancellation against a double click", async () => {
    const today = todayInTimezone("UTC");
    let finishCancel: (() => void) | undefined;
    const cancelPromise = new Promise<void>((resolve) => { finishCancel = resolve; });
    const client: CalendarClient = {
      fetch: vi.fn().mockResolvedValue({
        days: {
          [today]: [
            { id: 1, kind: "reminder", text: "Вода", time_local: "09:00", occurs_at: `${today}T09:00:00+00:00`, recurring: false, repeat_human: null, status: "active", truncated: false }
          ]
        }
      }),
      create: vi.fn(),
      cancel: vi.fn().mockReturnValue(cancelPromise)
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    act(() => {
      render(<CalendarScreen token="token" timezone="UTC" client={client} />, document.body);
    });
    await act(async () => {
      await flushEffects();
    });
    const button = [...document.querySelectorAll("button")].find((item) => item.textContent === "Отменить");
    expect(button).toBeDefined();

    button?.click();
    button?.click();
    expect(client.cancel).toHaveBeenCalledTimes(1);
    finishCancel?.();
    await act(async () => { await cancelPromise; });
  });

  it("refreshes once and retries a calendar GET once after 401", async () => {
    const fetch = vi.fn()
      .mockRejectedValueOnce(new ApiError(401, "invalid_session", "Сессия истекла"))
      .mockResolvedValueOnce({ days: {} });
    const refreshSession = vi.fn().mockResolvedValue("renewed-token");
    const client: CalendarClient = { fetch, create: vi.fn(), cancel: vi.fn() };

    act(() => {
      render(
        <CalendarScreen
          token="expired-token"
          timezone="UTC"
          client={client}
          refreshSession={refreshSession}
        />,
        document.body
      );
    });
    await act(async () => { await flushEffects(); });

    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls.map((call) => call[0])).toEqual(["expired-token", "renewed-token"]);
    expect(document.body.textContent).toContain("Пока пусто");
  });

  it("refreshes after mutation 401 without replaying the mutation", async () => {
    const today = todayInTimezone("UTC");
    const cancel = vi.fn().mockRejectedValue(
      new ApiError(401, "invalid_session", "Сессия истекла")
    );
    const refreshSession = vi.fn().mockResolvedValue("renewed-token");
    const client: CalendarClient = {
      fetch: vi.fn().mockResolvedValue({
        days: {
          [today]: [
            { id: 1, kind: "reminder", text: "Вода", time_local: "09:00", occurs_at: `${today}T09:00:00+00:00`, recurring: false, repeat_human: null, status: "active", truncated: false }
          ]
        }
      }),
      create: vi.fn(),
      cancel
    };
    vi.spyOn(window, "confirm").mockReturnValue(true);
    act(() => {
      render(
        <CalendarScreen
          token="expired-token"
          timezone="UTC"
          client={client}
          refreshSession={refreshSession}
        />,
        document.body
      );
    });
    await act(async () => { await flushEffects(); });
    const button = [...document.querySelectorAll("button")].find(
      (item) => item.textContent === "Отменить"
    );

    await act(async () => {
      button?.click();
      await flushEffects();
    });

    expect(refreshSession).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain("Нажмите «Отменить» ещё раз");
  });

  it("keeps the current month empty when events exist only in a week tail", async () => {
    const today = todayInTimezone("UTC");
    const [year, month] = today.split("-").map(Number);
    const tail = monthGrid(year, month).find((cell) => !cell.inMonth);
    expect(tail).toBeDefined();
    const client: CalendarClient = {
      fetch: vi.fn().mockResolvedValue({
        days: {
          [tail!.iso]: [
            { id: 9, kind: "reminder", text: "Хвост", time_local: "09:00", occurs_at: `${tail!.iso}T09:00:00+00:00`, recurring: false, repeat_human: null, status: "active", truncated: false }
          ]
        }
      }),
      create: vi.fn(),
      cancel: vi.fn()
    };
    act(() => {
      render(<CalendarScreen token="token" timezone="UTC" client={client} />, document.body);
    });
    await act(async () => { await flushEffects(); });

    expect(document.body.textContent).toContain("Пока пусто");
  });
});

async function flushEffects(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  await Promise.resolve();
}
