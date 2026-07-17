# Handoff исполнителю этапа 9

Дата подготовки: 2026-07-17. Пакеты 9.1A, 9.1B и 9.2 приняты локально; 9.3–9.4 открыты, общие 9.1/9.2 ждут activation gate.

Этот файл — оперативная карта текущего репозитория. Архитектурные нормы находятся
в `plan/stage-9-architecture.md`; при конфликте следовать ему и `stage-9.md`.

## Что уже существует

- `core/db.py`: app pool и `user_transaction` с `SET LOCAL`.
- `core/rate_limit.py`: Redis token bucket обобщён без изменения gateway;
  webapp использует отдельный namespace `rl:webapp:{user_id}`.
- `webapp/`: принят backend-фундамент — FastAPI app, Telegram initData auth,
  itsdangerous-сессия, RLS `/app/api/me`, health/metrics/errors/settings.
- Миграция `20260717_0002_webapp_resolve_user.py`: узкая SECURITY DEFINER-функция
  резолва Telegram ID; новых таблиц и колонок не добавлено.
- `core/llm.py`, `core/llm_resilient.py`: provider contracts, retry, semaphore и
  fallback.
- `core/usage_recorder.py`, `core/usage_store.py`, `core/spend.py`: учёт AI-cost и
  дневной ₽-бюджет.
- `tools/reminders.py`: create/list/cancel reminder, сейчас содержит и адаптер, и
  доменные правила.
- `worker/scheduler.py`: доставка и пересчёт cron; менять семантику нельзя.
- `tools/finance.py`: 492 строки, SQL, периоды, Decimal, бюджеты и PNG-график в
  одном модуле; главный кандидат на безопасный рефакторинг.
- `infra/docker-compose.dev.yml`, `infra/compose.prod.yml`: webapp отсутствует.
- `Dockerfile`: только Python build, не копирует `webapp/` и `miniapp/`.
- `infra/prod/caddy/Caddyfile`: глобальный `X-Frame-Options: DENY`, `/app/*`
  отсутствует.
- `infra/infisical/bootstrap.py`: identities только gateway/worker/scheduler.
- CI собирает Python image; Node job отсутствует.
- Gateway умеет set-webhook, но setChatMenuButton/deep links ещё нет.

## До начала кода

1. Прочитать обязательный комплект из `stage-9-architecture.md`.
2. Проверить чистоту дерева и не затереть пользовательские изменения.
3. Реализовывать строго 9.1 → 9.2 → 9.3 → 9.4.
4. Любой код по регламенту проекта делегировать через skill
   `codex-implementation` с самодостаточной ссылкой на эти документы.
5. После делегата полностью принять diff; мнение делегата не заменяет проверки.

## Рекомендуемые батчи

### Батч A — 9.1 backend foundation

Статус: **принят локально 2026-07-17**. Отчёт и результаты проверок:
`docs/stage-9-1a-acceptance-2026-07-17.md`.

Планируемые файлы:

- `webapp/__init__.py`, `webapp/__main__.py`, `webapp/app.py`;
- `webapp/auth.py`, `webapp/dependencies.py`, `webapp/errors.py`;
- `webapp/metrics.py`, `webapp/settings.py`;
- миграция `webapp_resolve_user`;
- unit/integration tests auth, session, resolver, RLS, rate limit.

Добавить `itsdangerous` как прямую Python dependency. `webapp` включить в hatch,
mypy и image smoke-import. Не добавлять PyJWT параллельно.

Definition of done батча: auth branches, актуальный active-status под RLS,
отдельный rate-limit namespace, health/metrics и тест двух пользователей. UI пока
может быть статическим shell.

### Батч B — 9.1 frontend and deployment

Статус: **принят локально 2026-07-17**. Отчёт и результаты проверок:
`docs/stage-9-1b-acceptance-2026-07-17.md`. Реальный Telegram WebView и prod
activation отложены решением владельца до завершения 9.2–9.4.

Планируемые области:

- `miniapp/package.json`, lockfile, Vite/TS/lint/test config;
- Telegram adapter, API client, app shell, theme tokens, общие состояния;
- Node build-stage в основном Dockerfile;
- dev/prod compose, Infisical identity/secrets, Prometheus target;
- Caddy route/header split, Ansible/deploy health checks;
- menu-button deploy command допускается подготовить здесь, активировать в 9.4.

Production image не должен содержать `node_modules` и исходный Node toolchain.
Проверить, что `/app/api/unknown` даёт JSON 404, а не SPA.

Визуальная система уже утверждена владельцем: токены и правила находятся в
`stage-9-architecture.md`. Не заменять палитру Telegram blue, не использовать
яркие градиенты; проверить WCAG AA и светлую/тёмную тему.

