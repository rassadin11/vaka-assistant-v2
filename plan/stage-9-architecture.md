# Архитектура этапа 9: Telegram Mini App

Статус: архитектурная подготовка, код этапа 9 ещё не начат. Документ уточняет
`stage-9.md`, не заменяет спецификации экранов и не закрывает пункты 9.1–9.4.

## Как читать комплект документов

Исполнитель этапа 9 перед работой читает в таком порядке:

1. блок «Принятые архитектурные решения» в `implementation_plan_v1.md`;
2. `plan/stage-9.md` целиком;
3. этот документ;
4. `plan/stage-9-handoff.md`;
5. спецификацию целевого экрана: `miniapp-calendar-spec.md` или
   `miniapp-finance-spec.md`;
6. при работе с БД — `plan/db-schema.md`.

При конфликте приоритет имеют мастер-план и `stage-9.md`. Найденный конфликт
сначала исправляется в плановых документах, только затем начинается код.

## Цели и границы

Mini App — дополнительная визуальная поверхность над существующими данными бота.
Он не создаёт второй аккаунт пользователя, вторую очередь задач или отдельные
финансовые сущности.

В v1 входят:

- календарь данных `scheduled_tasks`;
- создание одноразового напоминания и отмена задачи;
- финансовая сводка, графики, бюджеты и список транзакций;
- удаление транзакции;
- короткое AI-резюме по агрегатам;
- вход из меню и inline-кнопок бота.

Не входят: Google Calendar, редактирование транзакций, создание повторов в UI,
CSV/Excel, мультивалютность, новые категории, новая схема биллинга и произвольный
конструктор дашбордов.

## Топология

```text
Telegram WebView
      |
      | HTTPS /app/*
      v
    Caddy
      |
      v
webapp container (FastAPI + собранная SPA)
      |                  |
      | app role         | redis-cache
      v                  v
  PgBouncer          rate limit / cache
      |
      v
 PostgreSQL + RLS
```

- `webapp/` — отдельный stateless FastAPI-сервис и отдельный контейнер.
- `miniapp/` — исходники Preact SPA. В runtime Node.js отсутствует: в контейнер
  попадает только собранная статика.
- Gateway не проксирует API Mini App и не получает доступ к Postgres.
- Для v1 используется тот же application image, что у gateway/worker, но с
  дополнительной Node build-stage и командой `python -m webapp`. Отдельный GHCR
  image не нужен.
- Webapp подключается только ролью `app`. `SERVICE_DATABASE_URL` и роль `service`
  в его окружение не передаются.

## URL и маршрутизация

Префикс `/app` сохраняется Caddy и явно присутствует в маршрутах FastAPI. Caddy
не должен применять `handle_path`, чтобы поведение dev/prod не расходилось.

Порядок маршрутов в webapp:

1. `/app/healthz` и `/app/metrics`;
2. `/app/api/*`;
3. `/app/assets/*` со статикой и immutable cache headers;
4. SPA fallback только для `GET /app` и `GET /app/*`, не начинающихся с `api/`.

API и healthcheck никогда не должны возвращать `index.html` при 404. Для
`/app` допустим редирект на `/app/`, но deep-link маршруты должны отвечать SPA.

CORS не включается: frontend и API same-origin. Сессионный токен не кладётся в
cookie, поэтому отдельная CSRF-защита в v1 не нужна.

## Аутентификация и сессия

### Валидация Telegram initData

`POST /app/api/auth` принимает только строку `initData`, с ограничением размера
тела. Сервер:

1. строго разбирает query string и отклоняет дубли обязательных полей;
2. исключает `hash`, сортирует остальные пары и строит data-check-string;
3. вычисляет secret key через HMAC-SHA256 с ключом `WebAppData` и bot token;
4. вычисляет hash data-check-string и сравнивает через constant-time compare;
5. проверяет `auth_date`: не из будущего с учётом малого clock skew и не старше
   одного часа;
6. берёт `tg_user_id` только из проверенного поля `user`.

`initDataUnsafe` разрешён frontend только для неавторитетных UI-подсказок,
например `start_param`; идентичность из него не берётся.

### Разрешение tg_user_id в users.id

