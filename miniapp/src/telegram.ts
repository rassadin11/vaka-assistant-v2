export interface TelegramWebApp {
  initData: string;
  initDataUnsafe?: {
    start_param?: string;
  };
  colorScheme?: "light" | "dark";
  ready(): void;
  expand(): void;
  BackButton?: TelegramButton;
  MainButton?: TelegramMainButton;
}

interface TelegramButton {
  show(): void;
  hide(): void;
  onClick(callback: () => void): void;
  offClick(callback: () => void): void;
}

interface TelegramMainButton extends TelegramButton {
  setText(text: string): void;
  enable(): void;
  disable(): void;
  showProgress?(leaveActive?: boolean): void;
  hideProgress?(): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function telegramWebApp(): TelegramWebApp | undefined {
  return window.Telegram?.WebApp;
}

export function applyTelegramTheme(webApp: TelegramWebApp): void {
  const root = document.documentElement;
  root.dataset.theme = webApp.colorScheme === "dark" ? "dark" : "light";
}

export function bindBackButton(callback: () => void): () => void {
  const button = telegramWebApp()?.BackButton;
  if (!button) return () => undefined;
  button.onClick(callback);
  button.show();
  return () => {
    button.offClick(callback);
    button.hide();
  };
}

export function bindMainButton(
  text: string,
  callback: () => void,
  options: { disabled?: boolean; progress?: boolean } = {}
): () => void {
  const button = telegramWebApp()?.MainButton;
  if (!button) return () => undefined;
  button.setText(text);
  if (options.disabled) button.disable(); else button.enable();
  if (options.progress) button.showProgress?.(true); else button.hideProgress?.();
  button.onClick(callback);
  button.show();
  return () => {
    button.offClick(callback);
    button.hideProgress?.();
    button.hide();
  };
}
