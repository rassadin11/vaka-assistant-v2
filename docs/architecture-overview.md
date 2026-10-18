# Обзор архитектуры (как есть)

Снимок реального состояния системы. Обновлять при значимых архитектурных изменениях, а не при каждом деплое. Эксплуатация живой установки — [runbook-small.md](runbook-small.md).

Последняя ревизия: 2026-10-18 (живая установка — профиль `small`; прежний прод на 16 ГБ выведен из эксплуатации 2026-10-16).

## Что это

Multi-tenant Telegram-ассистент: один бот на всех пользователей, изоляция данных через PostgreSQL RLS (`SET LOCAL app.user_id` в каждой транзакции роли `app`). LLM — через OpenRouter; боевая модель задаётся env `OPENROUTER_MODEL` (на живой установке — `deepseek/deepseek-v4-flash`), дефолт в коде — `deepseek/deepseek-chat`. Статичное ядро промпта одно на все модели (`PROMPT_VERSION = "v2"`). Плюс Telegram Mini App с экранами «Календарь», «Финансы» и «Заметки».

Одна кодовая база, два профиля развёртывания (`DEPLOYMENT_PROFILE=prod|small`, [plan/small-profile.md](../plan/small-profile.md)): инфраструктура у них разная, код и тесты общие. Развёрнут сейчас только `small`.

## Поток данных бота

```
Telegram ← polling (gateway; в профиле prod — webhook через Caddy)
  → дедуп update_id → rate limit (token bucket 20/мин) → Redis Streams
     q:{interactive|background}:{0..15} (партиция по пользователю)
  → worker (пул asyncio): per-user lock → агентный цикл (LLM + инструменты)
     → ответ через TelegramSender (лимиты ~30/с глобально, ~1/с на чат)
     → диалог/стоимость в Postgres (messages, usage, tool_calls_log)
```

- **Надёжность очереди**: at-least-once, reclaim незаACKнутых через 210 с, после 3 доставок — DLQ `q:dlq`; порядок сообщений одного пользователя гарантирован (per-user lock + predecessor-guard). Interactive обрабатывается прежде background.
- **Устойчивость LLM** (`core/llm_resilient.py`): семафор одновременных запросов, retry с джиттером на 429/5xx, circuit breaker → fallback-модель. OpenRouter всегда с `data_collection: deny` и allowlist провайдеров.
- **Контекст** (`core/context_manager.py`, спека [plan/system-prompt-spec.md](../plan/system-prompt-spec.md)): блоки A–F (системный промпт, персона C2, память, суммаризация старой истории, хвост диалога); суммаризация — fire-and-forget после ответа.
- **Инструменты** (`tools/`, реестр [tools_registry_v1.md](../tools_registry_v1.md)): финансы, напоминания и регулярные agent-задачи (шедулер в воркере, тик 60 с), долговременная память с автоинжектом, тематические заметки, web_search/fetch_page (SearXNG + SSRF-фильтр), PDF (+OCR tesseract) и поиск по документам, персона ассистента, часовой пояс. Поиск по фактам и документам — полнотекстовый Postgres в `small`, векторный (эмбеддинги) в `prod` (`RETRIEVAL_BACKEND`). Модель получает весь набор инструментов сразу — профили реестра §4 не реализованы. `user_id` никогда не берётся из аргументов LLM; mutating-инструменты идемпотентны (`idem:{update_id}:{n}`).
- **Другие модальности**: голос — Groq whisper-large-v3, фото — OCR → текст в агентный цикл.
- **Лимиты и деградация** (`core/spend.py`, `core/limits.py`, числа — `core/profile.py`): дневной ₽-бюджет со ступенями деградации (без фоновых → lean-контекст → мягкий отказ), счётчики по осям тарифа, предупреждения на 80 %; всё в Redis с ключами по локальной дате пользователя, fail-open.

## Mini App

```
Telegram WebView → Caddy /app/* → webapp (FastAPI)
  → auth по initData → сессия 12 ч (itsdangerous cookie)
  → REST /app/api/* под RLS (резолв tg_user_id → users.id через SECURITY DEFINER)
  → Postgres (те же таблицы, что у бота) ; Redis rate limit; AI-резюме финансов
    через тот же ResilientLLMProvider с общим ₽-бюджетом пользователя
```

Frontend — `miniapp/` (Vite + Preact + TS), собирается в статику и раздаётся webapp. Вход: кнопка меню бота, inline-кнопки после финансовых/календарных ответов, deep-link `start_param`. Решения — [plan/archive/stages/stage-9-architecture.md](../plan/archive/stages/stage-9-architecture.md), спеки экранов — [plan/specs/](../plan/specs/).

## Сервисы живой установки (профиль `small`, 8 контейнеров)

| Группа | Контейнеры | Роль |
|--------|-----------|------|
| Приложение | gateway, worker, webapp | приём апдейтов (polling); агентный цикл + шедулер + outbox; Mini App API/SPA |
| Данные | postgres, redis | БД с RLS (без PgBouncer); один Redis `noeviction` — очередь, бюджеты, кэш с TTL |
| Вспомогательные | caddy, searxng, migrate | TLS и маршрут `/app/*` ради Mini App; мета-поиск; одноразовый прогон миграций Alembic |

Секреты — env-файлы с правами 600 на сервере (без Infisical). Наблюдаемость — `docker compose logs` и healthcheck'и (без Prometheus/Grafana). Бэкапы — `pg_dump` по cron, пока только локально. Логи всех сервисов — JSON в stdout с единым `trace_id` на задачу. Подробности и процедуры — [runbook-small.md](runbook-small.md).

**Профиль `prod`** (17 контейнеров: + pgbouncer, второй redis, infisical, embeddings, prometheus/grafana/blackbox/node-exporter/cadvisor; webhook, pgBackRest, деплой CI → GHCR → Ansible) остаётся в коде и compose-файлах, но не развёрнут. Его рунбук — в архиве: [archive/runbook-deploy.md](archive/runbook-deploy.md).

## Где искать детали

- Схема БД (состав таблиц, роли, RLS, служебные функции; число таблиц — только там) — [plan/db-schema.md](../plan/db-schema.md).
- Принятые решения — [plan/decisions.md](../plan/decisions.md); что дальше — [plan/roadmap.md](../plan/roadmap.md); статус — [plan/progress.md](../plan/progress.md).
- Инварианты кода и регламент — [CLAUDE.md](../CLAUDE.md).
- Dev-окружение (`make dev-up`, роли БД) — [infra/README.md](../infra/README.md); инциденты — [runbook.md](runbook.md).
