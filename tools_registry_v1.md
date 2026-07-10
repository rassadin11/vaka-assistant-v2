# Реестр инструментов v1 — спецификация для реализации

Документ самодостаточен: каждый раздел можно передавать нейросети-исполнителю как отдельную задачу вместе с разделами 1–3 (общий контракт). Стек: Python 3.12, Pydantic v2, asyncio, PostgreSQL (RLS по user_id), Redis, LLM — DeepSeek через OpenRouter (openai SDK).

---

## 1. Базовые принципы (передавать нейросети вместе с каждой задачей)

1. **user_id никогда не приходит из аргументов модели.** Диспетчер инжектит его из контекста задачи (объект `TaskContext`). Если модель сгенерировала поле user_id в аргументах — оно игнорируется.
2. **Валидация до исполнения.** Аргументы инструмента валидируются Pydantic-моделью. Ошибка валидации не роняет цикл: модель получает tool_result с текстом ошибки и полем `retryable: true`, максимум 2 повтора.
3. **Каждый вызов логируется** в `tool_calls_log` (user_id, tool_name, args jsonb, result_status, latency_ms, ts). Аргументы с чувствительными полями (тексты писем) — усечение до 500 символов.
4. **Простые схемы.** DeepSeek слабее флагманов в tool calling: максимум 5 полей на инструмент, без вложенных объектов глубже 1 уровня, обязательных полей — минимум, enum вместо свободных строк где возможно.
5. **Компактный контекст.** В один запрос к LLM передаётся не более 10 инструментов. Состав определяется профилем (раздел 4).
6. **Даты и время** — ISO 8601 в таймзоне пользователя (`users.timezone`, задаётся при активации, 2.9; всегда явная, дефолта нет). Модель получает текущее время пользователя в системном промпте.
7. **Идемпотентность mutating-инструментов при переигровке.** Задача может быть доставлена повторно (падение воркера до ACK, redelivery из 2.5). Для инструментов с risk != read_only диспетчер использует ключ `idem:{update_id}:{порядковый номер tool-вызова в задаче}` в redis-queue (`SET NX EX 86400`; после исполнения — сериализованный ToolResult в значение): ключ уже существует → handler не исполняется, возвращается сохранённый результат. redis-queue (noeviction, AOF) даёт ту же гарантию сохранности, что и сама очередь, чьи переигровки этот ключ страхует. update_id приходит в TaskContext из envelope.

## 2. Уровни риска и поведение диспетчера

| Уровень | Описание | Поведение |
|---|---|---|
| `read_only` | Чтение данных, поиск | Исполняется сразу |
| `mutating_internal` | Запись во внутренние таблицы, обратимо | Исполняется сразу, логируется |
| `mutating_external` | Действие во внешнем мире (письмо, событие в календаре) | Требует подтверждения пользователем |

**Механика подтверждения (`mutating_external`):**
1. Диспетчер сериализует вызов в Redis: ключ `pending:{user_id}:{uuid}`, TTL 15 минут.
2. Пользователю отправляется сообщение с человекочитаемым описанием действия и inline-кнопками «Выполнить» / «Отменить».
3. Агентный цикл завершается ответом модели вида «Подготовил письмо, ожидаю подтверждения».
4. Callback кнопки → воркер читает pending-ключ → исполнение через outbox-таблицу (`outbox_actions`: id, user_id, action jsonb, status, created_at) → уведомление о результате.
5. Истечение TTL → действие отменяется, пользователь уведомляется.

## 3. Базовый контракт (код-скелет)