Обычный RLS-запрос здесь невозможен: до резолва неизвестен внутренний UUID,
который требуется для `SET LOCAL app.user_id`. Выдавать webapp роль `service`
нельзя.

Принято решение: миграция этапа 9 добавляет узкую функцию
`webapp_resolve_user(bigint)` с `SECURITY DEFINER`, описанную в
`plan/db-schema.md`. Функция:

- возвращает только `user_id` и `status` одной строки `users`;
- имеет фиксированный безопасный `search_path`;
- принадлежит `migrator`;
- отозвана у `PUBLIC` и доступна на `EXECUTE` только роли `app`;
- не принимает и не возвращает токены, timezone, plan или профиль.

Webapp вызывает её только после успешной HMAC-проверки. Неизвестный пользователь
и любой статус кроме `active` получают 403. Невалидный или просроченный initData
получает 401.

Это единственный разрешённый RLS-обход этапа 9. Новых таблиц и колонок нет, но
миграция функции обязательна и должна быть обратно совместимой.

### Сессионный токен

Для v1 выбран `itsdangerous.URLSafeTimedSerializer`, а не JWT: сервер и клиент
одного origin, межсервисной проверки токена нет, дополнительная JWT-семантика не
нужна.

Payload минимален: `{v: 1, sub: users.id}`. TTL — 12 часов, отдельный salt —
`telegram-webapp-session-v1`, ключ — `WEBAPP_SESSION_SECRET` из Infisical.
Токен подписан, но не зашифрован; секретные данные в payload запрещены.

Frontend хранит токен только в памяти. После reload выполняется auth заново.
Каждый API-запрос перед доменной операцией под `user_transaction(sub)` заново
читает `users.status`, `timezone` и `plan`. Поэтому бан, смена плана и таймзоны не
ждут истечения сессии.

При 401 frontend один раз повторяет auth с текущим Telegram initData и затем
повторяет исходный GET. Mutating-запрос автоматически не переигрывается. Если
WebView открыт больше 12 часов и исходный initData уже старше часа, показывается
просьба закрыть и заново открыть Mini App.

## Граница транзакций и доменная логика

HTTP-слой не содержит SQL и бизнес-правил. Целевая структура:

- `webapp/routers/` — HTTP, DTO, status codes;
- `webapp/auth.py` — initData и session token;
- `core/reminders_service.py` — операции над напоминаниями;
- `core/calendar_view.py` — чистая развёртка occurrences и `repeat_human`;
- `core/finance_service.py` — периоды, агрегаты, пагинация, удаление;
- `core/finance_summary.py` — подготовка агрегатов и вызов AI-резюме.

Общие функции, меняющие или читающие пользовательские данные, принимают уже
открытый `asyncpg` connection. Владельцем транзакции остаётся адаптер:

- tools-адаптер открывает `user_transaction(ctx.user_id)` и вызывает сервис;
- webapp-эндпоинт открывает `user_transaction(session.sub)`, проверяет актуальный
  status/timezone/plan и вызывает тот же сервис;
- шедулер сохраняет свой существующий service-путь и не переиспользует
  пользовательский HTTP-адаптер.

Так одна операция выполняется в одной транзакции, а вложенные acquire/transaction
не появляются. Инструменты LLM сохраняют текущие имена, аргументы, ответы,
daily_limit, идемпотентность и `tool_calls_log`.

## Контракт API

Все успешные денежные значения передаются строками с двумя знаками после точки.
Все моменты времени — ISO 8601 с offset в текущей timezone пользователя.

Единая ошибка:

```json
{
  "error": {
    "code": "reminder_time_in_past",
    "message": "Это время уже прошло",
    "trace_id": "uuid"
  }
}
```

`message` можно показывать пользователю; `code` стабилен для логики frontend;
внутренние исключения и SQL в ответ не попадают.

Основные коды статуса:

- 400 — неверный диапазон или cursor;
- 401 — initData/session невалидны или истекли;
- 403 — пользователь не active;
- 404 — запись отсутствует или скрыта RLS;
- 409 — повторная отмена/конфликт состояния;
- 422 — корректный JSON нарушает доменное правило;
- 429 — per-user rate limit;
- 503 — временно недоступимая зависимость без безопасного fallback.

