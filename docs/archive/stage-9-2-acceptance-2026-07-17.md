# Приёмка этапа 9.2 — календарь Mini App

Дата: 2026-07-17
Статус: локальная реализация принята
Исполнитель: GPT-5.6 Terra
Прод-развёртывание: не выполнялось

## Объём принятой части

Реализован календарный экран поверх существующей таблицы `scheduled_tasks`:

- сетка месяца с хвостами недель, навигацией, свайпом и переходом к сегодняшней дате;
- напоминания и фоновые agent tasks в локальных днях пользователя;
- развёртка cron-повторов, человекочитаемые подписи и cap частых расписаний;
- список выбранного дня, приглушённые done one-off и скрытые cancelled;
- создание one-off напоминания и отмена reminder/agent_task;
- общий доменный слой для Mini App API и прежних LLM-инструментов;
- RLS/auth/rate-limit API без новых таблиц и без записей API в `tool_calls_log`.

## Изменённые файлы

- `core/calendar_view.py` — чистая календарная развёртка и `repeat_human`;
- `core/reminders_service.py` — общие create/cancel/query операции внутри caller-owned транзакции;
- `tools/reminders.py`, `tools/scheduled.py` — адаптация старых инструментов к общему сервису;
- `webapp/routers/calendar.py`, `webapp/app.py`, `webapp/metrics.py` — calendar API и метрики;
- `miniapp/src/calendar.tsx`, `app.tsx`, `api.ts`, `telegram.ts`, `styles.css` — UI, API и Telegram buttons;
- `tests/test_calendar_view.py`, `tests/test_webapp_calendar.py`, `tests/test_reminders.py` — unit/API/tool regression;
- `tests/integration/test_webapp_calendar.py` — живой PostgreSQL/Redis RLS-сценарий;
- `miniapp/src/calendar.test.tsx`, `api.test.ts` — frontend helpers/components/API/session tests.

## Зафиксированные решения

- Общие сервисы получают уже открытую app-role RLS-транзакцию; session-level `SET` и роль `service` в webapp не используются.
- API валидирует текст 1..500 как transport contract. Историческая `CreateReminderArgs` и все ToolResult/error/payload контракты LLM-инструмента сохранены.
- Инструмент сохраняет daily limit 30 и обычный dispatcher/tool_calls_log flow; Mini App API использует только webapp rate limit и в tool log не пишется.
- Calendar range включает обе локальные даты и ограничен 62 днями. На одну cron-задачу возвращается не более 100 occurrences с `truncated=true`.
- Cancel под RLS различает невидимую/несуществующую задачу (404) и terminal state (409); повтор мутации клиентом автоматом не выполняется.
- Истёкшая сессия для GET обновляется через Telegram initData ровно один раз с единственным повтором GET. Для POST/DELETE сессия обновляется, но пользователь повторяет действие явно.
- Маркеры различаются не только цветом: reminder — круг, agent task — терракотовый ромб.

## Автоматические проверки

- frontend ESLint и TypeScript — успешно;
- frontend Vitest — 10 passed;
- frontend production build — успешно (`25.78 kB` JS, `5.69 kB` CSS до gzip);
- `uv run ruff check .` — успешно;
- `uv run ruff format --check .` — 153 файла отформатированы;
- `uv run mypy` — успешно, 77 исходных файлов;
- полный offline pytest — 314 passed, 32 skipped;
- `git diff --check` — чисто;
- pattern scan новых областей на форматы токенов/private keys — совпадений нет;
- calendar webapp/core не содержит ссылок на `tool_calls_log`.

Полный Gitleaks по истории был чист непосредственно перед frontend/календарными пакетами. Повторный repo mount в GHCR-контейнер отклонён управляемой политикой безопасности; локального executable в среде нет. Реальные секреты в 9.2 не добавлялись.

## Локальный интеграционный e2e

На временно поднятых локальных PostgreSQL, PgBouncer и Redis выполнены три живых теста:

1. пользователь A создаёт напоминание через HTTP API, видит его и done agent task в своём календаре;
2. задача пользователя B невидима и не отменяется пользователем A (404);
3. отменённое напоминание исчезает из календаря, done даёт 409;
4. прежние reminder и scheduled-task инструменты сохраняют create/list/cancel и RLS-изоляцию.

Результат: 3 passed. Тестовые данные удалены самими fixtures; временные сервисы остановлены с сохранением локальных volumes.

## Production image

Образ `personal-assistant:webapp-stage9-calendar` успешно собран. Проверено, что runtime не содержит Node, импортирует `core.calendar_view`, `core.reminders_service` и `webapp`, а hashed frontend bundle содержит календарный экран.

## Дефекты, найденные при приёмке

- До кода устранено противоречие плана между API text 1..500 и запретом менять прежний LLM tool contract.
- Добавлен отсутствовавший одноразовый re-auth для истёкшей сессии; мутации не переигрываются.
- Исправлен empty-month: события только в хвосте соседнего месяца больше не скрывают пустой placeholder текущего месяца.
- Стабилизирован Preact test harness с отдельным `act`/effect flush.
- Усилены frontend-тесты: week tails, timezone today, маркеры, recurring/done, double-click guard, auth refresh и no mutation replay.

## Ограничения и следующий шаг

Полный scheduler integration в общей dev-БД встретил ранее существовавшую чужую active `agent_task` раньше тестовой строки. Запись не удалялась; scheduler-код в 9.2 не менялся, его unit regression и изолированные RLS/service integrations зелёные. Это ограничение загрязнённой локальной БД, а не принятое изменение продукта.

Не выполнялись VPS/Infisical/BotFather/webhook/menu/push/deploy. Живой e2e владельца «Mini App → +5 минут → сообщение в Telegram → done в календаре» остаётся activation gate после реализации 9.3–9.4.

Следующий пакет: 9.3D — общие финансовые агрегаты, API, пагинация/удаление и frontend-дашборд без AI-summary.
