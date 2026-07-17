import { afterEach, describe, expect, it } from "vitest";

import { resolveStartScreen, screenFromStartParam, startScreen } from "./app";

afterEach(() => {
  delete window.Telegram;
});

describe("Mini App start parameter", () => {
  it("resolves query screen before the Telegram start parameter", () => {
    expect(resolveStartScreen("?screen=finance", undefined)).toBe("finance");
    expect(resolveStartScreen("", "finance")).toBe("finance");
    expect(resolveStartScreen("?screen=bogus", undefined)).toBe("calendar");
    expect(resolveStartScreen("?screen=calendar", "finance")).toBe("calendar");
  });

  it("allows finance and defaults calendar for all other hints", () => {
    expect(screenFromStartParam("finance")).toBe("finance");
    expect(screenFromStartParam("calendar")).toBe("calendar");
    expect(screenFromStartParam("unknown")).toBe("calendar");
    expect(screenFromStartParam(null)).toBe("calendar");
  });

  it("reads the hint from initDataUnsafe instead of parsing initData", () => {
    const webApp = {
      initData: "start_param=calendar",
      initDataUnsafe: { start_param: "finance" },
      ready() {},
      expand() {}
    };
    window.Telegram = { WebApp: webApp };

    expect(startScreen()).toBe("finance");
    webApp.initDataUnsafe.start_param = "calendar";
    expect(startScreen()).toBe("calendar");
    webApp.initDataUnsafe.start_param = "unknown";
    expect(startScreen()).toBe("calendar");
  });
});