Платформенный endpoint `GET /app/api/me` требует bearer session, открывает
`user_transaction(session.sub)`, проверяет актуальный `users.status` и возвращает
только `{timezone, plan}`. Он нужен frontend bootstrap и является минимальным
RLS-smoke до появления экранных API; Telegram profile и внутренний user_id наружу
не возвращаются.

Cursor финансов — URL-safe base64 от версионированного JSON `{v, ts, id}`. Он не
считается доверенным: сервер полностью валидирует структуру и типы.

## Rate limit

Текущий gateway limiter нельзя использовать с тем же ключом: трафик Mini App не
должен съедать лимит сообщений бота. Общую Lua-механику можно обобщить, сохранив
gateway wrapper без изменения поведения.

- ключ API: `rl:webapp:{user_id}`;
- скорость: 60 запросов/мин;
- burst: 20;
- auth до получения UUID: отдельный грубый лимит по digest initData/IP не является
  надёжной идентичностью; основной контроль — малый request body и Caddy limits.

Не использовать in-memory limiter: контейнер stateless и может масштабироваться.

## Финансовый кэш и AI-резюме

Wildcard deletion и `KEYS`/`SCAN` в request path запрещены. Инвалидация делается
через поколение пользователя:

- `fin:gen:{user_id}` — integer, отсутствие означает 0;
- cache key содержит generation, период и локальную дату;
- add/delete transaction выполняет `INCR fin:gen:{user_id}` после успешного
  commit; старые ключи доживают до TTL и становятся недоступны.

Если Postgres commit прошёл, а Redis INCR упал, endpoint отвечает успехом и
логирует cache-invalidation failure; кэш AI-summary при чтении обязан иметь
короткий TTL 6 часов. Это допустимая eventual consistency только для текста
резюме, финансовые цифры всегда читаются из Postgres.

Для защиты от повторной оплаты LLM используется lock
`fin:summary-lock:{user_id}:{generation}:{from}:{to}` через `SET NX EX`. Второй
запрос коротко ждёт появления результата, но не запускает второй LLM-вызов.

Два инстанса Redis участвуют в AI-резюме, и это разделение обязательно:

- **redis-cache** (эфемерный, allkeys-lru): `fin:gen:{user_id}`, ключ кэша
  `fin_summary:*` и stampede-lock `fin:summary-lock:*`. Потеря этих ключей
  безопасна — пересчитается.
- **redis-queue** (noeviction+AOF, общий с воркером): дневной ₽-счётчик
  `spend_rub:*` (`add_spend`/`get_spent_rub`) и контролы `ResilientLLMProvider`
  (`sem:openrouter`, `cb:openrouter:*`). Эти ключи webapp разделяет с агентом,
  иначе бюджет 5.2 и лимит конкурентности 3.5 раздвоятся. `save_usage` пишет в
  Postgres под RLS (не Redis).

AI-путь использует `ResilientLLMProvider` (на redis-queue, как в воркере),
`UsageRecordingProvider`, `save_usage` с queue=`background`, `add_spend` (на
redis-queue) и общую метрику стоимости. Сырые транзакции в LLM не передаются;
топ-5 крупнейших трат периода (сумма+категория+описание) из
`miniapp-finance-spec.md` — согласованная часть агрегатов, а не «сырой список»,
и стоп-условием не является. При `no_background`, пустом периоде или негативном кэше LLM не
вызывается.

## Frontend

Принятый минимальный стек:

- Vite + Preact + TypeScript strict;
- CSS modules или обычные scoped-by-convention CSS без UI-kit;
- browser `fetch` через один API client;
- native `Intl` для чисел и дат;
- donut и bar chart — доступный SVG собственного компонента, без chart library;
- внутреннее состояние Preact hooks, без Redux и общего state framework.

Два верхнеуровневых route: `calendar` и `finance`. Выбор стартового экрана — hint из
allowlist (`calendar`, `finance`): фронт читает сначала query-параметр `screen`
(из `web_app`-кнопок бота, см. «9.4 Интеграция с ботом»), затем `initDataUnsafe.start_param`
(меню/deep link); неизвестное или пустое значение открывает экран по умолчанию (`calendar`).
`screen` — единственный дополнительный UI-параметр в URL; user_id, session token, период и любые
финансовые параметры в URL не передаются.

