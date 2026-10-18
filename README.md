# Telegram-сервис персональных ассистентов

Multi-tenant сервис: один бот, общий пул asyncio-воркеров, изоляция пользователей через PostgreSQL RLS. Стек: Python 3.12, FastAPI, aiogram 3.x, PostgreSQL 16 (+pgvector), Redis, Alembic, Caddy; LLM — через OpenRouter. Плюс Telegram Mini App (календарь, финансы, заметки): backend `webapp/` (FastAPI), frontend `miniapp/` (Vite + Preact + TS).

**Живая установка**: закрытая бета через @vAssistantv2testbot на сервере профиля `small` (2 ГБ, polling), Mini App — `https://vaka-assistant.ru/app/`. Эксплуатация и деплой — [docs/runbook-small.md](docs/runbook-small.md). Прежний прод на 16 ГБ выведен из эксплуатации 2026-10-16; профиль `prod` остаётся в коде.

## Документация

- [CLAUDE.md](CLAUDE.md) — протокол работы: что читать перед задачей, регламент, инварианты кода.
- [plan/](plan/README.md) — куда идём ([roadmap](plan/roadmap.md)), что решено ([decisions](plan/decisions.md)), что сделано ([progress](plan/progress.md)), справочники и спеки фич.
- [tools_registry_v1.md](tools_registry_v1.md) — реестр инструментов LLM.
- [docs/](docs/README.md) — обзор архитектуры, рунбуки, журнал разработки.

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
