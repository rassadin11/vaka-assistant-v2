# docs/ — индекс

Эксплуатационная документация и журнал. Планы, спеки и решения — в [plan/](../plan/README.md); протокол работы — [CLAUDE.md](../CLAUDE.md).

## Живые документы

- [architecture-overview.md](architecture-overview.md) — обзор системы как она есть: сервисы, потоки данных, где что искать.
- [runbook-small.md](runbook-small.md) — эксплуатация живой установки (профиль `small`): развёртывание, обновление, Mini App, бэкапы, OOM-kill, ежедневные операции.
- [runbook.md](runbook.md) — инциденты: очередь, LLM, сбор контекста по `trace_id`. Написан под профиль `prod`; что применимо к `small` — в шапке документа.
- [dev-log.md](dev-log.md) — журнал разработки понятным языком, запись после каждого принятого пункта.

## Архив

Снимки на дату — не редактировать, кроме починки ссылок.

- [archive/runbook-deploy.md](archive/runbook-deploy.md) — рунбук контура `prod` на 16 ГБ, выведенного из эксплуатации 2026-10-16.
- [archive/bot-analysis-2026-07-17.md](archive/bot-analysis-2026-07-17.md) — разбор бота по бета-фидбеку владельца; лёг в основу доработок 2026-07-17.
- [archive/stage-9-preparation-2026-07-17.md](archive/stage-9-preparation-2026-07-17.md) — журнал архитектурной подготовки этапа 9 (Mini App).
- Протоколы приёмки батчей этапа 9 — что проверено, какими командами, что найдено и исправлено: [9.1a](archive/stage-9-1a-acceptance-2026-07-17.md), [9.1b](archive/stage-9-1b-acceptance-2026-07-17.md), [9.2](archive/stage-9-2-acceptance-2026-07-17.md), [9.3d](archive/stage-9-3d-acceptance-2026-07-17.md), [9.3e](archive/stage-9-3e-acceptance-2026-07-17.md), [9.4](archive/stage-9-4-acceptance-2026-07-18.md).
