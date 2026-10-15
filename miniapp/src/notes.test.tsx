import { render } from "preact";
import { act } from "preact/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type Note, type NotesList } from "./api";
import { NotesScreen, formatUpdated, type NotesClient } from "./notes";

afterEach(() => {
  render(null, document.body);
  vi.restoreAllMocks();
});

const note: Note = {
  id: 12,
  title: "Пробежки",
  content: "Первая строка\nВторая строка",
  chars: 26,
  created_at: "2026-10-01T09:00:00+03:00",
  updated_at: "2026-10-15T09:12:00+03:00"
};

const listing: NotesList = {
  notes: [
    { id: 12, title: "Пробежки", chars: 26, updated_at: "2026-10-15T09:12:00+03:00" },
    { id: 13, title: "Книги", chars: 5, updated_at: "2026-09-01T09:12:00+03:00" }
  ],
  notes_total: 2,
  notes_limit: 100
};

function client(overrides: Partial<NotesClient> = {}): NotesClient {
  return {
    list: vi.fn().mockResolvedValue(listing),
    get: vi.fn().mockResolvedValue(note),
    create: vi.fn().mockResolvedValue({ ...note, id: 14, title: "Новая" }),
    update: vi.fn().mockResolvedValue(note),
    delete: vi.fn().mockResolvedValue(undefined),
    ...overrides
  };
}

