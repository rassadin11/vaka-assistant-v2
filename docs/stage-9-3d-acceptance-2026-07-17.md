# Приёмка этапа 9.3, батч D — данные финансового дашборда Mini App

Дата: 2026-07-17
Статус: локальная реализация принята
Исполнитель кода: Codex (gpt-5.5), спека и приёмка — Claude (opus)
Прод-развёртывание: не выполнялось (activation gate)

## Объём принятой части

Батч D покрывает данные экрана «Финансы» без AI-резюме (AI-резюме и generation-кэш —
отдельный батч E):

- рефакторинг `tools/finance.py`: общая доменная логика вынесена в
  `core/finance_service.py`, поведение инструментов бота не изменилось;
- API `GET /app/api/finance/summary` — итоги по direction, сравнение с прошлым периодом,
  donut-категории с долей, динамика по bucket'ам, бюджеты только для календарного месяца;
- API `GET /app/api/finance/transactions` — keyset-пагинация `(ts, id) DESC`, непрозрачный
  версионированный курсор, опциональный фильтр категории;
- API `DELETE /app/api/finance/transactions/{id}` — удаление своей строки под RLS
  (чужая невидима → 404), 204, метрика `webapp_transactions_deleted_total`;
- фронт-дашборд: селектор периода, сводка, donut+столбики (accessible SVG), бюджеты,
  список транзакций с пагинацией/фильтром/удалением; рабочие вкладки Календарь/Финансы и
  allowlist `start_param`.

## Изменённые/новые файлы

- `core/finance_service.py` (новый) — общая финансовая доменная логика: суммы (Decimal),
  границы периодов в таймзоне пользователя, SQL-агрегаты, bucket-гранулярность,
  keyset-пагинация, кодек курсора, удаление.
- `tools/finance.py` — переведён на shared-хелперы через алиасы импорта; внешнее поведение
  инструментов, ToolResult, daily_limit, tool_calls_log и PNG-график сохранены.
- `webapp/routers/finance.py` (новый) — summary/transactions/DELETE эндпоинты.
- `webapp/app.py` — регистрация finance-роутера после calendar-роутера.
- `webapp/metrics.py` — счётчик `webapp_transactions_deleted_total`.
- `miniapp/src/api.ts` — типы и три finance-клиента со строгой runtime-валидацией ответа.
- `miniapp/src/finance.tsx` (новый) — экран финансов, SVG-графики, фильтры, пагинация, DELETE.
- `miniapp/src/app.tsx` — вкладки Календарь/Финансы, `start_param` allowlist.
- `miniapp/src/telegram.ts` — типизация `initDataUnsafe.start_param`.
- `miniapp/src/styles.css` — стили финансов (коричневая палитра категорий, графики,
  бюджеты, вкладки, состояния, `.visually-hidden`).
- `tests/test_finance_service.py`, `tests/test_webapp_finance.py`,
  `tests/integration/test_webapp_finance.py`, `miniapp/src/finance.test.tsx`,
  `miniapp/src/app.test.ts`, `miniapp/src/api.test.ts` — юнит/API/интеграция/фронт-тесты.

## Проверенные инварианты (CLAUDE.md)

- RLS: доступ к данным только через `active_request_user`/`user_transaction` (роль `app`,
  без `service`, без session-level SET).
- `user_id` берётся только из подписанной сессии; курсор пагинации user_id не содержит и RLS
  не ослабляет.
- Деньги сериализуются строкой с двумя знаками, не float; время в БД timestamptz UTC, в API —
  ISO 8601 в таймзоне пользователя.
- API не пишет в `tool_calls_log`; LLM-инструменты продолжают писаться.
- Новых таблиц/колонок нет; миграции батч не добавляет (13 таблиц).
- Код/идентификаторы — английский; тексты интерфейса и ошибок — русский; тесты офлайн.
- Батч E не затронут: нет AI-summary, `fin:gen` и его инвалидации.

## Автоматические проверки (прогнаны Claude независимо)

- `uv run ruff check .` — PASS (All checks passed).
- `uv run ruff format --check .` — PASS (158 файлов отформатированы).
- `uv run mypy` — PASS (no issues in 79 source files).
- `uv run pytest -q` — PASS: `324 passed, 33 skipped` (integration финансов скипается офлайн).
- Целевой прогон `test_finance_service.py + test_webapp_finance.py + test_finance_tools.py`
  — 17 passed (регресс инструментов сохранён).
- miniapp: `npm run lint`, `npm run typecheck` — PASS; `npm test` — 21 passed (finance 6,
  calendar 7, api 6, app 2); `npm run build` — PASS.
- Секрет-скан батч-файлов (gitleaks не установлен → ручной pattern-scan, как в 9.2) —
  совпадений нет, только имена переменных/тест-фикстуры.

## Проверка глубины тестов (риск «брошенного хвоста» Codex)

Интеграционный тест содержателен и математически сверен вручную: разделение expense/income
(итог 177.25 против дохода 1000 — регресс today_total), RLS-изоляция A/B, пагинация 50+4 без
дублей/пропусков, фильтр категории, бюджет только для календарного месяца, foreign-delete →
404 с сохранением строки, own-delete → 204 с корректными суммами после удаления. Фронт-тесты
покрывают все сценарии спеки (дефолтный месяц + accessible-графики, empty-state, custom-range +
фильтр, пагинация без дублей, guard двойного клика удаления, 401-refresh для GET и запрет
авто-реплея DELETE).

## Решения Codex, принятые на приёмке

- `prev_period` считается существующим при ≥1 транзакции в прошлом периоде, включая
  income-only (тогда `expense="0.00"`). Фронт гасит сравнение при `previous > 0` — деления на
  ноль и вводящего в заблуждение процента нет; пользовательское поведение совпадает со спекой
  «нет данных → не показывать». Приемлемо.
- Список транзакций и агрегаты фильтруют `currency='RUB'`; отображение иной валюты отнесено к
  мультивалютному v2 (граница спеки). Приемлемо.
- Текущая неделя — с понедельника по сегодняшний локальный день; `start_param` — только hint из
  allowlist `calendar|finance`.

## Ограничения и следующий шаг

- Живой e2e (реальный Telegram WebView, сверка сумм с ответом бота, удаление → изменение суммы у
  бота) и прод-развёртывание остаются за activation gate (решение владельца от 2026-07-17 —
  работаем локально до отдельной команды). Интеграционный тест финансов проверяется при
  доступной dev-БД.
- Следующий пакет — **батч E (9.3 AI-резюме)**: `core/finance_summary.py`, инициализация
  ResilientLLMProvider для webapp, generation-кэш (`fin:gen`) + stampede-lock, usage/spend/
  метрики, `GET /app/api/finance/ai-summary`, инвалидация кэша (INCR `fin:gen`) в
  add_transaction и DELETE.
