# plan/ — индекс

Плановые документы — источник истины: сначала правится план, потом код (регламент в [CLAUDE.md](../CLAUDE.md)). Мастер-план — [../implementation_plan_v1.md](../implementation_plan_v1.md), реестр инструментов — [../tools_registry_v1.md](../tools_registry_v1.md).

## Ядро

- [progress.md](progress.md) — статус выполнения; читать перед началом любой задачи, отмечать после приёмки.
- [requirements.md](requirements.md) — продуктовые требования и границы.
- [db-schema.md](db-schema.md) — схема БД (зафиксирована, 13 таблиц + правки через план).
- [system-prompt-spec.md](system-prompt-spec.md) — спецификация системного промпта (блоки A–F, C2 персоны).
- [pricing-draft.md](pricing-draft.md) — лимиты и тарифы (рабочие числа, финал перед 7.4).
- [plan-improvements.md](plan-improvements.md) — отложенные предложения; не реализовывать без явного решения.

## Этапы

`stage-0.md` … `stage-8.md` — детализация этапов мастер-плана (0–6 закрыты, 7–8 впереди). Этап 9 (Mini App):

- [stage-9.md](stage-9.md) — план этапа;
- [stage-9-architecture.md](stage-9-architecture.md) — архитектурные решения Mini App (auth, RLS, API, frontend, deploy);
- [stage-9-handoff.md](stage-9-handoff.md) — карта репозитория и батчей для исполнителя (снимок 2026-07-17; актуальный статус — в progress.md);
- [miniapp-calendar-spec.md](miniapp-calendar-spec.md) / [miniapp-finance-spec.md](miniapp-finance-spec.md) — спеки экранов.

## Внеплановые итерации (спеки; история — в progress.md)

- [miniapp-design-iteration-1.md](miniapp-design-iteration-1.md), [miniapp-design-iteration-2.md](miniapp-design-iteration-2.md) — правки UI Mini App по фидбеку владельца.
- [onboarding-script-iteration-1.md](onboarding-script-iteration-1.md) — welcome, отмена ручного одобрения, капельные подсказки.
- [assistant-persona-iteration-1.md](assistant-persona-iteration-1.md), [assistant-persona-iteration-2.md](assistant-persona-iteration-2.md) — персона ассистента (итерация 2 — следующая в очереди).