```python
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import UUID
from pydantic import BaseModel

class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    MUTATING_INTERNAL = "mutating_internal"
    MUTATING_EXTERNAL = "mutating_external"

class TaskContext(BaseModel):
    user_id: UUID        # внутренний users.id (НЕ tg_user_id) — см. plan/db-schema.md
    chat_id: int         # телеграмный chat_id для ответов
    update_id: int       # из envelope, для ключей идемпотентности (§1.7)
    timezone: str
    plan: str            # тариф, для лимитов
    trace_id: str

class ToolResult(BaseModel):
    status: str                    # "ok" | "error" | "pending_confirmation"
    payload: dict[str, Any] = {}
    error: str | None = None
    retryable: bool = False

class ToolDefinition(BaseModel):
    name: str
    description: str               # 1-2 предложения, на английском (лучше для tool calling)
    args_schema: type[BaseModel]
    risk: RiskLevel
    handler: Callable[[TaskContext, BaseModel], Awaitable[ToolResult]]
    daily_limit: int | None = None # None = без лимита; проверка через Redis-счётчик

    def to_openai_schema(self) -> dict: ...   # конвертация Pydantic → JSON Schema для API

class ToolRegistry:
    def get_for_context(self, ctx: TaskContext, connected: set[str]) -> list[ToolDefinition]: ...
    async def dispatch(self, ctx: TaskContext, name: str, raw_args: dict) -> ToolResult: ...
```

Definition of done для скелета: unit-тесты на инжекцию user_id, обработку невалидных аргументов, ветку pending_confirmation, срабатывание daily_limit, идемпотентность (повторный dispatch с тем же ключом не исполняет handler и возвращает сохранённый результат — §1.7).

---

## 4. Профили включения инструментов

Чтобы не превышать 10 инструментов на запрос:

- **core (всегда):** add_transaction, query_transactions, create_reminder, list_reminders, cancel_reminder, remember_fact, web_search — 7 шт.
- **+finance_extra:** set_budget, get_budget_status — включаются, если классификатор пометил сообщение как финансовое (правило: наличие сумм/валют/слов категорий) — итого 9.
- **+web_extra:** fetch_page — включается после первого web_search в рамках задачи (второй проход цикла) либо при наличии URL в сообщении.
- **+docs:** search_documents — включается, если у пользователя есть загруженные документы (флаг в кэше профиля).
- **+google:** list_events, create_calendar_event, send_email — включаются только при подключённом OAuth (флаг connected).
- **+scheduler:** schedule_task, list_scheduled_tasks, cancel_scheduled_task — включаются по ключевым словам регулярности («каждый», «ежедневно», cron-подобные фразы) или всегда, если укладываемся в лимит 10.

Правило приоритета при переполнении: core > google > docs > остальные.

---

## 5. Инструменты: финансы

### 5.1 add_transaction — MUTATING_INTERNAL
Назначение: записать доход или расход из свободного текста пользователя.

```python
class TxCategory(str, Enum):
    food = "food"; transport = "transport"; housing = "housing"
    health = "health"; entertainment = "entertainment"; shopping = "shopping"
    subscriptions = "subscriptions"; salary = "salary"; other = "other"

class AddTransactionArgs(BaseModel):
    amount: float                  # положительное число
    direction: str                 # "expense" | "income"
    category: TxCategory = TxCategory.other
    description: str = ""          # исходная формулировка пользователя
    ts: str | None = None          # ISO 8601; None = сейчас
```

Реализация: INSERT в `transactions`; amount ≤ 0 → ошибка retryable; ts в будущем более чем на 1 день → ошибка. Ответ: payload с итогом дня по категории (для реплики ассистента «сегодня на еду уже 2 340 ₽»).

### 5.2 query_transactions — READ_ONLY
Назначение: выборки и агрегации для ответов и отчётов.

```python
class QueryTransactionsArgs(BaseModel):
    period_start: str              # ISO date
    period_end: str
    category: TxCategory | None = None
    group_by: str = "category"     # "category" | "day" | "none"
```

Реализация: SELECT с агрегацией; суммы разных валют не смешиваются — при наличии не-RUB операций payload группируется дополнительно по currency; максимум 100 строк в payload; период > 366 дней → ошибка. Отдельно: если group_by="day" и период ≥ 14 дней — воркер дополнительно рендерит PNG-график (matplotlib) и отправляет в чат (вне tool_result).

### 5.3 set_budget — MUTATING_INTERNAL
```python
class SetBudgetArgs(BaseModel):
    category: TxCategory
    monthly_limit: float
```
UPSERT в `budgets`. Проверка бюджета — не инструмент: триггер в add_transaction (превышение 80%/100% → уведомление в ответе).

