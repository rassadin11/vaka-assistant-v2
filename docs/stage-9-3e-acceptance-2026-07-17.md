# Приёмка этапа 9.3, батч E — финансовое AI-резюме Mini App

Дата: 2026-07-17
Статус: локальная реализация принята
Исполнитель кода: Codex (gpt-5.5) — с двумя обрывами и до-делкой; спека, правка плана,
исправление дефектов и приёмка — Claude (opus)
Прод-развёртывание: не выполнялось (activation gate)

## Объём принятой части

Батч E завершает экран «Финансы» коротким AI-резюме по агрегатам:

- `core/finance_summary.py` — provider-neutral сервис: сборка компактного JSON-агрегата
  (без сырых транзакций, только топ-5 крупнейших), один прямой вызов LLM, ветки
  empty / budget_exhausted / cache_hit / generated / unavailable, generation-кэш и
  stampede-lock;
- `GET /app/api/finance/ai-summary` — агрегаты берутся под RLS, транзакция закрывается ДО
  вызова LLM; учёт usage (queue=background) + инкремент spend; метрика
  `webapp_ai_summary_total{outcome}`;
- инвалидация кэша: `INCR fin:gen` в `add_transaction` (доменная обёртка) и в DELETE-эндпоинте;
- фронт: карточка AI-резюме (skeleton → ready/empty/budget_exhausted; unavailable — скрыта),
  грузится независимо от цифр, 401→re-auth→retry один раз, перезагрузка при смене периода и
  после удаления;
- конфиг: webapp получает `REDIS_QUEUE_URL` в dev/prod compose и Ansible-шаблоне.

## Ключевое архитектурное решение (правка плана перед кодом)

Дневной ₽-бюджет (`add_spend`/`spend_rub:*`, инвариант 5.2) и глобальные контролы
`ResilientLLMProvider` (семафор `sem:openrouter`, breaker `cb:openrouter:*`, инвариант 3.5) у
воркера живут на **redis-queue**. Архитектура выдавала webapp только redis-cache. Если бы
AI-резюме считало spend и брало семафор на другом инстансе, дневной лимит и лимит
конкурентности OpenRouter раздвоились бы между ботом и Mini App. По регламенту («в плане нет
ответа — сначала правка плана») выправлены `stage-9-architecture.md` (§«Секреты и
конфигурация», §«Финансовый кэш и AI-резюме»), `miniapp-finance-spec.md` (§«Учёт и лимиты») и
`stage-9-handoff.md`: webapp получает `REDIS_QUEUE_URL` только для AI-резюме, cache-redis
остаётся для кэша. Реализация повторяет разводку воркера (`_active_inner_processor`); поведение
агента не меняется. Разделение подтверждено в коде:

- **redis-queue**: `get_spent_rub`/`add_spend`, `ResilientLLMProvider(OpenRouterProvider,
  queue_redis, model, …)`;
- **redis-cache**: `fin:gen`, `fin_summary:*`, `fin:summary-lock:*`.

## Изменённые/новые файлы

- `core/finance_summary.py` (новый) — сервис AI-резюме и кэш.
- `core/finance_service.py` — добавлен `fetch_top_transactions` (+ `TopExpenseTransaction`).
- `webapp/routers/finance.py` — эндпоинт ai-summary; DELETE теперь инвалидирует `fin:gen`.
- `webapp/app.py` — queue-redis в lifespan, `_finance_provider_from_env` (ResilientLLMProvider
  как у воркера), новые getter'ы `summary_cache`/`queue`/`provider`.
- `webapp/settings.py` — `redis_queue_url` из `REDIS_QUEUE_URL`.
- `webapp/metrics.py` — счётчик `webapp_ai_summary_total{outcome}`.
- `tools/finance.py`, `tools/registry.py`, `worker/__main__.py` — проброс cache-redis в
  `register_finance_tools`; инвалидация `fin:gen` после успешного `add_transaction`.
- `miniapp/src/api.ts`, `finance.tsx` — клиент `fetchFinanceAiSummary`, компонент
  `AiSummaryCard`, стейт/загрузка резюме.