Definition of done батча: image и frontend собираются в CI, webapp healthy в
dev/prod topology, живой Telegram auth проходит, X-Frame-Options не ломает Mini
App и не ослаблен для остальных routes.

### Батч C — 9.2 calendar

Статус: **принят локально 2026-07-17**. Отчёт и результаты проверок:
`docs/stage-9-2-acceptance-2026-07-17.md`. Живая доставка в Telegram и проверка
done через реальный WebView отложены до разрешённого production activation.

Сначала вынести сервисные функции из `tools/reminders.py`, затем подключить к ним
старые tools и только после зелёной регрессии добавлять API.

Планируемые файлы:

- `core/reminders_service.py`;
- `core/calendar_view.py`;
- `webapp/routers/calendar.py`;
- frontend calendar components и tests;
- регрессия `tests/test_reminders.py`, integration scheduler/RLS.

Не менять: имена инструментов, JSON args, daily_limit 30, лимит 25 активных,
cron-семантику шедулера и tool_calls_log.

### Батч D — 9.3 finance data

Статус: **принят локально 2026-07-17**. Отчёт и результаты проверок:
`docs/stage-9-3d-acceptance-2026-07-17.md`. Реализовано: `core/finance_service.py`
(общая доменная логика без регрессии `tools/finance.py`), summary/transactions
(keyset)/DELETE API под RLS, фронт-дашборд с вкладками. AI-резюме и generation-кэш
осознанно не входили — они в батче E.

Сначала вынести периоды/Decimal/SQL из `tools/finance.py`. PNG-график бота
оставить в tools-адаптере. Затем добавить summary, keyset list и delete API.

Планируемые файлы:

- `core/finance_service.py`;
- `webapp/routers/finance.py`;
- frontend selector, summary, SVG charts, budgets, transaction list;
- tests direction/period/DST/cursor/RLS/delete/cache generation.

Сравнить live суммы с `query_transactions`; нельзя создавать вторую формулу
агрегации только для API.

### Батч E — 9.3 AI summary

Статус: **принят локально 2026-07-17**. Отчёт: `docs/stage-9-3e-acceptance-2026-07-17.md`.
Реализовано: `core/finance_summary.py`, `GET /app/api/finance/ai-summary`, ResilientLLMProvider
для webapp на redis-queue (общий с ботом ₽-бюджет/семафор), generation-кэш + stampede-lock на
redis-cache, инвалидация `fin:gen` в add_transaction+DELETE, фронт-карточка. Следующий пакет —
**батч F (9.4)**.

- `core/finance_summary.py` или эквивалентный provider-neutral service;
- инициализация ResilientLLMProvider для webapp **на redis-queue** (как в
  `worker/__main__.py`): семафор `sem:openrouter`, breaker `cb:openrouter:*` и
  дневной ₽-счётчик `spend_rub:*` общие с агентом — webapp получает
  `REDIS_QUEUE_URL` (правка stage-9-architecture.md §«Секреты и конфигурация»);
- generation cache + stampede lock **на redis-cache** (`fin:gen`,
  `fin_summary:*`, `fin:summary-lock:*`); инвалидация — `INCR fin:gen` в
  add_transaction (доменная функция) и в DELETE-эндпоинте (добавить оба — в батче
  D их сознательно не было);
- usage (queue=background, save_usage под RLS) + `add_spend` на redis-queue +
  метрика исхода AI-summary (generated/cache_hit/empty/budget_exhausted/unavailable);
- бюджет-гейт: при `BudgetState >= NO_BACKGROUND` (spent ≥ дневного бюджета) LLM
  не вызывается → `{status: "budget_exhausted"}`; пустой период → `{status:
  "empty"}`; ошибка/таймаут LLM → `{status: "unavailable"}` c кэшем на 10 минут;
- MockLLM tests всех веток (без сети).

Не переиспользовать AgentLoop и системный prompt агента. Это один прямой generate
по агрегатам, максимум около 300 output tokens.

### Батч F — 9.4 bot integration and polish

Статус: **принят локально 2026-07-18**. Отчёт: `docs/stage-9-4-acceptance-2026-07-18.md`.
Реализовано всё ниже: `AgentResult.tool_names` → `worker/reply.py` → `Worker._deliver_reply`;
inline web_app-кнопки на `{PUBLIC_URL}/app/?screen=…`; gateway `set-menu-button`; тексты
welcome/`/help`; метрика `webapp_app_opened_total` + панели Grafana; фронт `resolveStartScreen`.
Живой e2e и прод-регистрация меню — на activation gate. Полный механизм — `stage-9-architecture.md`
§«9.4 Интеграция с ботом». Кратко — состав и файлы:

