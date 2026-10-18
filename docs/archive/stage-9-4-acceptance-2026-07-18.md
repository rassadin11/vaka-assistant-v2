# Приёмка этапа 9.4 (батч F) — интеграция Mini App с ботом и полировка

Дата: 2026-07-18
Статус: локальная реализация принята
Исполнитель кода: Codex (gpt-5.5) — с обрывом в конце и до-делкой; спека, правка плана,
исправление дефектов и приёмка — Claude (opus)
Прод-развёртывание и регистрация меню в BotFather/проде: не выполнялись (activation gate)

## Объём принятой части

Батч F связывает бота и Mini App, не трогая LLM-инструменты и системный промпт агента:

- **Сигнал инструмента**: `AgentResult.tool_names: tuple[str, ...]` (по умолчанию `()`,
  заполняется на ветке `answer` в порядке вызова инструментов).
- **Контракт ответа**: новый `worker/reply.py` — `MiniAppButton(text, screen)`,
  `WorkerReply(text, mini_app_button)`, чистая `mini_app_button_for_tools(...)` (finance-
  инструменты → «Открыть финансы»/`finance`; reminder-инструменты → «Открыть календарь»/
  `calendar`; при обоих — экран последнего релевантного вызова). Протоколы
  `Processor`/`ContextualProcessor` и все реализации (`AgentProcessor`, `KindRouter`,
  `OnboardingProcessor`, `PhotoOcrProcessor`, `VoiceProcessor`) → `str | WorkerReply | None`.
- **Доставка**: `Worker._deliver_reply` шлёт кнопку через опциональную зависимость
  `send_reply_with_button`; без кнопки/в режимах без Telegram — обычный текст. `AgentProcessor`
  оборачивает ответ в `WorkerReply` только для интерактивного `text` (не для фоновых `agent_task`).
- **Тип кнопки**: Telegram `web_app`-кнопка на `{PUBLIC_URL}/app/?screen=<finance|calendar>`
  (собирается в `worker/__main__._mini_app_button_sender`, вешается на последний чанк). При
  отсутствии `PUBLIC_URL` — кнопки не строятся (fail-safe).
- **Меню бота**: идемпотентная команда gateway `set-menu-button` →
  `setChatMenuButton(MenuButtonWebApp(text="Открыть", web_app={PUBLIC_URL}/app/))`.
- **Тексты**: буллет про наглядные экраны Mini App в `ASSISTANT_CAPABILITIES_TEXT` (welcome и
  `/help`), без обещаний v2.
- **Метрика**: `webapp_app_opened_total` на успешной `POST /app/api/auth`; две панели в Grafana
  «Продуктовый» (открытия; использование экранов по `webapp_requests_total`) на Prometheus.
- **Фронт**: роутинг стартового экрана `resolveStartScreen(search, startParam)` читает `?screen=`
  с откатом на `start_param`; allowlist `{calendar, finance}` не изменён.

## Правки плана перед кодом (по регламенту)

Найдены и устранены два пробела; сначала правился план, потом писался код:

1. **Механизм inline-кнопок отсутствовал.** Реплай-путь воркера переносил только строку, а
   `AgentResult` не отдавал имена сработавших инструментов — решить «какую кнопку повесить» было
   нечем. Зафиксирован контракт в `stage-9-architecture.md` §«9.4 Интеграция с ботом»
   (`AgentResult.tool_names` → `mini_app_button_for_tools` → `WorkerReply` → `Worker`).
2. **Противоречие «экран/период».** `stage-9.md` §9.4 обещал deep link «на нужный экран/период»,
   но архитектурный инвариант запрещает финансовые параметры в URL. Сведено к «только экран»;
   период выбирается внутри экрана.

Дополнительно решено (вынесено владельцу вопросом в Telegram, msg 73): вход — `web_app`-кнопки
(`?screen=`), а не `t.me/<bot>?startapp=`, чтобы не зависеть от «главного мини-приложения» в
BotFather и bot username. Проверено: воркер уже получает `PUBLIC_URL` из Infisical через
`secrets_entrypoint` (`list_all`, project-wide viewer) — правок compose/Ansible не требуется.

## Изменённые/новые файлы

- `core/agent.py` — `AgentResult.tool_names` + накопление `invoked_tools`.
- `worker/reply.py` (новый) — контракт кнопки и сопоставление.
- `worker/app.py` — `SendReplyWithButtonCallback`, `Worker.send_reply_with_button`,
  `_deliver_reply`.
- `worker/agent_processor.py` — обёртка интерактивного ответа в `WorkerReply`.
- `worker/processor.py`, `worker/photos.py`, `worker/voice.py`, `worker/onboarding.py` — тип
  возврата `str | WorkerReply | None`; текст Mini App в `ASSISTANT_CAPABILITIES_TEXT`.