Обязательные состояния каждого экрана: loading, empty, error с retry, offline,
unauthorized, content. Удаление и отмена требуют подтверждения; кнопка блокируется
на время запроса. Основная навигация остаётся работоспособной без haptic.

Backend не получает dev-auth bypass. Для локальной вёрстки допускаются только
frontend fixtures/mock Telegram adapter, исключаемые production build. API-тесты
подписывают тестовые сессии тестовым ключом через dependency injection.

### Визуальная система v1

Решение владельца от 2026-07-17: спокойная коричнево-белая палитра, без ярких
акцентов. Базовые design tokens светлой темы:

- page background `#F7F3ED`;
- surface/card `#FFFCF8`;
- primary brown `#6B5142`;
- secondary accent `#92715D`;
- soft accent `#D8C5B7`;
- primary text `#302823`;
- secondary text `#756860`;
- border/divider `#E5DAD0`;
- destructive/error `#A65346`.

Цвета оформляются CSS custom properties, а не литералами компонентов. Для тёмной
темы используются такие же тёплые коричневые отношения на тёмном фоне; конкретные
dark tokens исполнитель подбирает с сохранением WCAG AA для обычного текста.
Telegram `themeParams` определяет режим и может уточнять фон/текст, но не должен
делать интерфейс ярче или разрушать контраст. Синий и фиолетовый маркеры календаря
из ранней UI-спеки заменяются различимыми приглушёнными оттенками коричневого и
терракотового плюс форма/иконка: цвет не является единственным носителем смысла.

## 9.4 Интеграция с ботом

Батч F связывает бота и Mini App, не трогая LLM-инструменты и системный промпт агента.
Механизм фиксируется здесь, потому что реплай-путь воркера сейчас переносит только строку и не
знает, какой инструмент сработал; без этого решения кнопку в ответ не встроить.

### Контракт ответа воркера (какой инструмент сработал → какая кнопка)

- `AgentResult` получает поле `tool_names: tuple[str, ...]` — имена инструментов в порядке
  вызова; заполняется только на ветке `answer` (fallback-ветки budget/tool_limit/timeout/
  malformed остаются с `()` и кнопкой не украшаются). Поле аддитивное, значение по умолчанию
  `()` — существующие конструкторы `AgentResult` не ломаются.
- Чистая функция `mini_app_button_for_tools(tool_names)` (модуль `worker/reply.py`) возвращает
  `MiniAppButton | None`. Сопоставление:
  - финансовый экран (`screen="finance"`, текст «Открыть финансы»): `add_transaction`,
    `query_transactions`, `set_budget`, `get_budget_status`;
  - календарный экран (`screen="calendar"`, текст «Открыть календарь»): `create_reminder`,
    `list_reminders`, `cancel_reminder`.
  Если сработали инструменты обоих экранов — побеждает экран **последнего** релевантного вызова
  (последнее действие пользователя); кнопка всегда одна.
- Реплай-контракт расширяется типом `WorkerReply(text: str, mini_app_button: MiniAppButton | None)`
  (тот же `worker/reply.py`). Протоколы `Processor`/`ContextualProcessor` возвращают
  `str | WorkerReply | None`; строковые ответы всех прочих процессоров остаются валидными
  (обратная совместимость). `AgentProcessor` оборачивает ответ в `WorkerReply` только для
  интерактивного `text`-ответа (не для фоновых `agent_task`/пушей шедулера) и только когда кнопка
  не `None`; иначе возвращает строку, как раньше. В диалог сохраняется только текст — кнопка
  транзиентна.
- `Worker` нормализует ответ: `WorkerReply` с кнопкой уходит через отдельную зависимость
  `send_reply_with_button`; без кнопки или в режимах без Telegram (reply-stream/лог/нет токена)
  посылается только текст через существующий `send_reply` (кнопка молча отбрасывается).

### Тип кнопки и построение URL

