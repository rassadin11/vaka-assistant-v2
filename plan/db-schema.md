# Схема БД v1 (для миграции 1.6)

Статус: зафиксирована (отложенное обсуждение №1 закрыто). Изменения — правкой этого файла до реализации, не после.

Расширения после фиксации: `notes` (внеплановая задача 2026-07-31), полнотекстовые колонки (S3, 2026-09-07), `friend_links` / `friend_invites` и две колонки `scheduled_tasks` (общие объекты часть 1, 2026-10-18 — `plan/specs/shared-objects-spec.md`; реализация ждёт отложенного обсуждения №12).

## Сквозные решения

- **PK**: uuid v7 для сущностей, не фигурирующих в аргументах модели (users, messages, dialog_summaries, memory_facts, outbox_actions); **bigint identity** для model-facing сущностей (transactions, scheduled_tasks, documents, doc_chunks) и логов (tool_calls_log, usage). Причина: DeepSeek должен уметь безошибочно воспроизводить id в аргументах инструментов — короткие числа надёжнее uuid. Утечки счётчика нет: id видны только владельцу, доступ режет RLS.
- **Генерация uuid v7 — на приложении** (библиотека `uuid-utils`); default в БД не ставится (в Postgres 16 нативной функции нет).
- **Enum-поля — `text` + `CHECK`**, не нативные enum'ы (мучительны в миграциях). Списки значений зеркалят Pydantic-енумы реестра инструментов.
- **Все времена — `timestamptz` (UTC)**; таймзона пользователя применяется на презентации.
- **FK `user_id → users(id) ON DELETE CASCADE`** на всех пользовательских таблицах — механика GDPR-удаления (этап 7.5) одной командой. `user_id` везде `NOT NULL` (уточнение 2026-07-09 по ревью миграции v1): строка с NULL user_id была бы невидима app-роли, но выпадала бы из GDPR-каскада — таких строк не должно существовать. В tool_calls_log/usage (без FK) user_id тоже NOT NULL.
- **Роли БД**: `migrator` (владелец таблиц, только Alembic), `app` (рабочая, RLS активна), `service` (BYPASSRLS — шедулер-тик, outbox-процессор, фоновый OAuth-refresh, админ-команды, broadcast; всё, что по определению работает поперёк пользователей), `metrics_ro` (добавлена 2026-07-12 для 6.2: только SELECT на users/messages/usage/tool_calls_log, BYPASSRLS — кросс-пользовательские агрегаты продуктовых дашбордов Grafana; из приложения не используется, только datasource Grafana).
- **RLS-шаблон**: `USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)` на каждой таблице с колонкой user_id; для users — то же с `id =`. Форма с `missing_ok=true` + NULLIF выбрана осознанно (уточнение 2026-07-09 при реализации 1.3): при неустановленной переменной запрос возвращает 0 строк (fail-closed), а не ошибку — ровно поведение, требуемое приёмкой 1.3; строгая форма `current_setting('app.user_id')` кидала бы исключение. CI-тест: каждая таблица с колонкой user_id имеет политику.
- **Vector-колонки — `vector(1024)` и HNSW-индексы (`vector_cosine_ops`)** в memory_facts/doc_chunks создаются сразу в v1: эмбеддинги решены — локальная модель, оба кандидата (bge-m3, multilingual-e5-large) дают 1024 измерения, финальный выбор на 4.4 схему не меняет.
- **Полнотекстовый поиск (добавлено 2026-09-07, задача S3 профиля small — plan/small-profile.md §4.1)**: в memory_facts и doc_chunks — генерируемая колонка `text_tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED` + GIN-индекс, плюс расширение `pg_trgm` (сигнал `similarity()` про запас, когда стеммер не дал ничего). Колонка именно генерируемая: значение поддерживает СУБД, отдельного шага переиндексации нет ни при вставке, ни при `pg_restore --data-only` (важно для S18). Конфигурация словаря задана явно (`'russian'`), потому что одноаргументная `to_tsvector(text)` зависит от `default_text_search_config` и не IMMUTABLE — генерируемая колонка её не примет. **Схема общая для обоих профилей**: `prod` продолжает искать векторами и колонку не читает, `small` читает её вместо `embedding`; выбор — флагом `RETRIEVAL_BACKEND` (S4). Векторные колонки и HNSW-индексы остаются в схеме в обоих профилях — на пустых таблицах они бесплатны, а единый набор миграций дороже мелкой экономии.
- **Партиционирование**: tool_calls_log и usage — по месяцу (`created_at`), pg_partman; ключ партиции входит в PK. Ретеншн партиций — отложенное обсуждение №9 (сроки хранения).
- **Хранение сообщений**: бессрочно до GDPR-удаления пользователя; суммаризация ничего не стирает.