### 5.4 get_budget_status — READ_ONLY
Без аргументов. Возвращает все бюджеты с текущим расходом месяца в процентах.

Definition of done (раздел 5): миграция таблиц, 4 инструмента, тесты на агрегации и границы периодов, e2e: фраза «потратил 500 на такси» → корректная запись.

---

## 6. Инструменты: напоминания и планировщик

### 6.1 create_reminder — MUTATING_INTERNAL
```python
class CreateReminderArgs(BaseModel):
    text: str
    remind_at: str                 # ISO 8601, будущее время
    repeat: str = "none"           # "none" | "daily" | "weekly" | "monthly"
```
Реализация: запись в `scheduled_tasks` (repeat → cron_expr маппингом; none → одноразовая с полем run_at). Лимит: 25 активных на пользователя (daily_limit не подходит — проверка COUNT при создании) + daily_limit 30 созданий/день (защита от циклов создал-удалил-создал, жгущих токены). remind_at в прошлом → ошибка retryable с текущим временем пользователя в тексте ошибки.

### 6.2 list_reminders — READ_ONLY
Без аргументов. Активные напоминания, отсортированные по времени, максимум 25.

### 6.3 cancel_reminder — MUTATING_INTERNAL
```python
class CancelReminderArgs(BaseModel):
    reminder_id: int
```
Проверка принадлежности через RLS происходит автоматически; отсутствие записи → ошибка not_found (не retryable).

### 6.4 schedule_task — MUTATING_INTERNAL (этап 5)
Отличие от напоминания: исполняет промпт агентом, а не шлёт текст.
```python
class ScheduleTaskArgs(BaseModel):
    prompt: str                    # что сделать, например "собери сводку расходов за вчера"
    cron: str                      # стандартное cron-выражение
    title: str
```
Лимит: 10 активных (по тарифу из блока лимитов) + daily_limit 10 созданий/день. Валидация cron через croniter; минимальный интервал — 1 час (защита от token-burn). Исполнение — очередь q:background с дневным токен-бюджетом.

### 6.5–6.6 list_scheduled_tasks / cancel_scheduled_task — по аналогии с 6.2–6.3.

Definition of done: шедулер-процесс (croniter, тик 60 с), доставка одноразовых и повторяющихся, тесты на таймзоны (границы суток), лимиты.

---

## 7. Инструменты: память

### 7.1 remember_fact — MUTATING_INTERNAL
```python
class RememberFactArgs(BaseModel):
    fact: str                      # атомарный факт: "работает в X", "аллергия на орехи"
```
Реализация: эмбеддинг локально через sentence-transformers, отдельный сервис эмбеддингов. **Модель зафиксирована по мини-бенчмарку 2026-07-10 (обсуждение №4): intfloat/multilingual-e5-large** (28 русских пар + 55 дистракторов: recall@5 0.929 против 0.893 у bge-m3 — критерий плана; худший ранг 13 против 51; обязательные префиксы query:/passage:; 1024 изм.) → INSERT в memory_facts (text, embedding). Дедупликация: cosine similarity > 0.92 с существующим фактом → обновление ts вместо вставки (калибровка: разные факты у e5 дают ≤0.885). Лимит: 500 фактов, при превышении — вытеснение самых старых по last_used.

### 7.2 Поиск по памяти — НЕ инструмент
Релевантные факты (top-5 по cosine, порог 0.80 — калибровка под e5 2026-07-10: у неё сжатый диапазон близостей, p99 нерелевантных пар 0.801, исходный порог 0.55 пропускал бы всё) инжектятся в системный промпт автоматически перед каждым запросом. Причина: DeepSeek не всегда догадывается вызвать поиск памяти; автоинжект надёжнее и дешевле лишнего вызова.

Definition of done: выбор и фиксация модели эмбеддингов, pgvector-индекс (HNSW), автоинжект в контекст-менеджере, дедупликация.

---

## 8. Инструменты: поиск и веб