- Inline-кнопка — Telegram `web_app`-кнопка (`InlineKeyboardButton.web_app = WebAppInfo(url=…)`),
  допустимая в приватных чатах (бот работает только в них). URL —
  `{PUBLIC_URL}/app/?screen=<finance|calendar>`. Выбран `web_app`, а не `t.me/<bot>?startapp=`,
  чтобы не зависеть от включённого «Main Mini App» в BotFather и от bot username: кнопка открывает
  экран напрямую, зная только `PUBLIC_URL`. `web_app`-кнопки не порождают `callback_query` —
  callback-путь gateway/воркера не меняется.
- URL строится в обвязке `worker/__main__.py` (там, где известен `PUBLIC_URL` и создан `Bot`), а
  не в `TelegramSender` и не в `AgentProcessor`: доменное решение (`MiniAppButton.screen`)
  отделено от транспорта. `send_reply_with_button` вешает кнопку на последний чанк ответа
  (`TelegramSender.send_message(..., reply_markup=…)` уже кладёт markup на последний чанк).
- Конфиг: воркер уже получает `PUBLIC_URL` из Infisical через `core/secrets_entrypoint`
  (`list_all()` мержит все секреты проекта; identity воркера имеет project-wide `viewer`), тем же
  путём, что gateway. Отдельной правки compose/Ansible не требуется. При отсутствии `PUBLIC_URL` в
  окружении — кнопки не строятся (fail-safe), текст уходит.
- Меню бота: идемпотентная команда gateway `set-menu-button` (образец `set-webhook`,
  `gateway/__main__.py`) вызывает `setChatMenuButton(MenuButtonWebApp(text="Ассистент",
  web_app=WebAppInfo(url=f"{PUBLIC_URL}/app/")))`. Требует `PUBLIC_URL`; в deploy-раннере встаёт
  тем же способом, что и `set-webhook`. Меню открывает экран по умолчанию (`calendar`); текст
  кнопки меню — «Открыть».

### Тексты бота и метрики

- `/help` и welcome (`worker/onboarding.py`, `ASSISTANT_CAPABILITIES_TEXT`) дополняются строкой про
  наглядные экраны Mini App (календарь напоминаний и дашборд трат из кнопки меню), без обещаний
  функций v2. Точные формулировки задаёт постановщик (fable/opus), не делегат.
- Продуктовая метрика: счётчик `webapp_app_opened_total` инкрементируется при успешной
  `POST /app/api/auth` (старт сессии/открытие). Использование экранов выводится из
  `webapp_requests_total{route}` (календарь vs финансы). Две панели в
  `infra/grafana/provisioning/dashboards/product.json` на Prometheus-датасорсе (`uid=prometheus`);
  без user-id-лейблов. Новых таблиц/колонок нет.

### Границы батча F

- Frontend меняется минимально: роутинг стартового экрана читает `?screen=` (query) с откатом на
  `start_param`; вынести в чистую функцию для теста. Экраны, API и стили не трогаются.
- Живой e2e (меню и обе inline-кнопки открывают нужный экран на Desktop и мобильном, светлая/
  тёмная тема, неизвестный `screen` → default) и прод-регистрация меню — на activation gate.

## Наблюдаемость

Каждый API-запрос получает trace_id, возвращаемый также в `X-Trace-Id` и в error
envelope. Логи JSON не содержат initData, Authorization, session payload,
описания транзакций и тексты напоминаний.

Минимальные метрики:

- request count/latency по нормализованному route и status;
- auth failures по причине без tg_user_id label;
- rate limited count;
- reminders created/cancelled;
- transactions deleted;
- AI summary outcome: generated/cache_hit/empty/budget_exhausted/unavailable;
- product screen open с конечным label calendar|finance.

Нельзя использовать user_id, transaction id, cursor, raw path или error text как
Prometheus label. Prometheus получает отдельный scrape target `webapp`.

## Секреты и конфигурация

Webapp identity в Infisical получает только необходимое:

