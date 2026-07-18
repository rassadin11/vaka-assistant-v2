import { useEffect, useState } from "preact/hooks";

import { ApiError, authenticate, fetchMe, type Me } from "./api";
import { CalendarScreen } from "./calendar";
import { FinanceScreen } from "./finance";
import { applyTelegramTheme, telegramWebApp } from "./telegram";

type AppState =
  | { kind: "boot" }
  | { kind: "loading" }
  | { kind: "authenticated"; me: Me; token: string }
  | { kind: "unsupported" }
  | { kind: "error"; message: string };

export function App() {
  const [state, setState] = useState<AppState>({ kind: "boot" });

  async function refreshSession(): Promise<string> {
    const webApp = telegramWebApp();
    if (!webApp?.initData) {
      throw new Error("Данные Telegram недоступны.");
    }
    const token = await authenticate(webApp.initData);
    const me = await fetchMe(token);
    setState({ kind: "authenticated", me, token });
    return token;
  }

  async function bootstrap() {
    const webApp = telegramWebApp();
    if (!webApp?.initData) {
      setState({ kind: "unsupported" });
      return;
    }
    setState({ kind: "loading" });
    try {
      applyTelegramTheme(webApp);
      webApp.ready();
      webApp.expand();
      await refreshSession();
    } catch (error) {
      const message = error instanceof ApiError ? error.message : "Не удалось открыть Mini App.";
      setState({ kind: "error", message });
    }
  }

  useEffect(() => {
    void bootstrap();
  }, []);

  if (state.kind === "boot" || state.kind === "loading") {
    return <Status title="Открываем приложение" message="Проверяем безопасное соединение…" />;
  }
  if (state.kind === "unsupported") {
    return (
      <Status
        title="Откройте в Telegram"
        message="Mini App работает только во встроенном окне Telegram."
      />
    );
  }
  if (state.kind === "error") {
    return (
      <Status title="Не удалось загрузить" message={state.message} actionLabel="Повторить" onAction={bootstrap} />
    );
  }
  return <Shell me={state.me} token={state.token} refreshSession={refreshSession} />;
}

function Status({
  title,
  message,
  actionLabel,
  onAction
}: {
  title: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <main class="status" aria-live="polite">
      <div class="mark" aria-hidden="true">V</div>
      <h1>{title}</h1>
      <p>{message}</p>
      {actionLabel && onAction ? <button onClick={() => void onAction()}>{actionLabel}</button> : null}
    </main>
  );
}

function Shell({
  me,
  token,
  refreshSession
}: {
  me: Me;
  token: string;
  refreshSession: () => Promise<string>;
}) {
  const [screen, setScreen] = useState<"calendar" | "finance">(() => startScreen());
  return (
    <main class="shell">
      <header>
        <p class="eyebrow">Vaka Assistant</p>
        <h1>{screen === "calendar" ? "Календарь" : "Финансы"}</h1>
        <p class="quiet">{me.timezone} · {me.plan}</p>
      </header>
      {screen === "calendar" ? (
        <CalendarScreen token={token} timezone={me.timezone} refreshSession={refreshSession} />
      ) : (
        <FinanceScreen token={token} timezone={me.timezone} refreshSession={refreshSession} />
      )}
      <nav class="app-tabs" aria-label="Разделы Mini App">
        <div class="app-tabs-inner">
          <button type="button" class="nav-item" aria-current={screen === "calendar" ? "page" : undefined} onClick={() => setScreen("calendar")}>
            <CalendarTabIcon />
            <span>Календарь</span>
          </button>
          <button type="button" class="nav-item" aria-current={screen === "finance" ? "page" : undefined} onClick={() => setScreen("finance")}>
            <FinanceTabIcon />
            <span>Финансы</span>
          </button>
        </div>
      </nav>
    </main>
  );
}

function CalendarTabIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="3" y="4.5" width="18" height="16.5" rx="2.5" />
      <line x1="3" y1="9.5" x2="21" y2="9.5" />
      <line x1="8" y1="2.5" x2="8" y2="6" />
      <line x1="16" y1="2.5" x2="16" y2="6" />
    </svg>
  );
}

function FinanceTabIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M19 8V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2H6" />
      <circle cx="16.5" cy="14" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function startScreen(): "calendar" | "finance" {
  return resolveStartScreen(
    window.location.search,
    telegramWebApp()?.initDataUnsafe?.start_param
  );
}

export function resolveStartScreen(
  search: string,
  startParam: string | null | undefined
): "calendar" | "finance" {
  const fromQuery = new URLSearchParams(search).get("screen");
  return screenFromStartParam(fromQuery ?? startParam);
}

export function screenFromStartParam(value: string | null | undefined): "calendar" | "finance" {
  return value === "finance" ? "finance" : "calendar";
}
