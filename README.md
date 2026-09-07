# Telegram-сервис персональных ассистентов

Multi-tenant сервис: один бот, общий пул asyncio-воркеров, изоляция пользователей через PostgreSQL RLS. Стек: Python 3.12, FastAPI, aiogram 3.x, PostgreSQL 16 + pgvector + PgBouncer, Redis ×2, Alembic, Caddy; LLM — DeepSeek через OpenRouter. Плюс Telegram Mini App (календарь + финансы): backend `webapp/` (FastAPI), frontend `miniapp/` (Vite + Preact + TS).

**Прод**: `vaka-assistant.ru`, бета через @vAssistantv2testbot. Эксплуатация и деплой — [docs/runbook-deploy.md](docs/runbook-deploy.md).

## Документация

- [docs/architecture-overview.md](docs/architecture-overview.md) — обзор системы как она есть (сервисы, потоки данных).
- [implementation_plan_v1.md](implementation_plan_v1.md) — мастер-план и архитектурные решения.
- [plan/](plan/) — детализация этапов, схема БД, требования, прогресс ([индекс](plan/README.md)).
- [tools_registry_v1.md](tools_registry_v1.md) — спецификация инструментов.
- [docs/](docs/) — runbook'и, журнал разработки, приёмочные отчёты ([индекс](docs/README.md)).
- [CLAUDE.md](CLAUDE.md) — протокол работы для агентов-исполнителей; [AGENTS.md](AGENTS.md) — то же для внешних CLI-агентов (Codex).

## Разработка

Зависимости — через [uv](https://docs.astral.sh/uv/):

```bash
uv sync --dev          # окружение
uv run pytest          # тесты
uv run ruff check .    # линт
uv run mypy            # типы
make gitleaks          # скан секретов (docker)
```

Структура: `gateway/` (приём апдейтов), `worker/` (агентный цикл, шедулер), `core/` (контракты, LLM, контекст), `tools/` (инструменты LLM), `webapp/` (backend Mini App), `miniapp/` (frontend Mini App), `embeddings/` (сервис эмбеддингов), `migrations/` (Alembic), `infra/` (compose, Ansible, мониторинг), `tests/`, `evals/` (живые прогоны промпта, вне CI), `docs/`, `plan/`.

Mini App локально: `cd miniapp && npm ci`, затем `npm run build` / `npm test` / `npm run lint` / `npm run typecheck`; backend (`webapp/`) поднимается вместе с dev-стеком (`make dev-up`).

Секреты — только из Infisical; `.env` в репозитории запрещён.