### 8.1 web_search — READ_ONLY
```python
class WebSearchArgs(BaseModel):
    query: str
    num_results: int = 5           # максимум 8
```
Реализация: HTTP к внутреннему SearXNG (`/search?format=json`), таймаут 8 с; payload: title, url, snippet. Кэш Redis по нормализованному query, TTL 1 час. daily_limit: 50. SearXNG недоступен → ошибка not retryable с текстом «поиск временно недоступен» (модель честно сообщает пользователю).

### 8.2 fetch_page — READ_ONLY
```python
class FetchPageArgs(BaseModel):
    url: str
```
Реализация: httpx (follow_redirects, таймаут 10 с, max 5 МБ) → trafilatura.extract → усечение до 8 000 токенов (tiktoken-подсчёт приближённо по символам ×0.4). Блокировки: только http/https; резолв в приватные подсети (SSRF) → отказ; страница без извлекаемого текста → ошибка not retryable. daily_limit: 30.

Definition of done: развёрнутый SearXNG (internal-only, лимит upstream-запросов), SSRF-фильтр с тестами (localhost, 169.254.0.0/16, RFC1918), кэш.

---

## 9. Документы (PDF)

### 9.1 Пайплайн загрузки — НЕ инструмент, обработчик события файла
1. Гейтвей: message.document с mime application/pdf → задача типа `ingest_pdf` в q:background.
2. Воркер: скачивание через Bot API (лимит 20 МБ — файлы больше отклоняются с сообщением пользователю), PyMuPDF → текст постранично.
3. Текстовый слой пуст (< 50 символов на страницу в среднем) → OCR: растеризация страниц PyMuPDF (pixmap ~200 dpi) + tesseract (rus+eng, через pytesseract). pdf2image/poppler не используются — PyMuPDF уже в пайплайне и рендерит сам, лишний системный бинарь не нужен (правка 2026-07-10, детализация 4.6). Лимит OCR: 100 страниц.
4. Чанкинг: 800 токенов, overlap 100, метаданные (doc_id, page). Эмбеддинги → таблица `doc_chunks` (pgvector).
5. Уведомление: «Документ обработан, N страниц. Спрашивайте.» Пользователь может сразу попросить суммаризацию — тогда чанки первых ~30k токенов идут одним проходом.
Лимит тарифа: 200 страниц/мес (Redis-счётчик page-ingest; число финализируется обсуждением №11, синхронизировано с pricing-draft.md).

### 9.2 search_documents — READ_ONLY
```python
class SearchDocumentsArgs(BaseModel):
    query: str
    doc_id: int | None = None      # None = по всем документам пользователя
```
Top-6 чанков по cosine, payload с указанием (документ, страница) — модель обязана ссылаться на страницу в ответе (требование в системном промпте).

### 9.3 list_documents / delete_document — READ_ONLY / MUTATING_INTERNAL, тривиальные.

Definition of done: e2e на текстовом PDF и скане, счётчик страниц, ответы с указанием страниц.

---

## 10. Google-интеграции (после OAuth-флоу)

Предусловие: OAuth-флоу реализован (callback-endpoint, токены в Postgres с envelope-шифрованием — ключ в vault, таблица oauth_tokens из миграции v1; фоновый refresh). Scopes v1: `calendar.events` + `gmail.send` (чтение Gmail отложено — restricted-класс, см. решение в переписке).

### 10.1 list_events — READ_ONLY
```python
class ListEventsArgs(BaseModel):
    date_from: str                 # ISO date
    date_to: str                   # максимум 31 день
```
Google Calendar API events.list, primary-календарь. Токен невалиден и refresh неуспешен → ошибка с флагом reconnect_required → воркер шлёт кнопку «Переподключить Google».

### 10.2 create_calendar_event — MUTATING_EXTERNAL (подтверждение)
```python
class CreateEventArgs(BaseModel):
    title: str
    start: str                     # ISO 8601 datetime
    end: str
    description: str = ""
```
Человекочитаемое описание для кнопки подтверждения: «Создать событие "{title}" {дата} {время}–{время}?»

