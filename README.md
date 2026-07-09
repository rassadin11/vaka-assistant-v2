# Telegram-сервис персональных ассистентов

Multi-tenant сервис: один бот, общий пул asyncio-воркеров, изоляция пользователей через PostgreSQL RLS. Стек: Python 3.12, FastAPI, aiogram 3.x, PostgreSQL 16 + pgvector + PgBouncer, Redis ×2, Alembic, Caddy; LLM — DeepSeek через OpenRouter.

## Документация

- [implementation_plan_v1.md](implementation_plan_v1.md) — мастер-план и архитектурные решения.
- [plan/](plan/) — детализация этапов, схема БД, требования, прогресс.
- [tools_registry_v1.md](tools_registry_v1.md) — спецификация инструментов.
- [CLAUDE.md](CLAUDE.md) — протокол работы для агентов-исполнителей.

## Разработка

Зависимости — через [uv](https://docs.astral.sh/uv/):

```bash
uv sync --dev          # окружение
uv run pytest          # тесты
uv run ruff check .    # линт
uv run mypy            # типы
make gitleaks          # скан секретов (docker)
```

Структура: `gateway/` (приём апдейтов), `worker/` (агентный цикл), `core/` (контракты), `tools/` (инструменты LLM), `migrations/` (Alembic), `infra/` (compose, деплой).

Секреты — только из Infisical; `.env` в репозитории запрещён.