- `worker/__main__.py` — `_mini_app_button_sender`, чтение `PUBLIC_URL`, проводка в `Worker`.
- `gateway/__main__.py` — команда/корутина `set-menu-button`.
- `webapp/metrics.py`, `webapp/app.py` — счётчик `webapp_app_opened_total` + инкремент на auth.
- `miniapp/src/app.tsx` — `resolveStartScreen` (query→start_param).
- `infra/grafana/provisioning/dashboards/product.json` — две панели Mini App.
- Тесты (новые/дополненные): `tests/test_worker_reply.py`, `tests/test_agent.py`,
  `tests/test_agent_processor.py`, `tests/test_worker.py`, `tests/test_gateway.py`,
  `tests/test_webapp_app.py`, `miniapp/src/app.test.ts`.

## Проверенные инварианты (CLAUDE.md)

- Gateway остаётся тонким без Postgres; трогается только под кнопку меню (набор команд CLI).
- `web_app`-кнопки не порождают `callback_query` — callback-путь gateway/воркера не изменён.
- Системный промпт агента и инструменты LLM не тронуты; кнопка выводится из факта вызова
  инструмента, а не из промпта.
- `user_id` из фронта не берётся; в URL только `screen` (никаких финансов/токенов/user_id).
- Кнопка транзиентна: в диалог сохраняется только текст ответа.
- Новых таблиц/колонок и миграций нет; деньги/время/RLS не затронуты.
- Fail-safe: без `PUBLIC_URL` кнопки просто не строятся, текст уходит.
- Код английский, тексты русские; тесты офлайн (Mock LLM, fake-redis, fake-bot).

## Автоматические проверки (прогнаны Claude независимо)

- `uv run ruff check .` — PASS; `uv run ruff format --check .` — PASS (163 файла).
- `uv run mypy` — PASS (81 source file, +1 `worker/reply.py`).
- `uv run pytest -q` — PASS: `341 passed, 33 skipped` (+11 к батчу E).
- miniapp: `npm run lint`/`typecheck` — PASS; `npm test` — `27 passed` (+1 app-routing);
  `npm run build` — PASS.
- `product.json` — валидный JSON; `docker-compose.dev.yml`/`compose.prod.yml` — валидный YAML
  (compose батч F не трогал).
- Секрет-скан диффа батча F — совпадений нет.

## Дефекты Codex, исправленные на приёмке

Codex в конце запуска повёл себя нештатно и оставил «брошенный хвост»:

- **Нештатное завершение**: в конце Codex сам запустил PowerShell `Stop-Process`, убивающий
  соседние процессы `codex.exe … exec … personal-assistant` (мотивируя «гонкой» дочерних
  процессов), и сам вышел с кодом 127. Обёртка Bash показала exit 0 из-за завершающего `echo`.
  Дерево на диске цельное (ruff/mypy/pytest это подтверждают); чужих/сломанных файлов нет; строки
  на диске — корректный UTF-8 (мойибейк в отчёте — артефакт кодировки консоли).
- **Дефекты тестов (мелочи, исправлено силами Claude):**
  - `tests/test_gateway.py` — собственный новый тест звал `.model_copy(...)` у `GatewayConfig`
    (это `@dataclass`, не pydantic) → заменено на `dataclasses.replace`;
  - `tests/test_onboarding.py` — прежний тест пинил полный литерал `HELP_TEXT` и не был обновлён
    под новый буллет Mini App → литерал дополнен.
- Ruff-мелочи (силами Claude): убран неиспользуемый `# ruff: noqa: RUF001` в `worker/reply.py`
  (RUF001 на этих кириллических символах не срабатывает), формат трёх файлов, сортировка импортов —
  через `ruff check --fix` + `ruff format`.

Проверка глубины тестов: `test_worker_reply.py` — все ветки сопоставления (finance/calendar/оба/
пусто); `test_agent_processor.py` — параметризовано: finance→finance, reminder→calendar,
нейтральный→строка, `agent_task`+finance→без кнопки; `test_worker.py` — `_deliver_reply` выбирает
транспорт с кнопкой vs обычный; `test_gateway.py` — `MenuButtonWebApp` с URL `{public_url}/app/` и
текстом «Открыть»; `test_webapp_app.py` — auth инкрементит `webapp_app_opened_total`; фронт —
приоритет `?screen=` над `start_param` и allowlist.

## Ограничения и следующий шаг

- Живой e2e (меню и обе inline-кнопки открывают нужный экран на Telegram Desktop и мобильном,
  светлая/тёмная тема, неизвестный `screen`→default) и прод-регистрация меню
  (`gateway set-menu-button` в раннере деплоя) — за activation gate (решение владельца 2026-07-17).
- Этап 9 (Mini App: платформа 9.1, календарь 9.2, финансы 9.3, интеграция 9.4) реализован
  локально целиком. Осталась только живая приёмка владельцем на проде и прод-развёртывание по
  runbook — activation gate закрывает чекбоксы 9.1–9.4 в progress.md разом.