### 10.3 send_email — MUTATING_EXTERNAL (подтверждение)
```python
class SendEmailArgs(BaseModel):
    to: str                        # один адрес, EmailStr
    subject: str
    body: str                      # plain text v1
```
Подтверждение показывает полный текст письма. daily_limit: 20. В лог — body усечённый до 500 символов.

Definition of done: OAuth e2e на тестовом аккаунте, refresh-ротация, ветка reconnect_required, подтверждения с outbox.

---

## 11. Голосовые сообщения (STT) — не инструмент, обработчик события

1. Гейтвей: message.voice (ogg/opus) → envelope kind=voice в q:interactive (голос — интерактивный ввод, не фон); в payload только tg_file_id, duration, size — файл скачивает воркер.
2. Воркер: длительность > 5 мин → отказ с сообщением пользователю; проверка лимита минут/день (Redis-счётчик) до скачивания.
3. Скачивание через Bot API → STTProvider.transcribe(audio, language_hint="ru") → транскрипт.
4. Транскрипт обрабатывается как обычное текстовое сообщение пользователя (тот же агентный путь); в messages сохраняется текст транскрипта с пометкой модальности в meta (voice, duration).
5. Пустой/неразборчивый результат → «не удалось разобрать голосовое, попробуйте ещё раз или напишите текстом».
6. Стоимость STT — в usage: model = 'stt:<provider>', токен-поля NULL, cost_usd по тарифу провайдера.

Интерфейс STTProvider (+ MockSTTProvider для тестов) — по образцу LLMProvider; выбор провайдера: локальный faster-whisper vs API (Whisper/Groq) — отложенное обсуждение №6 мастер-плана, ключевой критерий — качество на русском.

Definition of done: e2e голосовое → корректный ответ ассистента; отказ на >5 мин; лимит срабатывает; запись стоимости в usage.

---

## 12. Сводная таблица

| Инструмент | Риск | Лимит/день | Профиль | Этап плана |
|---|---|---|---|---|
| add_transaction | internal | — | core | 4 |
| query_transactions | read | — | core | 4 |
| set_budget | internal | — | finance_extra | 4 |
| get_budget_status | read | — | finance_extra | 4 |
| create_reminder | internal | 25 актив., 30 созд./день | core | 4 |
| list_reminders | read | — | core | 4 |
| cancel_reminder | internal | — | core | 4 |
| remember_fact | internal | 500 актив. | core | 4 |
| web_search | read | 50 | core | 4 |
| fetch_page | read | 30 | web_extra | 4 |
| search_documents | read | — | docs | 4–5 |
| list/delete_document | read/internal | — | docs | 4–5 |
| schedule_task | internal | 10 актив., 10 созд./день | scheduler | 5 |
| list/cancel_scheduled_task | read/internal | — | scheduler | 5 |
| list_events | read | — | google | 4 (после OAuth) |
| create_calendar_event | external | 30 | google | 4 |
| send_email | external | 20 | google | 4 |

## 13. Порядок реализации и передача задач нейросети

Каждая задача формулируется исполнителю как: (a) разделы 1–3 целиком, (b) целевой раздел, (c) definition of done, (d) существующие интерфейсы (файлы registry.py, context.py). Рекомендуемая последовательность:

1. Скелет (раздел 3) + тесты — фундамент, без него остальное не проверить.
2. Финансы (5) — без внешних зависимостей, обкатка контракта.
3. Напоминания (6.1–6.3) + шедулер.
4. Память (7) — включает выбор эмбеддингов, влияет на 9.
5. Поиск (8) — требует развёрнутого SearXNG.
6. Документы (9).
7. Планировщик задач (6.4–6.6).
8. Голосовые сообщения (11) — независимы от остальных, можно параллельно в любой момент; к закрытой бете обязательны.
9. Google (10) — последним: самая длинная внешняя зависимость (верификация OAuth-приложения запускается параллельно с п. 1).

Правило приёмки любой задачи: тесты зелёные + ручной e2e-сценарий через Telegram + запись появилась в tool_calls_log.