## Таблицы (16: 14 созданы миграциями, 2 ждут реализации)

Созданы: 13 базовых (миграция `20260709_0001`) + `notes` (`20260731_0004`). Ждут реализации: `friend_links` и `friend_invites` — общие объекты часть 1, миграции нет (блокер — обсуждение №12).

```
users
  id              uuid PK (v7)
  tg_user_id      bigint UNIQUE NOT NULL
  tg_chat_id      bigint NOT NULL            -- в личке == tg_user_id, но не завязываемся
  username        text                       -- tg username, для админ-команд
  first_name      text
  status          text CHECK (pending|active|rejected|banned) DEFAULT 'pending'
  plan            text DEFAULT 'trial'       -- задел этапа 7
  paid_until      timestamptz                -- задел этапа 7
  timezone        text NOT NULL              -- IANA, задаётся при активации (2.9)
  assistant_profile jsonb                    -- опц. персона ассистента (2026-07-18): {name?, address?: 'ty'|'vy', style?}; лимиты полей — спека assistant-persona-iteration-1
  created_at / updated_at
RLS: id = current_setting('app.user_id')::uuid
```

```
messages
  id              uuid PK (v7, сортируемый — порядок в диалоге)
  user_id         uuid FK CASCADE
  role            text CHECK (user|assistant|tool)
  content         text                       -- для голосовых — транскрипт
  tool_calls      jsonb                      -- для role=assistant
  tool_call_id    text                       -- для role=tool
  meta            jsonb                      -- модальность: {kind: voice, duration: N} и т.п.
  tokens          int                        -- для бюджетирования контекста (3.4)
  trace_id        uuid
  created_at
INDEX (user_id, id)                          -- выборка хвоста диалога
```

```
dialog_summaries
  id              uuid PK (v7)
  user_id         uuid FK CASCADE
  summary         text NOT NULL
  upto_message_id uuid NOT NULL              -- граница: контекст = последняя summary + messages после неё
  tokens          int
  created_at
INDEX (user_id, created_at DESC)             -- контекст-менеджер берёт последнюю
```

```
memory_facts
  id              uuid PK (v7)
  user_id         uuid FK CASCADE
  text            text NOT NULL
  last_used_at    timestamptz NOT NULL DEFAULT now()   -- вытеснение старейших при лимите 500 (реестр 7.1)
  created_at / updated_at                    -- updated_at обновляется дедупликацией (cosine > 0.92)
  embedding       vector(1024)               -- пусто в профиле small (эмбеддингов нет)
  text_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED
INDEX (user_id, last_used_at)
INDEX HNSW (embedding vector_cosine_ops)
INDEX GIN (text_tsv)
```

```
notes                                        -- тематические заметки (внеплановая задача 2026-07-31, реестр §7c)
  id              bigint identity PK         -- model-facing не нужен: адресация по title, id служебный
  user_id         uuid FK CASCADE
  title           text NOT NULL              -- тема: «Пробежки», «Идеи подарков»; ≤64 символов после trim
  content         text NOT NULL DEFAULT ''   -- ≤8000 символов (влезает в бюджет tool result)
  created_at / updated_at
UNIQUE (user_id, title)                      -- одна заметка на тему, цель UPSERT
```
Без vector-колонки: заметок ≤100, модель читает список заголовков целиком (list_notes),
семантический поиск не нужен. Автоинжекта в промпт нет — чтение только явными инструментами.

```
transactions
  id              bigint identity PK
  user_id         uuid FK CASCADE
  amount          numeric(12,2) NOT NULL CHECK (amount > 0)
  direction       text CHECK (expense|income)
  category        text CHECK (food|transport|housing|health|entertainment|shopping|subscriptions|salary|other)
  currency        text NOT NULL DEFAULT 'RUB'
  description     text NOT NULL DEFAULT ''
  ts              timestamptz NOT NULL       -- момент операции (может отличаться от created_at)
  created_at
INDEX (user_id, ts)                          -- все запросы = user + период
```

```
budgets
  user_id         uuid FK CASCADE
  category        text CHECK (как в transactions)
  monthly_limit   numeric(12,2) NOT NULL CHECK (monthly_limit > 0)
  created_at / updated_at
  PK (user_id, category)                     -- естественный ключ, цель UPSERT из set_budget
```

