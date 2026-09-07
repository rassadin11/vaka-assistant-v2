# Обзор архитектуры (как есть)

Снимок реального состояния системы. Обновлять при значимых архитектурных изменениях, а не при каждом деплое. Детали — по ссылкам; карта прод-сервера и процедуры — [runbook-deploy.md](runbook-deploy.md).

Последняя ревизия: 2026-07-18 (этап 9 задеплоен: Mini App на проде).

## Что это

Multi-tenant Telegram-ассистент: один бот на всех пользователей, изоляция данных через PostgreSQL RLS (`SET LOCAL app.user_id` в каждой транзакции роли `app`). LLM — DeepSeek V4 Flash через OpenRouter (промпт v2-flash; дефолт в коде пока `deepseek-chat`, боевая модель задана конфигом в Infisical). Плюс Telegram Mini App с экранами «Календарь» и «Финансы».

## Поток данных бота

```
Telegram → Caddy (443, TLS) → gateway (FastAPI, webhook)
  → дедуп update_id → rate limit (token bucket 20/мин) → Redis Streams
     q:{interactive|background}:{0..15} (партиция по пользователю)
  → worker (пул asyncio): per-user lock → агентный цикл (LLM + инструменты)
     → ответ через TelegramSender (лимиты ~30/с глобально, ~1/с на чат)
     → диалог/стоимость в Postgres (messages, usage, tool_calls_log)
```

- **Надёжность очереди**: at-least-once, reclaim незаACKнутых через 210 с, после 3 доставок — DLQ `q:dlq` с алертом; порядок сообщений одного пользователя гарантирован (per-user lock + predecessor-guard). Interactive обрабатывается прежде background.
- **Устойчивость LLM** (`core/llm_resilient.py`): семафор 8 одновременных запросов, retry ×3 с джиттером на 429/5xx, circuit breaker → fallback-модель на 300 с + алерт. OpenRouter всегда с `data_collection: deny` и allowlist провайдеров.
- **Контекст** (`core/context_manager.py`, спека [plan/system-prompt-spec.md](../plan/system-prompt-spec.md)): блоки A–F (системный промпт, персона C2, память, суммаризация старой истории, хвост диалога); суммаризация — fire-and-forget после ответа.
- **Инструменты** (`tools/`, реестр [tools_registry_v1.md](../tools_registry_v1.md)): финансы, напоминания (+шедулер в воркере, тик 60 с), долговременная память (pgvector, автоинжект ≥0.80), web_search/fetch_page (SearXNG + SSRF-фильтр), PDF (+OCR tesseract) и поиск по документам, отложенные agent_task, персона ассистента. `user_id` никогда не берётся из аргументов LLM; mutating-инструменты идемпотентны (`idem:{update_id}:{n}`).
- **Другие модальности**: голос — Groq whisper-large-v3 (лимит 10 мин/день), фото — OCR → текст в агентный цикл.
- **Лимиты и деградация** (`core/spend.py`, `core/limits.py`): дневной ₽-бюджет по тарифу со ступенями 100/120/150% (без фоновых → lean-контекст → мягкий отказ), счётчик сообщений/день, предупреждения на 80% по всем осям; всё в Redis с ключами по локальной дате пользователя, fail-open.

## Mini App

```
Telegram WebView → Caddy /app/* → webapp (FastAPI)
  → auth по initData → сессия 12 ч (itsdangerous cookie)
  → REST /app/api/* под RLS (резолв tg_user_id → users.id через SECURITY DEFINER)
  → Postgres (те же таблицы, что у бота) ; Redis rate limit; AI-резюме финансов
    через тот же ResilientLLMProvider с общим ₽-бюджетом пользователя
```

Frontend — `miniapp/` (Vite + Preact + TS), собирается в статику и раздаётся webapp. Вход: кнопка меню бота, inline-кнопки после финансовых/календарных ответов, deep-link `start_param`. Решения — [plan/stage-9-architecture.md](../plan/stage-9-architecture.md).

## Сервисы прод-стека (17 контейнеров)

| Группа | Контейнеры | Роль |
|--------|-----------|------|
| Приложение | gateway, worker, webapp | приём апдейтов; агентный цикл + шедулер + outbox; Mini App API/SPA |
| Данные | postgres (+pgvector), pgbouncer, redis-queue, redis-cache | БД с RLS; пул (transaction mode); очередь+бюджеты; кэш/лимиты/семафоры |
| Секреты | infisical + его postgres и redis | секреты приложения; контейнеры читают через `core/secrets_entrypoint` при старте |
| Вспомогательные | caddy, searxng, embeddings | TLS/маршрутизация (`/webhook`, `/app/*`, `/grafana`); мета-поиск; эмбеддинги multilingual-e5-large |
| Наблюдаемость | prometheus, grafana, blackbox, node-exporter, cadvisor | метрики; дашборды «Технический»/«Продуктовый» + Unified Alerting → Telegram владельца; пробы; метрики хоста и контейнеров |

Логи всех сервисов — JSON в stdout с единым `trace_id` на задачу (сбор контекста — [runbook.md](runbook.md) §6-7). Бэкапы — pgBackRest (7 дней + 4 недели, WAL) + RDB redis-queue + дамп Infisical; ежемесячная репетиция восстановления.

## Где искать детали

- Схема БД (13 таблиц + assistant_profile и webapp-правки) — [plan/db-schema.md](../plan/db-schema.md).
- Архитектурные решения и история — [implementation_plan_v1.md](../implementation_plan_v1.md), статус — [plan/progress.md](../plan/progress.md).
- Инварианты кода и регламент — [CLAUDE.md](../CLAUDE.md).
- Dev-окружение (`make dev-up`, роли БД, бэкапы) — [infra/README.md](../infra/README.md); прод — [runbook-deploy.md](runbook-deploy.md).
