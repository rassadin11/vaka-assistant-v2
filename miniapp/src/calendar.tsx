import type { JSX } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";

import {
  ApiError,
  cancelScheduled,
  createReminder,
  fetchCalendar,
  type CalendarData,
  type CalendarOccurrence
} from "./api";
import { bindBackButton, bindMainButton } from "./telegram";

export interface CalendarClient {
  fetch(token: string, from: string, to: string): Promise<CalendarData>;
  create(token: string, text: string, remindAtLocal: string): Promise<unknown>;
  cancel(token: string, id: number): Promise<void>;
}

const defaultClient: CalendarClient = {
  fetch: fetchCalendar,
  create: createReminder,
  cancel: cancelScheduled
};

interface Month {
  year: number;
  month: number;
}

export interface CalendarCell {
  iso: string;
  day: number;
  inMonth: boolean;
}

export function monthGrid(year: number, month: number): CalendarCell[] {
  const first = new Date(Date.UTC(year, month - 1, 1));
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const start = new Date(first);
  start.setUTCDate(1 - mondayOffset);
  const last = new Date(Date.UTC(year, month, 0));
  const cells = mondayOffset + last.getUTCDate() <= 35 ? 35 : 42;
  return Array.from({ length: cells }, (_, index) => {
    const value = new Date(start);
    value.setUTCDate(start.getUTCDate() + index);
    return {
      iso: isoDate(value),
      day: value.getUTCDate(),
      inMonth: value.getUTCMonth() === month - 1
    };
  });
}

