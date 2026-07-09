# Этап 1. Инфраструктура

Часть мастер-плана: [../implementation_plan_v1.md](../implementation_plan_v1.md) — там архитектурные решения, сводный график и регламент передачи задач.

Разделён на два трека. 1A — локальная dev-инфраструктура (2–3 дня, блокирует этап 2). 1B — боевой сервер (2–3 дня, нужен к закрытой бете; ведётся параллельно этапам 3–4, покупка сервера — к концу этапа 3).

## Трек 1A. Локальная инфраструктура (dev)

1.1. Dev-окружение (docker-compose из 0.4, уточнение состава): PostgreSQL 16 + pgvector + PgBouncer, redis-queue, redis-cache, Infisical (со своими Postgres и Redis — не общими, чтобы его миграции не трогали наш кластер). Итог: `make dev-up` поднимает всё с нуля.
1.2. PgBouncer transaction mode × asyncpg: PgBouncer ≥ 1.21 с `max_prepared_statements` (protocol-level prepared statements); smoke-тест prepared statements через пул — в CI. Без этого на этапе 3 будут плавающие ошибки.
1.3. Роли БД: `migrator` (владелец, только Alembic), `app` (рабочая, RLS активна), `service` (BYPASSRLS — для шедулера, outbox, OAuth-refresh, админ-команд). Хелпер в core/: открытие транзакции с `SET LOCAL app.user_id`. Тест: под ролью app без установленного user_id пользовательские таблицы возвращают 0 строк.
1.4. Redis: два инстанса. redis-queue: noeviction, AOF everysec, maxmemory с запасом. redis-cache: allkeys-lru, без персистентности.
1.5. Infisical: machine identity (universal auth) per сервис (gateway, worker, scheduler). Bootstrap-проблема: сам Infisical и доступ к нему — из `/etc/assistant/bootstrap.env` (права 600, вне репозитория); всё остальное — только из vault. gitleaks в CI; .env запрещён в репозитории.
1.6. Alembic: миграция v1 строго по зафиксированной схеме [db-schema.md](db-schema.md) — 13 таблиц: users, messages, dialog_summaries, memory_facts, transactions, budgets, scheduled_tasks, documents, doc_chunks, oauth_tokens, outbox_actions, tool_calls_log и usage (обе — партиционирование по месяцу, pg_partman). Vector-колонки `vector(1024)` и HNSW-индексы в memory_facts/doc_chunks создаются сразу (эмбеддинги решены: локально, оба кандидата — 1024 измерения). RLS-политики по шаблону из схемы; CI-тест: каждая таблица с колонкой user_id имеет RLS-политику (защита от забытых таблиц в будущих миграциях).
1.7. Хранение per-user секретов: envelope-шифрование oauth_tokens на приложении, ключ — в vault (решение принято; vault для per-user данных не используется).
1.8. Бэкапы: pgBackRest (ретеншн 7 daily + 4 weekly, шифрование репозитория, verify). Дополнительно: RDB-снапшоты redis-queue (раз в час) и дамп БД Infisical — потеря vault = потеря всех интеграций. Скрипт `restore-to-dev.sh`: восстановление бэкапа в локальный контейнер.

[DoD 1A] `make dev-up` с нуля до рабочего состояния; миграция v1 применяется ролью migrator; RLS-тест и PgBouncer smoke-тест зелёные; restore-to-dev.sh восстанавливает бэкап.

## Трек 1B. Боевой сервер (к началу закрытой беты)

1.9. Сервер: ориентир 4–6 vCPU / 16 GB / NVMe 100+ GB (на машине живут Postgres, 2×Redis, Infisical, SearXNG, gateway, воркеры, Caddy и сервис эмбеддингов ~2 GB; запас под локальный STT, если обсуждение №6 решится в его пользу). Ubuntu 24.04 LTS. Hardening: ssh только по ключам, PasswordAuthentication no, ufw (allow 22/80/443), fail2ban на sshd, unattended-upgrades (security), sysctl: net.core.somaxconn, vm.overcommit_memory=1. Отдельный пользователь deploy без sudo для приложений. [Обсудить: выбор хостера]
1.10. Ansible: роли base (hardening) / docker / app (compose-файлы, каталоги). Критерий идемпотентности: повторный прогон — 0 changed. Роли переиспользуются при переезде на k3s (этап 8).
1.11. Caddy: TLS, маршруты /webhook/{secret_path} → gateway (секретный путь поверх secret_token), /oauth/callback → gateway, /tribute/webhook → gateway (задел на этап 7). Security headers, JSON access-логи (для trace_id на этапе 6), лимит размера тела запроса.
1.12. Доставка на сервер: образы в GHCR; CI: build + push по тегу; деплой: `alembic upgrade head` (ролью migrator, шаг CI) → `docker compose pull && up -d` по ssh с healthcheck-гейтом (/healthz). Миграции обратно совместимы с предыдущей версией кода (expand-contract): воркеры перезапускаются не атомарно, старый код должен работать поверх новой схемы. Без watchtower — деплой осознанный. compose.prod.yml в репозитории (infra/), секреты — через Infisical agent/entrypoint.
1.13. Бэкапы на проде: S3-совместимое хранилище у другого провайдера, чем сервер (иначе это не бэкап); ежедневный full + WAL. [Обсудить: выбор провайдера хранилища]

[DoD 1B] Провижининг с нуля одной командой; повторный прогон — 0 changed; образ из CI доезжает до сервера, миграции применяются, сервис поднимается через healthcheck-гейт; бэкап на проде создаётся и восстанавливается.