- **Сигнал инструмента**: `core/agent.py` — `AgentResult` получает `tool_names:
  tuple[str, ...]` (по умолчанию `()`, заполняется на ветке `answer` в порядке
  вызова). Не менять остальные поля/семантику стоп-причин.
- **Контракт ответа**: новый `worker/reply.py` — `MiniAppButton(text, screen)`,
  `WorkerReply(text, mini_app_button)` и чистая `mini_app_button_for_tools(...)`
  (finance-инструменты → «Открыть финансы»/`finance`; reminder-инструменты →
  «Открыть календарь»/`calendar`; при обоих — экран последнего релевантного вызова).
- **`worker/processor.py`**: протоколы `Processor`/`ContextualProcessor` →
  `str | WorkerReply | None`.
- **`worker/agent_processor.py`**: только для интерактивного `text`-ответа обернуть
  реплай в `WorkerReply`, когда кнопка не `None`; в диалог сохраняется лишь текст.
- **`worker/app.py`**: `Worker` нормализует `str | WorkerReply | None`, шлёт кнопку
  через опциональную зависимость `send_reply_with_button` (нет её / нет кнопки →
  только текст).
- **`worker/__main__.py`**: собрать `send_reply_with_button` (web_app-кнопка на
  `{PUBLIC_URL}/app/?screen=<screen>` через `sender.send_message(reply_markup=…)`)
  при наличии токена и `PUBLIC_URL`; иначе `None`. `WebAppInfo` из `aiogram.types`.
- **`worker/__main__.py` (`_run`)**: прочитать `PUBLIC_URL` через `os.getenv` (тем же
  паттерном, что `WORKER_REPLY_STREAM`/`SEARXNG_URL`); fail-safe при отсутствии.
- **`gateway/__main__.py`**: команда `set-menu-button` по образцу `_set_webhook`
  (`setChatMenuButton(MenuButtonWebApp(web_app=WebAppInfo(url=f"{public_url}/app/")))`,
  требует `PUBLIC_URL`).
- **`worker/onboarding.py`**: строка про Mini App в `ASSISTANT_CAPABILITIES_TEXT`
  (влияет на welcome и `/help`), без обещаний v2. Текст даёт постановщик.
- **`webapp`**: счётчик `webapp_app_opened_total` (metrics.py) + инкремент при
  успешной `POST /app/api/auth`.
- **`miniapp/src/app.tsx`** (+ `telegram.ts` при нужде): роутинг стартового экрана
  читает `?screen=` (query) с откатом на `start_param`; вынести в чистую функцию
  (тест `app.test.ts`). Allowlist `{calendar, finance}`, неизвестное → default.
- **Grafana**: две панели в `product.json` (открытия Mini App; использование экранов
  по `webapp_requests_total`) на Prometheus-датасорсе.
- **Конфиг деплоя**: правок не требуется — воркер уже получает `PUBLIC_URL` из Infisical
  через `secrets_entrypoint` (`list_all`, project-wide viewer), как gateway. Fail-safe при
  отсутствии.

Тесты (offline): `AgentResult.tool_names` заполнение; `mini_app_button_for_tools`
(finance/calendar/оба/пусто); `Worker` доставляет `WorkerReply` с кнопкой и без;
`set-menu-button` строит корректный `MenuButtonWebApp`; `webapp_app_opened_total`
инкрементится на auth; фронт-роутинг из `?screen=`/`start_param`; compose dev/prod
валидны; `product.json` — валидный JSON.

Не добавлять текст Mini App в prompt агента: stage-9 явно запрещает ненужную
prompt-правку. Живой e2e и прод-регистрация меню — на activation gate; для inline —
кнопки строятся при заданном `PUBLIC_URL`.

## Контракты, которые нельзя сломать

- gateway остаётся без Postgres;
- webapp не получает service role;
- user_id не приходит из frontend body/query/path;
- app-транзакции используют только `SET LOCAL` через `user_transaction`;
- API-вызовы не пишутся в `tool_calls_log`;
- инструменты LLM продолжают туда писаться;
- mutating tools сохраняют существующую идемпотентность;
- API mutation не получает автоматический retry на клиенте;
- тесты не ходят в Telegram/OpenRouter и используют mocks;
- в БД остаётся 13 таблиц; resolver — функция, не новая сущность данных;
- cancelled calendar items скрыты, done one-off видны приглушённо;
- income не попадает в expense totals;
- деньги сериализуются строкой, не float.

## Матрица API и прав

