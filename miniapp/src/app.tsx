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
      <nav class="app-tabs" aria-label="Разделы Mini App">
        <button type="button" class="nav-item" aria-current={screen === "calendar" ? "page" : undefined} onClick={() => setScreen("calendar")}>Календарь</button>
        <button type="button" class="nav-item" aria-current={screen === "finance" ? "page" : undefined} onClick={() => setScreen("finance")}>Финансы</button>
      </nav>
      {screen === "calendar" ? (
        <CalendarScreen token={token} timezone={me.timezone} refreshSession={refreshSession} />
      ) : (
        <FinanceScreen token={token} timezone={me.timezone} refreshSession={refreshSession} />
      )}
    </main>
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
