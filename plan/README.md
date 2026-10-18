# plan/ — индекс

Плановые документы — источник истины: сначала правится план, потом код (регламент — [CLAUDE.md](../CLAUDE.md)). Документы устроены по четырём вопросам (решение D-38). Индекс содержит только указатели — числа и статусы живут в самих документах.

## Куда идём, что решено, что сделано

- [roadmap.md](roadmap.md) — очередь работ, вехи, открытые обсуждения и всё, что ждёт решения владельца.
- [decisions.md](decisions.md) — все принятые решения с датами и причинами.
- [progress.md](progress.md) — статус: что сделано и что нет, по строке на пункт.

## Как устроено — справочники

- [requirements.md](requirements.md) — продуктовые требования и границы.
- [db-schema.md](db-schema.md) — схема БД: таблицы, роли, RLS, служебные функции.
- [../tools_registry_v1.md](../tools_registry_v1.md) — реестр инструментов LLM: контракт, уровни риска, каждый инструмент.
- [system-prompt-spec.md](system-prompt-spec.md) — спецификация системного промпта (блоки A–F, персона C2) и eval-набора.
- [small-profile.md](small-profile.md) — профиль развёртывания `small`: состав стека, лимиты, задачи S1–S20.
- [pricing-draft.md](pricing-draft.md) — тарифы и юнит-экономика (черновик до 7.4).
- [plan-improvements.md](plan-improvements.md) — отложенные и отклонённые предложения; не реализовывать без явного решения.

## Спеки

Живые спеки фич: по ним вносят изменения и по ним пишется код. Структура новой спеки — по образцу [specs/shared-objects-spec.md](specs/shared-objects-spec.md): назначение и объём → принятые решения → данные → контракты → лимиты → промпт и eval → подзадачи и DoD → что сознательно не делаем.

- [specs/shared-objects-spec.md](specs/shared-objects-spec.md) — общие объекты, часть 1: друзья и общие напоминания.
- [specs/recurring-agent-tasks-spec.md](specs/recurring-agent-tasks-spec.md) — регулярные агентные задачи словами и разовый запуск.
- [specs/miniapp-calendar-spec.md](specs/miniapp-calendar-spec.md) — экран «Календарь» Mini App.
- [specs/miniapp-finance-spec.md](specs/miniapp-finance-spec.md) — экран «Финансы» Mini App.
- [specs/miniapp-notes-spec.md](specs/miniapp-notes-spec.md) — экран «Заметки» Mini App.

## Этапы впереди

- [stage-7.md](stage-7.md) — онбординг и биллинг.
- [stage-8.md](stage-8.md) — подготовка к росту.

## Архив

Снимки на дату — не редактировать, кроме починки ссылок. Открытые пункты из архивных документов перенесены в [roadmap.md](roadmap.md).

- [archive/stages/](archive/stages/) — детализация закрытых этапов 0–6 и 9 (включая архитектуру и handoff этапа 9).
- [archive/iterations/](archive/iterations/) — спеки внеплановых итераций: персона, онбординг-скрипт, дизайн Mini App.
- [archive/journal/](archive/journal/) — полный журнал прогресса до 2026-10-18, с подробностями приёмок и передачами сессий.
- [archive/implementation_plan_v1.md](archive/implementation_plan_v1.md) — мастер-план целиком на 2026-10-18 (сводный график этапов, исходные формулировки решений).