describe("NotesScreen", () => {
  it("shows skeleton rows while loading, then the list with counters", async () => {
    let resolve: ((value: NotesList) => void) | undefined;
    const api = client({ list: vi.fn().mockReturnValue(new Promise<NotesList>((r) => { resolve = r; })) });
    mount(api);
    await flushEffects();
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("Загружаем…");

    resolve?.(listing);
    await flushEffects();
    expect(document.body.textContent).toContain("Пробежки");
    expect(document.body.textContent).toContain("2 из 100");
    expect(document.body.textContent).toContain("26 симв.");
  });

  it("renders the empty state", async () => {
    mount(client({ list: vi.fn().mockResolvedValue({ notes: [], notes_total: 0, notes_limit: 100 }) }));
    await flushEffects();
    expect(document.body.textContent).toContain("Заметок пока нет");
  });

  it("filters by title case-insensitively without a server request", async () => {
    const api = client();
    mount(api);
    await flushEffects();
    type("Поиск по заголовку", "КНИГ");
    await flushEffects();
    expect(document.body.textContent).toContain("Книги");
    expect(document.body.textContent).not.toContain("Пробежки");
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  it("disables the add button at the notes limit", async () => {
    mount(client({ list: vi.fn().mockResolvedValue({ ...listing, notes_total: 100 }) }));
    await flushEffects();
    expect(addButton().disabled).toBe(true);
  });

  it("opens a note keeping line breaks and keeps the filter after going back", async () => {
    mount(client());
    await flushEffects();
    type("Поиск по заголовку", "проб");
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    expect(document.querySelector(".note-content")?.textContent).toBe("Первая строка\nВторая строка");

    button("К списку").click();
    await flushEffects();
    expect((document.querySelector('input[type="search"]') as HTMLInputElement).value).toBe("проб");
  });

  it("confirms deletion with the title and returns to the list", async () => {
    const api = client();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    mount(api);
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    button("Удалить").click();
    await flushEffects();

    expect(confirm.mock.calls[0][0]).toContain("«Пробежки»");
    expect(api.delete).toHaveBeenCalledWith("token", 12);
    expect(document.body.textContent).toContain("Заметка удалена.");
  });

  it("does not delete when confirmation is declined", async () => {
    const api = client();
    vi.spyOn(window, "confirm").mockReturnValue(false);
    mount(api);
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    button("Удалить").click();
    await flushEffects();
    expect(api.delete).not.toHaveBeenCalled();
  });

  it("returns to the list with a notice when the note was deleted elsewhere", async () => {
    mount(client({ get: vi.fn().mockRejectedValue(new ApiError(404, "not_found", "нет")) }));
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    expect(document.body.textContent).toContain("Заметка удалена.");
    expect(document.body.textContent).toContain("Книги");
  });

  it("creates a note and keeps typed text on title_taken", async () => {
    const create = vi.fn().mockRejectedValue(new ApiError(409, "title_taken", "занято"));
    mount(client({ create }));
    await flushEffects();
    addButton().click();
    await flushEffects();
    expect(saveButton().disabled).toBe(true);
    type("Название заметки", "Книги");
    type("Текст заметки", "мой текст");
    await flushEffects();
    saveButton().click();
    await flushEffects();

    expect(create).toHaveBeenCalledWith("token", "Книги", "мой текст");
    expect(document.body.textContent).toContain("Заметка с таким названием уже есть");
    expect((document.querySelector("textarea") as HTMLTextAreaElement).value).toBe("мой текст");
  });

  it("reports the server-side notes limit on create", async () => {
    const create = vi.fn().mockRejectedValue(new ApiError(409, "notes_limit", "лимит"));
    mount(client({ create }));
    await flushEffects();
    addButton().click();
    await flushEffects();
    type("Название заметки", "Книги");
    type("Текст заметки", "мой текст");
    await flushEffects();
    saveButton().click();
    await flushEffects();

    expect(document.body.textContent).toContain("Достигнут лимит заметок");
    expect((document.querySelector("textarea") as HTMLTextAreaElement).value).toBe("мой текст");
  });

  it("shows counters and blocks saving over the limits", async () => {
    mount(client());
    await flushEffects();
    addButton().click();
    await flushEffects();
    type("Название заметки", "а".repeat(50));
    type("Текст заметки", "б".repeat(8001));
    await flushEffects();
    expect(document.querySelector('[data-testid="title-counter"]')?.textContent).toContain("50 / 64");
    expect(document.querySelector('[data-testid="content-counter"]')?.textContent).toContain("8001 / 8000");
    expect(saveButton().disabled).toBe(true);
  });

  it("sends only changed fields with expected_updated_at on edit", async () => {
    const api = client();
    mount(api);
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    button("Править").click();
    await flushEffects();
    type("Текст заметки", "новый текст");
    await flushEffects();
    saveButton().click();
    await flushEffects();
    expect(api.update).toHaveBeenCalledWith("token", 12, {
      content: "новый текст",
      expected_updated_at: note.updated_at
    });
  });

  it("asks before cancelling a dirty edit", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount(client());
    await flushEffects();
    button("Пробежки").click();
    await flushEffects();
    button("Править").click();
    await flushEffects();
    type("Текст заметки", "правка");
    await flushEffects();
    button("Отмена").click();
    await flushEffects();
    expect(confirm).toHaveBeenCalled();
    expect((document.querySelector("textarea") as HTMLTextAreaElement).value).toBe("правка");
  });

  describe("version conflict", () => {
    async function openConflict(api: NotesClient) {
      mount(api);
      await flushEffects();
      button("Пробежки").click();
      await flushEffects();
      button("Править").click();
      await flushEffects();
      type("Текст заметки", "моя правка");
      await flushEffects();
      saveButton().click();
      await flushEffects();
    }

    it("shows the banner and keeps the typed text", async () => {
      const update = vi.fn().mockRejectedValue(new ApiError(409, "stale", "изменена"));
      await openConflict(client({ update }));
      expect(document.body.textContent).toContain("Заметка изменилась, пока вы её правили");
      expect((document.querySelector("textarea") as HTMLTextAreaElement).value).toBe("моя правка");
    });

    it("saves anyway without expected_updated_at", async () => {
      const update = vi.fn()
        .mockRejectedValueOnce(new ApiError(409, "stale", "изменена"))
        .mockResolvedValueOnce(note);
      await openConflict(client({ update }));
      button("Всё равно сохранить").click();
      await flushEffects();
      expect(update).toHaveBeenLastCalledWith("token", 12, { content: "моя правка" });
      expect(document.querySelector(".note-content")).not.toBeNull();
    });

    it("shows the new version after confirmation and discards the edit", async () => {
      const update = vi.fn().mockRejectedValue(new ApiError(409, "stale", "изменена"));
      const fresh = { ...note, content: "дописал ассистент" };
      const get = vi.fn().mockResolvedValueOnce(note).mockResolvedValueOnce(fresh);
      vi.spyOn(window, "confirm").mockReturnValue(true);
      await openConflict(client({ update, get }));
      button("Показать новую версию").click();
      await flushEffects();
      expect(document.querySelector(".note-content")?.textContent).toBe("дописал ассистент");
    });
  });
});

describe("formatUpdated", () => {
  const now = new Date("2026-10-15T12:00:00Z");
  it("formats relative days and falls back to a date", () => {
    expect(formatUpdated("2026-10-14T09:00:00+00:00", "UTC", now)).toBe("вчера");
    expect(formatUpdated("2026-10-12T09:00:00+00:00", "UTC", now)).toBe("3 дня назад");
    expect(formatUpdated("2026-10-15T09:12:00+00:00", "UTC", now)).toBe("сегодня в 09:12");
    expect(formatUpdated("2026-08-01T09:00:00+00:00", "UTC", now)).toContain("авг");
  });
});

function mount(api: NotesClient): void {
  act(() => {
    render(
      <NotesScreen token="token" timezone="UTC" client={api} refreshSession={vi.fn().mockResolvedValue("renewed")} />,
      document.body
    );
  });
}

function button(text: string): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!(found instanceof HTMLButtonElement)) throw new Error(`button not found: ${text}`);
  return found;
}

function addButton(): HTMLButtonElement {
  return document.querySelector('button[aria-label="Создать заметку"]') as HTMLButtonElement;
}

function saveButton(): HTMLButtonElement {
  return document.querySelector("button.note-save") as HTMLButtonElement;
}

function type(label: string, value: string): void {
  const found = document.querySelector(`[aria-label="${label}"]`);
  if (!(found instanceof HTMLInputElement) && !(found instanceof HTMLTextAreaElement)) {
    throw new Error(`field not found: ${label}`);
  }
  found.value = value;
  found.dispatchEvent(new Event("input", { bubbles: true }));
}

async function flushEffects(): Promise<void> {
  // Two passes: the first renders after a state change, the second settles the effect's fetch.
  for (let pass = 0; pass < 2; pass += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
      await Promise.resolve();
    });
  }
}
