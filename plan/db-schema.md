# Схема БД v1 (для миграции 1.6)

Статус: зафиксирована (отложенное обсуждение №1 закрыто). Изменения — правкой этого файла до реализации, не после.

## Сквозные решения

- **PK**: uuid v7 для сущностей, не фигурирующих в аргументах модели (users, messages, dialog_summaries, memory_facts, outbox_actions); **bigint identity** для model-facing сущностей (transactions, scheduled_tasks, documents, doc_chunks) и логов (tool_calls_log, usage). Причина: DeepSeek должен уметь безошибочно воспроизводить id в аргументах инструментов — короткие числа надёжнее uuid. Утечки счётчика нет: id видны только владельцу, доступ режет RLS.
- **Генерация uuid v7 — на приложении** (библиотека `uuid-utils`); default в БД не ставится (в Postgres 16 нативной функции нет).
- **Enum-поля — `text` + `CHECK`**, не нативные enum'ы (мучительны в миграциях). Списки значений зеркалят Pydantic-енумы реестра инструментов.
- **Все времена — `timestamptz` (UTC)**; таймзона пользователя применяется на презентации.
- **FK `user_id → users(id) ON DELETE CASCADE`** на всех пользовательских таблицах — механика GDPR-удаления (этап 7.5) одной командой. `user_id` везде `NOT NULL` (уточнение 2026-07-09 по ревью миграции v1): строка с NULL user_id была бы невидима app-роли, но выпадала бы из GDPR-каскада — таких строк не должно существовать. В tool_calls_log/usage (без FK) user_id тоже NOT NULL.
- **Роли БД**: `migrator` (владелец таблиц, только Alembic), `app` (рабочая, RLS активна), `service` (BYPASSRLS — шедулер-тик, outbox-процессор, фоновый OAuth-refresh, админ-команды, broadcast; всё, что по определению работает поперёк пользователей), `metrics_ro` (добавлена 2026-07-12 для 6.2: только SELECT на users/messages/usage/tool_calls_log, BYPASSRLS — кросс-пользовательские агрегаты продуктовых дашбордов Grafana; из приложения не используется, только datasource Grafana).
- **RLS-шаблон**: `USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)` на каждой таблице с колонкой user_id; для users — то же с `id =`. Форма с `missing_ok=true` + NULLIF выбрана осознанно (уточнение 2026-07-09 при реализации 1.3): при неустановленной переменной запрос возвращает 0 строк (fail-closed), а не ошибку — ровно поведение, требуемое приёмкой 1.3; строгая форма `current_setting('app.user_id')` кидала бы исключение. CI-тест: каждая таблица с колонкой user_id имеет политику.
- **Vector-колонки — `vector(1024)` и HNSW-индексы (`vector_cosine_ops`)** в memory_facts/doc_chunks создаются сразу в v1: эмбеддинги решены — локальная модель, оба кандидата (bge-m3, multilingual-e5-large) дают 1024 измерения, финальный выбор на 4.4 схему не меняет.
- **Партиционирование**: tool_calls_log и usage — по месяцу (`created_at`), pg_partman; ключ партиции входит в PK. Ретеншн партиций — отложенное обсуждение №9 (сроки хранения).
- **Хранение сообщений**: бессрочно до GDPR-удаления пользователя; суммаризация ничего не стирает.

## Таблицы (13)

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
  embedding       vector(1024)
INDEX (user_id, last_used_at)
INDEX HNSW (embedding vector_cosine_ops)
```

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
INDEX (status, next_run_at)                  -- тик шедулера (роль service)
INDEX (user_id, status)                      -- list_reminders / list_scheduled_tasks
```

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
  embedding       vector(1024)
INDEX (doc_id), INDEX (user_id)
INDEX HNSW (embedding vector_cosine_ops)
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

## Примечания для исполнителя миграции

- GDPR-удаление (7.5): DELETE users каскадом закрывает всё, кроме tool_calls_log/usage (без FK) — их чистить отдельным DELETE по user_id в той же операции.
- RLS включается (`ENABLE ROW LEVEL SECURITY`) на всех 13 таблицах; на партиционированных — на родителе.
- TaskContext.user_id в реестре инструментов — внутренний uuid users.id (не tg_user_id).