- `miniapp/src/styles.css` — стиль карточки/скелетона.
- `tests/test_finance_summary.py`, `tests/test_webapp_ai_summary.py` (новые),
  `miniapp/src/finance.test.tsx`, `api.test.ts` — юнит/API/фронт-тесты всех веток.
- `infra/docker-compose.dev.yml`, `infra/compose.prod.yml`,
  `infra/ansible/roles/app/templates/webapp.env.j2` — `REDIS_QUEUE_URL` для webapp.

## Проверенные инварианты (CLAUDE.md)

- RLS: агрегаты и usage — только через `active_request_user`/`user_transaction` (роль `app`);
  транзакция не удерживается во время вызова LLM.
- LLM только через `LLMProvider`; переиспользованы `ResilientLLMProvider`,
  `UsageRecordingProvider`, `save_usage`, `add_spend`, `budget_state` — не реализованы заново.
- В LLM уходят только агрегаты + топ-5 трат, не сырой список.
- Деньги строкой; время UTC в БД / локальный offset в API; API не пишет в `tool_calls_log`.
- Новых таблиц/колонок и миграций нет.
- Fail-open на Redis (кэш/generation/spend/lock — сбой Redis не роняет запрос).
- Код английский, тексты русские; тесты офлайн (MockLLM + fake-redis).

## Автоматические проверки (прогнаны Claude независимо)

- `uv run ruff check .` — PASS; `uv run ruff format --check .` — PASS (161 файл).
- `uv run mypy` — PASS (80 source files).
- `uv run pytest -q` — PASS: `330 passed, 33 skipped` (+6 к батчу D — новые backend-тесты).
- miniapp: `npm run lint`/`typecheck` — PASS; `npm test` — `26 passed` (+5 ai-summary);
  `npm run build` — PASS.
- `docker compose config` dev+prod — YAML валиден (webapp: REDIS_QUEUE_URL+REDIS_CACHE_URL,
  depends_on = infisical/pgbouncer/redis-cache/redis-queue).
- Секрет-скан батч-файлов — совпадений нет.

## Дефекты Codex, исправленные на приёмке

Codex дважды обрывался на этом батче: первый запуск — исчерпание лимита GPT (пополнено
владельцем); второй — краш (exit 127) в середине, оставивший «брошенный хвост». Исправлено:

- **мелочи (силами Claude, разрешено регламентом):** дубль `aclose` в протоколе `ClosableQueueRedis`
  и потерянный `aclose` у `ClosableRedis` (`webapp/app.py`); аннотация `dict[str, str]` в
  `finance_summary.py`; `# ruff: noqa: RUF001` для русского промпта; вынос длинной строки в
  хелпер `_budget_status_label`; сортировка импортов;
- **сломанный YAML в обоих compose** (`environment` под `env_file`, `redis-*` вложены в
  `pgbouncer` в `depends_on`) — восстановлена структура, `docker compose config` зелёный;
- **брошенный хвост до-делан отдельным узким промптом Codex:** фронт-компонент `AiSummaryCard`
  + стейт/загрузка (упоминались в рендере, но не были определены — ломали все финансовые
  фронт-тесты) и два backend-тест-файла.

Проверка глубины до-деланных тестов: `test_finance_summary.py` покрывает все ветки
оркестратора (empty/budget без вызова провайдера; cold→generated с проверкой temperature/
max_tokens/usage/spend/cache; warm→cache_hit одним вызовом; provider-error→negative cache;
stampede-lock→провайдер не зовётся; generation-bump→промах старого ключа).
`test_webapp_ai_summary.py` — статусы эндпоинта, 401/429, DELETE→bump generation.

## Ограничения и следующий шаг

- Живой e2e (реальный Telegram WebView, генерация резюве на живых данных, проверка usage/spend в
  БД/Redis, кэш-хит при повторном открытии) и прод-развёртывание — за activation gate (решение
  владельца 2026-07-17). Интеграционные тесты финансов проверяются при доступной dev-БД/Redis.
- Экран «Финансы» (9.3) реализован локально целиком (данные + AI-резюме). Следующий пакет —
  **батч F (9.4)**: кнопка меню бота, inline-кнопки «Открыть финансы/календарь», deep-link
  `start_param`, тексты welcome/help, продуктовые метрики.