| Endpoint | Auth | DB | Side effect | Повтор клиентом |
|---|---|---|---|---|
| POST `/app/api/auth` | Telegram HMAC | resolver function | session token | один раз |
| GET `/app/api/me` | bearer | app + RLS | нет | допустим |
| GET `/app/api/calendar` | bearer | app + RLS | нет | допустим |
| POST `/app/api/reminders` | bearer | app + RLS | INSERT | автоматически нет |
| DELETE `/app/api/scheduled/{id}` | bearer | app + RLS | UPDATE | автоматически нет |
| GET `/app/api/finance/summary` | bearer | app + RLS | нет | допустим |
| GET `/app/api/finance/transactions` | bearer | app + RLS | нет | допустим |
| DELETE `/app/api/finance/transactions/{id}` | bearer | app + RLS | DELETE | автоматически нет |
| GET `/app/api/finance/ai-summary` | bearer | app + RLS | LLM/usage/spend/cache | только через cache/lock |

## Живой e2e чек-лист

### 9.1

- активный владелец открывает Mini App из Telegram;
- tampered/expired initData отклоняется;
- pending/rejected/banned не входят;
- пользователь A не видит данные B;
- после смены status/timezone уже выданная сессия видит новое состояние;
- 61+ быстрых запросов демонстрируют rate limit;
- `/app/api/unknown` и `/app/unknown` различаются корректно;
- проверить Desktop и мобильный клиент, светлую и тёмную темы.

### 9.2

- one-off, recurring reminder и agent_task видны в правильных локальных днях;
- создать +5 минут → получить сообщение → увидеть done;
- отменить reminder и agent_task → бот больше не показывает;
- месяц с DST и cap частого cron покрыты тестами.

### 9.3

- месяц и custom range совпадают с query_transactions;
- category filter и две страницы не дают дублей/пропусков;
- income/expense разделены;
- удалить → суммы бота меняются;
- AI первый раз пишет usage/spend, второй раз cache hit;
- mutation меняет generation и старое резюме не возвращается;
- no_background/empty/unavailable не вызывают лишний LLM.

### 9.4

- menu button и обе inline-кнопки открывают нужный экран;
- неизвестный start_param безопасно ведёт на default;
- `/help` и welcome не обещают v2-функции;
- продуктовые метрики видны без user-id labels.

## Полная приёмка каждого батча

1. Просмотреть весь diff и проверить отсутствие чужих изменений.
2. Frontend: install из lockfile, lint, typecheck, unit tests, build.
3. Backend: `ruff`, format check, `mypy`, unit tests.
4. Integration tests с dev Postgres/Redis и RLS.
5. Compose config dev/prod, image build и healthchecks.
6. Gitleaks по git history/diff.
7. Ручной e2e из раздела выше; для инструментов проверить `tool_calls_log`.
8. Зелёный CI.
9. Создать отдельный отчёт приёмки
   `docs/stage-9-<батч>-acceptance-YYYY-MM-DD.md`: scope, изменённые файлы,
   решения, тесты/e2e, исправленные дефекты, ограничения и handoff следующему
   исполнителю.
10. Только после этого отметить соответствующий пункт `progress.md` и добавить
    краткую ссылку-резюме в `docs/dev-log.md`.

## Известные риски

- Global Caddy `X-Frame-Options: DENY` уже конфликтует с WebView.
- Telegram initData нельзя полноценно получить из обычного браузера; backend dev
  bypass запрещён.
- Исходный initData старше часа не обновит истёкшую 12-часовую сессию: пользователю
  потребуется переоткрыть Mini App.
- Redis invalidation после Postgres commit имеет небольшое окно eventual
  consistency; generation + TTL ограничивают ущерб устаревшим AI-текстом.
- Рефакторинг `tools/finance.py` — наибольший риск регрессии этапа.
- Frontend — первый Node-проект в монорепо; lockfile и CI должны появиться в одном
  батче, нельзя оставлять плавающие версии.

## Что остаётся после 9.1A–9.2

Порядок оставшихся работ: **батч D (9.3 finance data) → батч E (9.3 AI summary) →
батч F (9.4)**, затем activation gate. E зависит от D (`core/finance_service.py`
и summary API появляются в D); F — после обоих.

Не реализованы экран 9.3 (батчи D, E) и интеграция с ботом 9.4 (батч F). Общие
пункты 9.1 и 9.2 остаются открытыми только до живого Telegram e2e и production
activation: identity и secret values создаются перед отдельным разрешённым
deploy, а не в текущем локальном цикле. Activation gate закрывает чекбоксы
9.1–9.4 в progress.md разом: deploy по runbook, живой e2e из матрицы выше
(разделы 9.1–9.4) владельцем на проде.

## Ограничение текущего цикла

Решение владельца от 2026-07-17: реализовать и полностью проверить этап локально,
но не выполнять deploy на сервер до отдельной команды. Запрещены push-triggered
prod deploy, Ansible apply на VPS, изменение prod Infisical/BotFather и живое
переключение Caddy. Prod-конфиги можно подготовить и валидировать локально.