- `TELEGRAM_BOT_TOKEN` — проверка initData;
- `WEBAPP_SESSION_SECRET` — отдельный случайный ключ, не bot token;
- `DATABASE_URL` роли app;
- `REDIS_CACHE_URL` — кэш AI-резюме, generation и stampede-lock, rate limit;
- `REDIS_QUEUE_URL` — **только** для AI-резюме (батч E): общий с агентом дневной
  ₽-счётчик расходов (`add_spend`/`spend_rub:*`, инвариант 5.2) и глобальные
  контролы `ResilientLLMProvider` (семафор `sem:openrouter`, circuit breaker
  `cb:openrouter:*`, инвариант 3.5) живут на redis-queue у воркера. Без общего
  инстанса дневной бюджет и лимит конкурентности OpenRouter раздвоились бы между
  ботом и Mini App. webapp пишет в те же ключи, что и воркер, — новой сущности
  состояния это не создаёт; поведение агента не меняется. До батча E переменную
  можно не заводить;
- `OPENROUTER_API_KEY` и существующие OPENROUTER_* настройки для AI-summary.

Не передавать `SERVICE_DATABASE_URL`, encryption KEK, Google OAuth secrets,
Tribute secrets и admin Telegram credentials. В dev/prod compose секреты не
записываются литералами.

## Caddy и security headers

Текущий prod Caddy глобально задаёт `X-Frame-Options: DENY`; это известный блокер
9.1. Для `/app/*` нужен отдельный handler до общего header block:

- удалить `X-Frame-Options` только на Mini App route;
- задать CSP с `default-src 'self'`, разрешением официального Telegram script и
  необходимыми connect/style/img директивами;
- сохранить HSTS, nosniff и строгий Referrer-Policy;
- не ослаблять заголовки webhook и остальных маршрутов;
- assets кэшировать по content hash, `index.html` — без долгого cache.

Точный CSP утверждается по результату первого живого запуска и фиксируется в
плане до изменения кода, если потребуется дополнительный origin.

## Deployment

Webapp добавляется в dev/prod compose как отдельный сервис с app DB URL,
redis-cache и собственной Infisical identity. В prod он зависит от Infisical,
PgBouncer и redis-cache, но не от gateway/worker.

Deploy считается успешным только если healthy gateway, worker, Caddy и webapp.
Образ smoke-тестом импортирует `webapp`; CI отдельно выполняет frontend lint,
typecheck, tests и build. Ansible раскладывает webapp credentials и Caddy config
тем же idempotent способом, что существующие сервисы.

## Тестовая стратегия

Backend:

- unit initData: valid, tampered hash, missing hash/user/auth_date, duplicate,
  expired, future, malformed user JSON;
- unit session: valid, tampered, expired, wrong version;
- integration auth resolver: функция доступна app, скрытые таблицы по-прежнему
  fail-closed, PUBLIC не имеет EXECUTE;
- API auth/RLS на каждом endpoint для двух пользователей;
- регрессионные тесты tools/reminders.py и tools/finance.py;
- календарь: границы, DST, cap 100;
- финансы: direction, periods, cursor, deletion, cache generation, AI branches.

Frontend:

- auth bootstrap и один retry GET после 401;
- mutating request не переигрывается автоматически;
- route по allowlist start_param;
- loading/empty/error states;
- month navigation и period selection;
- confirmation/double-click guards;
- SVG charts с текстовым summary для accessibility.

Живой e2e проводится в Telegram Desktop и хотя бы одном мобильном клиенте,
светлая и тёмная темы. Полная матрица находится в `stage-9-handoff.md`.

## Принятые решения и точки пересмотра

Следующие решения считаются принятыми для v1 и не требуют нового обсуждения:

- один application image, отдельный webapp container;
- Preact без UI-kit и chart library;
- коричнево-белая token-based палитра с WCAG AA и спокойной тёмной темой;
- itsdangerous bearer token только в памяти;
- SECURITY DEFINER resolver вместо service role;
- актуальные status/timezone/plan читаются на каждом API-запросе;
- generation-based invalidation вместо Redis key scan;
- same-origin без CORS/cookies;
- новые таблицы и колонки не нужны.

Остановиться и править план, если реализация требует: отдельного frontend origin,
cookie auth, новой DB-колонки/таблицы, роли service в webapp, внешней аналитики,
нового UI-фреймворка или передачи сырых финансов в LLM.