export function todayInTimezone(timezone: string, now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function CalendarScreen({
  token,
  timezone,
  client = defaultClient,
  refreshSession = async () => { throw new Error("Сессия недоступна."); }
}: {
  token: string;
  timezone: string;
  client?: CalendarClient;
  refreshSession?: () => Promise<string>;
}) {
  const today = todayInTimezone(timezone);
  const [year, month] = today.split("-").map(Number);
  const [view, setView] = useState<Month>({ year, month });
  const [selected, setSelected] = useState(today);
  const [data, setData] = useState<CalendarData | null>(null);
  const [state, setState] = useState<"loading" | "content" | "offline" | "unauthorized" | "error">("loading");
  const [message, setMessage] = useState("");
  const [notice, setNotice] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [text, setText] = useState("");
  const [formDate, setFormDate] = useState(selected);
  const [formTime, setFormTime] = useState("09:00");
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState<number | null>(null);
  const touchStart = useRef<number | null>(null);
  const tokenRef = useRef(token);
  const submittingRef = useRef(false);
  const cancellingRef = useRef(false);
  const grid = useMemo(() => monthGrid(view.year, view.month), [view]);
  const from = grid[0].iso;
  const to = grid[grid.length - 1].iso;

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  async function load(retriedAfterRefresh = false) {
    if (!navigator.onLine) {
      setState("offline");
      return;
    }
    setState("loading");
    try {
      setData(await client.fetch(tokenRef.current, from, to));
      setState("content");
    } catch (error) {
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
        setMessage(error instanceof Error ? error.message : "Не удалось загрузить календарь.");
        setState("error");
      }
    }
  }

  useEffect(() => {
    void load();
  }, [from, to]);

  useEffect(() => {
    if (!formOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFormOpen(false);
    };
    window.addEventListener("keydown", onKey);
    const unbindBack = bindBackButton(() => setFormOpen(false));
    return () => {
      window.removeEventListener("keydown", onKey);
      unbindBack();
    };
  }, [formOpen]);

  useEffect(() => {
    if (!formOpen) return;
    const disabled = submitting || text.length < 1 || text.length > 500;
    return bindMainButton("Создать напоминание", () => void submitReminder(), {
      disabled,
      progress: submitting
    });
  }, [formOpen, submitting, text, formDate, formTime]);

  function moveMonth(delta: number) {
    const date = new Date(Date.UTC(view.year, view.month - 1 + delta, 1));
    const next = { year: date.getUTCFullYear(), month: date.getUTCMonth() + 1 };
    setView(next);
    setSelected(`${next.year}-${pad(next.month)}-01`);
  }

  function openForm() {
    setFormDate(selected);
    setFormOpen(true);
  }

  async function submitReminder(event?: JSX.TargetedSubmitEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (submittingRef.current || text.length < 1 || text.length > 500) return;
    submittingRef.current = true;
    setNotice("");
    setSubmitting(true);
    try {
      await client.create(tokenRef.current, text, `${formDate}T${formTime}`);
      setText("");
      setFormOpen(false);
      await load();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        try {
          tokenRef.current = await refreshSession();
          setNotice("Сессия обновлена. Нажмите «Создать» ещё раз.");
        } catch {
          setState("unauthorized");
        }
      } else {
        setMessage(error instanceof Error ? error.message : "Не удалось создать напоминание.");
        setState("error");
      }
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  async function cancel(item: CalendarOccurrence) {
    if (cancellingRef.current || !window.confirm(`Отменить «${item.text}»?`)) return;
    cancellingRef.current = true;
    setNotice("");
    setCancelling(item.id);
    try {
      await client.cancel(tokenRef.current, item.id);
      await load();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        try {
          tokenRef.current = await refreshSession();
          setNotice("Сессия обновлена. Нажмите «Отменить» ещё раз.");
        } catch {
          setState("unauthorized");
        }
      } else {
        setMessage(error instanceof Error ? error.message : "Не удалось отменить задачу.");
        setState("error");
      }
    } finally {
      cancellingRef.current = false;
      setCancelling(null);
    }
  }

  if (state === "loading") return <CalendarSkeleton />;
  if (state === "offline") return <CalendarState title="Нет соединения" message="Проверьте интернет и попробуйте снова." retry={load} />;
  if (state === "unauthorized") return <CalendarState title="Сессия закончилась" message="Закройте и заново откройте Mini App." />;
  if (state === "error") return <CalendarState title="Не удалось загрузить" message={message} retry={load} />;

  const days = data?.days ?? {};
  const events = [...(days[selected] ?? [])]
    .filter((item) => item.status !== ("cancelled" as string))
    .sort((left, right) => left.occurs_at.localeCompare(right.occurs_at));
  const viewPrefix = `${view.year}-${pad(view.month)}-`;
  const emptyMonth = !Object.entries(days).some(
    ([day, items]) => day.startsWith(viewPrefix) && items.length > 0
  );

  return (
    <section class="calendar" aria-label="Календарь напоминаний">
      <header class="calendar-toolbar">
        <button type="button" aria-label="Предыдущий месяц" onClick={() => moveMonth(-1)}><ChevronIcon direction="left" /></button>
        <h2>{monthTitle(view)}</h2>
        <button type="button" aria-label="Следующий месяц" onClick={() => moveMonth(1)}><ChevronIcon direction="right" /></button>
      </header>
      <div
        class="month-grid"
        onTouchStart={(event) => { touchStart.current = event.touches[0]?.clientX ?? null; }}
        onTouchEnd={(event) => {
          const end = event.changedTouches[0]?.clientX;
          if (touchStart.current !== null && end !== undefined) {
            const distance = end - touchStart.current;
            if (Math.abs(distance) > 40) moveMonth(distance > 0 ? -1 : 1);
          }
          touchStart.current = null;
        }}
      >
        {['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((day) => <span class="weekday" key={day}>{day}</span>)}
        {grid.map((cell) => {
          const items = days[cell.iso] ?? [];
          return (
            <button
              type="button"
              key={cell.iso}
              class={`day-cell${cell.inMonth ? "" : " day-tail"}${cell.iso === today ? " day-today" : ""}`}
              aria-label={`${cell.iso}, событий: ${items.length}`}
              aria-pressed={selected === cell.iso}
              onClick={() => setSelected(cell.iso)}
            >
              <span>{cell.day}</span>
              <span class="markers" aria-hidden="true">
                {items.slice(0, 3).map((item, index) => <i key={`${item.id}-${index}`} class={`marker marker-${item.kind}`} />)}
                {items.length > 3 ? <small>+{items.length - 3}</small> : null}
              </span>
            </button>
          );
        })}
      </div>

      {emptyMonth ? <p class="calendar-empty">Пока пусто. Напишите боту «напомни…» или создайте напоминание кнопкой ниже.</p> : null}
      {notice ? <p class="session-notice" role="status">{notice}</p> : null}

      <section class="day-list" aria-labelledby="selected-day-title">
        <h3 id="selected-day-title">{dayTitle(selected)}</h3>
        {events.length === 0 ? <p class="quiet">На этот день ничего не запланировано.</p> : (
          <ul>
            {events.map((item) => (
              <li class={item.status === "done" ? "event-done" : ""} key={`${item.id}-${item.occurs_at}`}>
                <div class="event-copy">
                  <strong>{item.time_local}</strong>
                  <span>{item.text}</span>
                  {item.recurring ? <small class="repeat-badge">↻ {item.repeat_human}</small> : null}
                </div>
                {item.status === "active" ? (
                  <button type="button" class="cancel-button" aria-label="Отменить напоминание" disabled={cancelling !== null} onClick={() => void cancel(item)}><TrashIcon /></button>
                ) : <span class="done-label">Выполнено</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <button type="button" class="create-button" onClick={openForm}>+ Напоминание</button>
      {formOpen ? (
        <div
          class="modal-overlay"
          role="presentation"
          onClick={(event) => { if (event.target === event.currentTarget) setFormOpen(false); }}
        >
          <form
            class="reminder-form"
            role="dialog"
            aria-modal="true"
            aria-labelledby="reminder-form-title"
            onSubmit={(event) => void submitReminder(event)}
          >
            <div class="reminder-form-header">
              <h3 id="reminder-form-title">Новое напоминание</h3>
              <button type="button" class="modal-close" aria-label="Закрыть" onClick={() => setFormOpen(false)}>×</button>
            </div>
            <label>Текст<textarea required maxLength={500} value={text} onInput={(event) => setText(event.currentTarget.value)} /></label>
            <div class="form-row">
              <label>Дата<input type="date" required value={formDate} onInput={(event) => setFormDate(event.currentTarget.value)} /></label>
              <label>Время<input type="time" required value={formTime} onInput={(event) => setFormTime(event.currentTarget.value)} /></label>
            </div>
            <div class="form-actions">
              <button type="button" onClick={() => setFormOpen(false)}>Закрыть</button>
              <button type="submit" disabled={submitting || text.length < 1}>{submitting ? "Создаём…" : "Создать"}</button>
            </div>
          </form>
        </div>
      ) : null}
    </section>
  );
}

function ChevronIcon({ direction }: { direction: "left" | "right" }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <polyline points={direction === "left" ? "15 18 9 12 15 6" : "9 18 15 12 9 6"} />
    </svg>
  );
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

function CalendarSkeleton() {
  const weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  return (
    <section class="calendar" aria-busy="true" aria-label="Загружаем календарь">
      <header class="calendar-toolbar">
        <span class="skeleton skeleton-btn" />
        <span class="skeleton skeleton-title" />
        <span class="skeleton skeleton-btn" />
      </header>
      <div class="month-grid">
        {weekdays.map((day) => <span class="weekday" key={day}>{day}</span>)}
        {Array.from({ length: 42 }, (_, index) => <span class="skeleton skeleton-cell" key={index} />)}
      </div>
      <section class="day-list">
        <span class="skeleton skeleton-h3" />
        <span class="skeleton skeleton-line" />
        <span class="skeleton skeleton-line" />
      </section>
    </section>
  );
}

function CalendarState({ title, message, retry }: { title: string; message?: string; retry?: () => Promise<void> }) {
  return <section class="calendar-state" aria-live="polite"><h2>{title}</h2>{message ? <p>{message}</p> : null}{retry ? <button type="button" onClick={() => void retry()}>Повторить</button> : null}</section>;
}

function monthTitle(view: Month): string {
  const value = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(view.year, view.month - 1, 1)));
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function dayTitle(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  return new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, day)));
}

function isoDate(value: Date): string {
  return `${value.getUTCFullYear()}-${pad(value.getUTCMonth() + 1)}-${pad(value.getUTCDate())}`;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}