```
scheduled_tasks                              -- напоминания И agent-задачи, различает kind
  id              bigint identity PK         -- model-facing (cancel_reminder / cancel_scheduled_task)
  user_id         uuid FK CASCADE
  kind            text CHECK (reminder|agent_task)
  title           text                       -- agent_task: заголовок; reminder: усечённый payload
  payload         text NOT NULL              -- текст напоминания | промпт агенту
  cron_expr       text                       -- NULL = одноразовая
  next_run_at     timestamptz NOT NULL       -- единое поле тика шедулера; пересчёт по croniter в users.timezone
  status          text CHECK (active|done|cancelled)
  last_run_at, created_at
  group_id        uuid                       -- общие напоминания (2026-10-18): NULL = личная задача, иначе метка фан-аута
  created_by_user_id uuid FK users(id) ON DELETE SET NULL   -- NULL = создал владелец строки
INDEX (status, next_run_at)                  -- тик шедулера (роль service)
INDEX (user_id, status)                      -- list_reminders / list_scheduled_tasks
INDEX (group_id) WHERE group_id IS NOT NULL
```
Общее напоминание — N строк с общим `group_id`, по одной на участника (фан-аут,
`plan/specs/shared-objects-spec.md` §2): модель «одна строка — один владелец» и RLS-шаблон
не меняются, `next_run_at` считается по таймзоне каждого участника.
`group_id` без FK — группа не сущность, а метка; `created_by_user_id` с SET NULL,
чтобы GDPR-удаление создателя не ломало напоминание второго участника.

```
friend_links                                 -- дружба (2026-10-18); две зеркальные строки на связь
  user_id         uuid FK users(id) CASCADE NOT NULL
  friend_user_id  uuid FK users(id) CASCADE NOT NULL
  created_at      timestamptz NOT NULL DEFAULT now()
  PK (user_id, friend_user_id)
  CHECK (user_id <> friend_user_id)
INDEX (friend_user_id)                       -- обратный обход в служебных функциях
```
Зеркальные строки выбраны осознанно: RLS-политика остаётся однополевым шаблоном
`user_id = app.user_id`, без подзапроса по членству, и CI-тест на наличие политики
не меняется.

```
friend_invites                               -- одноразовые коды приглашений (2026-10-18)
  id              uuid PK (v7)
  user_id         uuid FK users(id) CASCADE NOT NULL   -- приглашающий, владелец строки
  code            text UNIQUE NOT NULL                -- secrets.token_urlsafe(16)
  status          text CHECK (pending|accepted|revoked|expired) DEFAULT 'pending'
  invitee_user_id uuid FK users(id) CASCADE            -- заполняется при приёме
  expires_at      timestamptz NOT NULL                 -- создание + 7 суток
  created_at / accepted_at
INDEX (user_id, status)
```
Колонка-владелец названа `user_id` ради генератора политик и CI-теста; приглашаемый
читает код не напрямую, а через `accept_friend_invite` (ниже).

```
documents
  id              bigint identity PK         -- model-facing (doc_id в search_documents)
  user_id         uuid FK CASCADE
  filename        text
  pages           int
  status          text CHECK (processing|ready|failed)
  tg_file_id      text                       -- источник v1: перекачка через Bot API
  s3_key          text                       -- задел: хранение оригинала у себя
  size_bytes      bigint
  created_at
INDEX (user_id)
```

```
doc_chunks
  id              bigint identity PK
  user_id         uuid FK CASCADE            -- денормализация под RLS (политика без join)
  doc_id          bigint FK documents(id) ON DELETE CASCADE
  page            int
  chunk_index     int
  text            text NOT NULL
  tokens          int
  embedding       vector(1024)               -- пусто в профиле small (эмбеддингов нет)
  text_tsv        tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED
INDEX (doc_id), INDEX (user_id)
INDEX HNSW (embedding vector_cosine_ops)
INDEX GIN (text_tsv)
```

```
oauth_tokens
  user_id         uuid FK CASCADE
  provider        text DEFAULT 'google'
  access_token_enc  bytea NOT NULL           -- envelope-шифрование на приложении, ключ в vault
  refresh_token_enc bytea
  key_version     int NOT NULL DEFAULT 1     -- ротация ключа без единовременной перешифровки
  expires_at      timestamptz
  scopes          text[]
  status          text CHECK (active|reconnect_required|revoked)
  created_at / updated_at
  PK (user_id, provider)
INDEX (expires_at) WHERE status = 'active'   -- фоновый refresh (роль service)
```

```
outbox_actions
  id              uuid PK (v7)
  user_id         uuid FK CASCADE
  action          jsonb NOT NULL             -- сериализованный tool-вызов
  status          text CHECK (pending|executing|done|failed|cancelled)
  attempts        int NOT NULL DEFAULT 0
  last_error      text
  created_at / executed_at
INDEX (status, created_at)                   -- процессор outbox (роль service)
```

