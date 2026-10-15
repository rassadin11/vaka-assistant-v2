import { useEffect, useRef, useState } from "preact/hooks";

import {
  ApiError,
  createNote,
  deleteNote,
  fetchNote,
  fetchNotes,
  updateNote,
  type Note,
  type NoteUpdate,
  type NotesList
} from "./api";
import { bindBackButton } from "./telegram";

export const NOTE_TITLE_LIMIT = 64;
export const NOTE_CONTENT_LIMIT = 8000;
const TITLE_COUNTER_FROM = 48;

export interface NotesClient {
  list(token: string): Promise<NotesList>;
  get(token: string, id: number): Promise<Note>;
  create(token: string, title: string, content: string): Promise<Note>;
  update(token: string, id: number, update: NoteUpdate): Promise<Note>;
  delete(token: string, id: number): Promise<void>;
}

const defaultClient: NotesClient = {
  list: fetchNotes,
  get: fetchNote,
  create: createNote,
  update: updateNote,
  delete: deleteNote
};

type View =
  | { kind: "list" }
  | { kind: "note"; id: number }
  | { kind: "edit"; note: Note | null };

type Call = <T>(fn: (token: string) => Promise<T>) => Promise<T>;

const DELETED_NOTICE = "Заметка удалена.";

export function NotesScreen({
  token,
  timezone,
  client = defaultClient,
  refreshSession = async () => { throw new Error("Сессия недоступна."); }
}: {
  token: string;
  timezone: string;
  client?: NotesClient;
  refreshSession?: () => Promise<string>;
}) {
  const [view, setView] = useState<View>({ kind: "list" });
  const [list, setList] = useState<NotesList | null>(null);
  const [listState, setListState] = useState<"loading" | "content" | "error">("loading");
  const [listMessage, setListMessage] = useState("");
  const [filter, setFilter] = useState("");
  const [notice, setNotice] = useState("");
  const [unauthorized, setUnauthorized] = useState(false);
  const tokenRef = useRef(token);

  useEffect(() => {
    tokenRef.current = token;
  }, [token]);

  // Retries once with a refreshed session; a failed refresh ends the session.
  const call: Call = async (fn) => {
    try {
      return await fn(tokenRef.current);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error;
      try {
        tokenRef.current = await refreshSession();
      } catch {
        setUnauthorized(true);
        throw error;
      }
      try {
        return await fn(tokenRef.current);
      } catch (retryError) {
        if (retryError instanceof ApiError && retryError.status === 401) setUnauthorized(true);
        throw retryError;
      }
    }
  };

  async function loadList(silent: boolean) {
    if (!silent) setListState("loading");
    try {
      setList(await call((t) => client.list(t)));
      setListState("content");
    } catch (error) {
      if (silent && list) return;
      setListMessage(error instanceof Error ? error.message : "Не удалось загрузить заметки.");
      setListState("error");
    }
  }

  useEffect(() => {
    if (view.kind !== "list") return;
    void loadList(list !== null);
  }, [view.kind]);

  function backToList(message = "") {
    setNotice(message);
    setView({ kind: "list" });
  }

  if (unauthorized) {
    return <NotesState title="Сессия закончилась" message="Закройте и заново откройте Mini App." />;
  }

  if (view.kind === "note") {
    return (
      <NoteView
        key={view.id}
        id={view.id}
        timezone={timezone}
        client={client}
        call={call}
        onBack={() => backToList()}
        onGone={() => backToList(DELETED_NOTICE)}
        onEdit={(note) => setView({ kind: "edit", note })}
      />
    );
  }

  if (view.kind === "edit") {
    const editing = view.note;
    return (
      <NoteEditor
        base={editing}
        client={client}
        call={call}
        onCancel={() => setView(editing ? { kind: "note", id: editing.id } : { kind: "list" })}
        onSaved={(note) => { setNotice(""); setView({ kind: "note", id: note.id }); }}
        onShowNew={(id) => setView({ kind: "note", id })}
        onGone={() => backToList(DELETED_NOTICE)}
      />
    );
  }

  if (listState === "loading" && !list) return <NotesSkeleton />;
  if (listState === "error" && !list) {
    return <NotesState title="Не удалось загрузить" message={listMessage} retry={() => loadList(false)} />;
  }

  const notes = list?.notes ?? [];
  const total = list?.notes_total ?? 0;
  const limit = list?.notes_limit ?? 0;
  const needle = filter.trim().toLocaleLowerCase("ru-RU");
  const visible = needle
    ? notes.filter((item) => item.title.toLocaleLowerCase("ru-RU").includes(needle))
    : notes;
  const limitReached = total >= limit;

  return (
    <section class="notes" aria-label="Заметки">
      <header class="notes-toolbar">
        <input
          type="search"
          class="notes-search"
          aria-label="Поиск по заголовку"
          placeholder="Поиск по заголовку"
          value={filter}
          onInput={(event) => setFilter(event.currentTarget.value)}
        />
        <button
          type="button"
          class="notes-add"
          aria-label="Создать заметку"
          title={limitReached ? "Достигнут лимит заметок" : undefined}
          disabled={limitReached}
          onClick={() => { setNotice(""); setView({ kind: "edit", note: null }); }}
        >
          +
        </button>
      </header>
      <p class={`notes-count${limitReached ? " notes-count-full" : ""}`}>
        {total} из {limit}{limitReached ? " — лимит заметок достигнут" : ""}
      </p>
      {notice ? <p class="session-notice" role="status">{notice}</p> : null}
      {notes.length === 0 ? (
        <p class="notes-empty">
          Заметок пока нет. Их можно вести и через ассистента («запомни в заметке…»), и здесь — кнопкой «+».
        </p>
      ) : visible.length === 0 ? (
        <p class="notes-empty">Ничего не найдено по запросу «{filter.trim()}».</p>
      ) : (
        <ul class="notes-list">
          {visible.map((item) => (
            <li key={item.id}>
              <button type="button" class="note-row" onClick={() => { setNotice(""); setView({ kind: "note", id: item.id }); }}>
                <strong>{item.title}</strong>
                <span>{formatUpdated(item.updated_at, timezone)} · {item.chars} симв.</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NoteView({
  id,
  timezone,
  client,
  call,
  onBack,
  onGone,
  onEdit
}: {
  id: number;
  timezone: string;
  client: NotesClient;
  call: Call;
  onBack: () => void;
  onGone: () => void;
  onEdit: (note: Note) => void;
}) {
  const [note, setNote] = useState<Note | null>(null);
  const [state, setState] = useState<"loading" | "content" | "error">("loading");
  const [message, setMessage] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState("");
  const deletingRef = useRef(false);
  const backRef = useRef(onBack);
  backRef.current = onBack;

  async function load() {
    setState("loading");
    try {
      setNote(await call((t) => client.get(t, id)));
      setState("content");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        onGone();
        return;
      }
      setMessage(error instanceof Error ? error.message : "Не удалось загрузить заметку.");
      setState("error");
    }
  }

  useEffect(() => {
    void load();
  }, [id]);

  useEffect(() => bindBackButton(() => backRef.current()), []);

  async function remove() {
    if (!note || deletingRef.current) return;
    if (!window.confirm(`Удалить заметку «${note.title}»? Это нельзя отменить.`)) return;
    deletingRef.current = true;
    setDeleting(true);
    setActionError("");
    try {
      await call((t) => client.delete(t, note.id));
      onGone();
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        onGone();
      } else {
        setActionError(error instanceof Error ? error.message : "Не удалось удалить заметку.");
      }
    } finally {
      deletingRef.current = false;
      setDeleting(false);
    }
  }

  if (state === "loading") return <NoteSkeleton />;
  if (state === "error" || !note) {
    return (
      <NotesState title="Не удалось загрузить" message={message} retry={load} back={onBack} />
    );
  }

  return (
    <section class="notes note-view" aria-label="Заметка">
      <button type="button" class="notes-back" onClick={onBack}>← К списку</button>
      <h2 class="note-title">{note.title}</h2>
      <p class="quiet note-meta">
        Изменено {formatUpdated(note.updated_at, timezone)} · {note.chars} симв.
      </p>
      {actionError ? <p class="notes-error" role="alert">{actionError}</p> : null}
      {note.content === "" ? (
        <p class="notes-empty">В заметке пока пусто.</p>
      ) : (
        <div class="note-content">{note.content}</div>
      )}
      <div class="note-actions">
        <button type="button" class="note-edit" disabled={deleting} onClick={() => onEdit(note)}>Править</button>
        <button type="button" class="note-delete" disabled={deleting} onClick={() => void remove()}>
          {deleting ? "Удаляем…" : "Удалить"}
        </button>
      </div>
    </section>
  );
}

function NoteEditor({
  base,
  client,
  call,
  onCancel,
  onSaved,
  onShowNew,
  onGone
}: {
  base: Note | null;
  client: NotesClient;
  call: Call;
  onCancel: () => void;
  onSaved: (note: Note) => void;
  onShowNew: (id: number) => void;
  onGone: () => void;
}) {
  const [title, setTitle] = useState(base?.title ?? "");
  const [content, setContent] = useState(base?.content ?? "");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const baseTitle = base?.title ?? "";
  const baseContent = base?.content ?? "";
  const dirty = title !== baseTitle || content !== baseContent;
  const titleLength = charCount(title);
  const contentLength = charCount(content);
  const overLimit = titleLength > NOTE_TITLE_LIMIT || contentLength > NOTE_CONTENT_LIMIT;
  const canSave = !saving && dirty && !overLimit && title.trim().length > 0;

  function cancel() {
    if (dirty && !window.confirm("Отменить изменения? Введённый текст будет потерян.")) return;
    onCancel();
  }
  const cancelRef = useRef(cancel);
  cancelRef.current = cancel;
  useEffect(() => bindBackButton(() => cancelRef.current()), []);

  async function save(force: boolean) {
    if (savingRef.current || !canSave) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      let saved: Note;
      if (base) {
        const update: NoteUpdate = {};
        if (title !== baseTitle) update.title = title;
        if (content !== baseContent) update.content = content;
        if (!force) update.expected_updated_at = base.updated_at;
        saved = await call((t) => client.update(t, base.id, update));
      } else {
        saved = await call((t) => client.create(t, title, content));
      }
      onSaved(saved);
    } catch (caught) {
      handleError(caught);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  function handleError(caught: unknown) {
    if (caught instanceof ApiError) {
      if (caught.status === 404) {
        onGone();
        return;
      }
      if (caught.status === 409 && caught.code === "stale") {
        setConflict(true);
        return;
      }
      if (caught.status === 409 && caught.code === "title_taken") {
        setConflict(false);
        setError("Заметка с таким названием уже есть. Выберите другое название.");
        return;
      }
      if (caught.status === 409 && caught.code === "notes_limit") {
        setError("Достигнут лимит заметок. Удалите ненужные, чтобы создать новую.");
        return;
      }
    }
    setError(caught instanceof Error ? caught.message : "Не удалось сохранить заметку.");
  }

  function showNew() {
    if (dirty && !window.confirm("Отбросить вашу правку и показать новую версию?")) return;
    if (base) onShowNew(base.id);
  }

  return (
    <form
      class="notes note-editor"
      aria-label={base ? "Редактирование заметки" : "Новая заметка"}
      onSubmit={(event) => { event.preventDefault(); void save(false); }}
    >
      <h2 class="note-editor-title">{base ? "Правка заметки" : "Новая заметка"}</h2>
      {conflict ? (
        <div class="note-conflict" role="alert">
          <p>Заметка изменилась, пока вы её правили</p>
          <div class="note-conflict-actions">
            <button type="button" disabled={saving} onClick={showNew}>Показать новую версию</button>
            <button type="button" class="note-conflict-force" disabled={saving} onClick={() => void save(true)}>
              Всё равно сохранить
            </button>
          </div>
        </div>
      ) : null}
      {error ? <p class="notes-error" role="alert">{error}</p> : null}
      <label>
        Название
        <input
          type="text"
          aria-label="Название заметки"
          value={title}
          onInput={(event) => setTitle(event.currentTarget.value)}
        />
        {titleLength >= TITLE_COUNTER_FROM ? (
          <small class={`counter${titleLength > NOTE_TITLE_LIMIT ? " counter-over" : ""}`} data-testid="title-counter">
            {titleLength} / {NOTE_TITLE_LIMIT}
          </small>
        ) : null}
      </label>
      <label>
        Текст
        <textarea
          aria-label="Текст заметки"
          value={content}
          onInput={(event) => setContent(event.currentTarget.value)}
        />
        <small class={`counter${contentLength > NOTE_CONTENT_LIMIT ? " counter-over" : ""}`} data-testid="content-counter">
          {contentLength} / {NOTE_CONTENT_LIMIT}
        </small>
      </label>
      <div class="note-actions">
        <button type="button" disabled={saving} onClick={cancel}>Отмена</button>
        <button type="submit" class="note-save" disabled={!canSave}>{saving ? "Сохраняем…" : "Сохранить"}</button>
      </div>
    </form>
  );
}

function NotesState({
  title,
  message,
  retry,
  back
}: {
  title: string;
  message?: string;
  retry?: () => Promise<void>;
  back?: () => void;
}) {
  return (
    <section class="notes-state" aria-live="polite">
      <h2>{title}</h2>
      {message ? <p>{message}</p> : null}
      {retry ? <button type="button" onClick={() => void retry()}>Повторить</button> : null}
      {back ? <button type="button" onClick={back}>К списку</button> : null}
    </section>
  );
}

function NotesSkeleton() {
  return (
    <section class="notes" aria-busy="true" aria-label="Загружаем заметки">
      <div class="skeleton skeleton-search" />
      {Array.from({ length: 5 }, (_, index) => <span class="skeleton skeleton-row" key={index} />)}
    </section>
  );
}

function NoteSkeleton() {
  return (
    <section class="notes" aria-busy="true" aria-label="Загружаем заметку">
      <span class="skeleton skeleton-h2" />
      <span class="skeleton skeleton-line" />
      <span class="skeleton skeleton-chart" />
    </section>
  );
}

function charCount(value: string): number {
  // Code points, matching the server's character counting.
  return Array.from(value).length;
}

function dayKey(date: Date, timezone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(date);
}

function dayNumber(key: string): number {
  const [year, month, day] = key.split("-").map(Number);
  return Date.UTC(year, month - 1, day) / 86_400_000;
}

function daysWord(count: number): string {
  const last = count % 10;
  const lastTwo = count % 100;
  if (last === 1 && lastTwo !== 11) return "день";
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) return "дня";
  return "дней";
}

export function formatUpdated(iso: string, timezone: string, now = new Date()): string {
  const date = new Date(iso);
  const diff = dayNumber(dayKey(now, timezone)) - dayNumber(dayKey(date, timezone));
  if (diff <= 0) {
    const time = new Intl.DateTimeFormat("ru-RU", { timeZone: timezone, hour: "2-digit", minute: "2-digit" }).format(date);
    return `сегодня в ${time}`;
  }
  if (diff === 1) return "вчера";
  if (diff < 7) return `${diff} ${daysWord(diff)} назад`;
  const sameYear = dayKey(now, timezone).slice(0, 4) === dayKey(date, timezone).slice(0, 4);
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone,
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" })
  }).format(date);
}