```
tool_calls_log                               -- партиционирована по месяцу (pg_partman, created_at)
  id              bigint identity
  user_id         uuid                       -- без FK: лог переживает решения о ретеншне независимо
  trace_id        uuid
  tool_name       text NOT NULL
  args            jsonb                      -- чувствительные поля усечены до 500 символов (реестр §1.3)
  result_status   text                       -- ok | error | pending_confirmation
  error           text
  latency_ms      int
  created_at      timestamptz NOT NULL
  PK (id, created_at)
INDEX (user_id, created_at)
```

```
usage                                        -- партиционирована по месяцу (pg_partman, created_at)
  id              bigint identity
  user_id         uuid
  trace_id        uuid
  model           text NOT NULL              -- 'deepseek-chat' | 'stt:<provider>' | ...
  prompt_tokens   int                        -- NULL для не-LLM расходов (STT)
  completion_tokens int
  cached_tokens   int                        -- контроль эффективности prompt caching (6.2)
  cost_usd        numeric(10,6) NOT NULL
  queue           text CHECK (interactive|background)
  created_at      timestamptz NOT NULL
  PK (id, created_at)
INDEX (user_id, created_at)                  -- «стоимость/пользователь/день» для Grafana (6.2)
```

## Служебные функции

### `webapp_resolve_user(p_tg_user_id bigint)` — этап 9.1

Назначение: после успешной серверной HMAC-проверки Telegram `initData` разрешить
внешний `tg_user_id` во внутренний UUID до того, как webapp сможет открыть
`user_transaction(UUID)`. Обычный SELECT роли `app` здесь fail-closed из-за RLS,
а выдавать webapp роль `service` запрещено.

Контракт:

```sql
RETURNS TABLE (user_id uuid, status text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
```

Функция выбирает ровно `users.id, users.status` по точному `tg_user_id` и не
возвращает профиль, timezone, plan или Telegram-имя. Владелец — `migrator`.
Миграция обязана выполнить `REVOKE ALL ... FROM PUBLIC` и выдать только
`GRANT EXECUTE ... TO app`; роль `service` webapp не получает. Вызов разрешён в
коде только после успешной HMAC-проверки initData. Неизвестная строка даёт пустой
результат, а решение о 403 для статуса кроме `active` принимает webapp.

Функция не меняет зафиксированное количество таблиц. Её добавление — отдельная
обратно совместимая Alembic migration этапа 9.1; downgrade удаляет только функцию.

### Функции общих объектов — часть 1 (2026-10-18)

Контракты и проверки внутри функций — `plan/specs/shared-objects-spec.md` §4. Здесь
фиксируется только то, что относится к схеме:

- `accept_friend_invite(p_code text, p_invitee uuid)` → `(status text, inviter_user_id uuid)`;
- `create_shared_reminder(p_creator uuid, p_participants uuid[], p_text text, p_runs jsonb, p_cron text)` → `setof (user_id uuid, task_id bigint)`;
- `cancel_shared_reminder_group(p_caller uuid, p_group uuid, p_scope text)` → `integer`.

Все три — `SECURITY DEFINER`, владелец `migrator`, `SET search_path = pg_catalog, public`,
миграция делает `REVOKE ALL ... FROM PUBLIC` и `GRANT EXECUTE ... TO app`; роль
`service` их не получает. Это единственный разрешённый способ кросс-пользовательской
записи из пользовательского пути: роль `service` инструментам LLM не выдаётся.

**Ловушка реализации**: под `SECURITY DEFINER` политики RLS не действуют, поэтому
каждая проверка лимита и каждая выборка внутри этих функций обязана нести явный
`WHERE user_id = ...`. Существующий `core.reminders_service.create_reminder`
опирается на неявный RLS-скоуп (`COUNT(*)` без фильтра по пользователю) — копировать
этот запрос в функцию нельзя.

## Примечания для исполнителя миграции

- GDPR-удаление (7.5): DELETE users каскадом закрывает всё, кроме tool_calls_log/usage (без FK) — их чистить отдельным DELETE по user_id в той же операции.
- GDPR-удаление и общие объекты (2026-10-18): каскад убирает `friend_links` с обеих сторон и `friend_invites` (оба FK CASCADE); строки `scheduled_tasks` второго участника остаются — их владелец он, а `created_by_user_id` обнуляется (SET NULL). Текст общего напоминания, созданного удаляемым пользователем, сохраняется в данных второго участника: решение осознанное, иначе удаление одного человека тихо ломало бы напоминания другого (`plan/specs/shared-objects-spec.md` §11).
- RLS включается (`ENABLE ROW LEVEL SECURITY`) на каждой таблице с колонкой `user_id` — сейчас это 14 созданных, плюс 2 таблицы общих объектов в их миграции; на партиционированных — на родителе.
- TaskContext.user_id в реестре инструментов — внутренний uuid users.id (не tg_user_id).
