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

Крупные пункты 1.9–1.13 ниже декомпозированы в подзадачи с оценками (2026-07-15). Оценки — трудозатраты исполнителя (Claude+Codex) при уже доступном сервере и домене; **календарное ожидание владельца** (хостер, домен, S3) считается отдельно и в трудозатраты не входит. Ревизия: суммарно ~4.5–6 дней работы — больше исходной грубой оценки «2–3 дня» в мастер-плане (она предшествовала декомпозиции); строку графика мастер-плана обновить при следующей ревизии.

Особенность приёмки всего 1B: инфраструктурный код (Ansible, compose.prod, Caddy, deploy-workflow) **нельзя полностью проверить без реального VPS** — приёмка частично ручная на сервере. До аренды сервера задачи пишутся «вслепую» по спеке, финальная приёмка — после провижининга.

### Блокеры за владельцем (календарные, на критическом пути)
- **Б1. Выбор хостера + юрисдикция (обсуждение №3, 152-ФЗ) — РЕШЕНО (2026-07-15): всё за рубежом (EU/Финляндия) на бету как осознанный риск.** Релей не нужен (не-РФ аккаунт OpenRouter + не-РФ IP сервера). 152-ФЗ localization (primary Postgres в РФ) — долг миграции к публичному запуску; юр-заключение до паблика. Детали и режим трансграничной передачи — обсуждение №3 мастер-плана. Осталось: выбрать конкретного хостера/регион и способ оплаты.
- **Б2. Домен + опубликованная privacy policy (0.6/№10).** Блокирует TLS и webhook-режим (1.11). Домен нужен для Let's Encrypt и для `set-webhook`.
- **Б3. Выбор S3-провайдера для бэкапов** (у другого провайдера, чем сервер). Блокирует 1.13.
- **Б4. Перевыпуск засветившихся ключей** (боевой токен бота, OpenRouter-ключ) — до go-live, обязательно (см. блокеры progress.md).

### 1.9. Сервер + hardening
- **1.9.1** [владелец] Аренда сервера по Б1: 4–6 vCPU / 16 GB / NVMe 100+ GB, Ubuntu 24.04 LTS. **Выбрано и выдано (2026-07-15): xorek.cloud FI-R9-16 — 8 vCPU / 16 ГБ / 240 ГБ / 1 IPv4, Финляндия, ~1399 ₽/мес.** IP 31.76.15.130, Ubuntu 24.04.4 LTS, x86_64; SSH по ключу работает. Провалидировано вживую: с финского IP OpenRouter=200 / Groq=401 (не заблокировано → релей не нужен). *1.9.1 (аренда) закрыт; hardening — 1.9.2/1.9.3 в роли base (1.10.2).*
- **1.9.2** Базовый доступ: пользователь `deploy` без sudo для приложений, ssh только по ключам, `PasswordAuthentication no`. Кодифицируется в Ansible-роли base (1.10.2) — вручную только начальный bootstrap-ключ. ≈ 0.25 дня (в составе 1.10).
- **1.9.3** Hardening: ufw (allow 22/80/443), fail2ban на sshd, unattended-upgrades (security), sysctl (`net.core.somaxconn`, `vm.overcommit_memory=1`). Тоже в роли base. ≈ в составе 1.10.
- Итого 1.9 как отдельная работа сверх Ansible ≈ 0.25 дня (провижининг «сырого» сервера до готовности к Ansible).

### 1.10. Ansible (провижининг как код)  ≈ 1–1.5 дня (Codex по спеке + приёмка Claude на VPS)
- **1.10.1** Скелет: inventory, group_vars, секреты (ansible-vault или подтяг из Infisical), сухой прогон (`--check`) в CI-lint. ≈ 0.25 дня.
- **1.10.2** Роль `base`: hardening из 1.9.2/1.9.3 + пользователь deploy. ≈ 0.25–0.5 дня.
- **1.10.3** Роль `docker`: установка Docker Engine + compose-plugin, автозапуск демона. ≈ 0.25 дня.
- **1.10.4** Роль `app`: каталоги, выкладка `compose.prod.yml` (1.11.1), unit/автозапуск, entrypoint секретов Infisical. ≈ 0.25–0.5 дня.
- **1.10.5** Идемпотентность: повторный прогон = 0 changed; зафиксировать как критерий приёмки. Роли переиспользуемы при переезде на k3s (этап 8).

#### Детализация 1.10.1 — скелет Ansible (2026-07-15, перед кодом)

**Объём:** только каркас + CI-тест-контур. Логика ролей (base/docker/app) — в 1.10.2–1.10.4, здесь роли-заглушки.

**Решения (владелец, 2026-07-15):**
- **A. Тестирование — Molecule (docker-драйвер, Ubuntu 24.04)**, не голый `--check`. Роли прогоняются в CI на эфемерном контейнере без VPS; `molecule test` включает converge + **idempotence** (закрывает критерий 1.10.5 автоматически). На реальном VPS остаётся только сетевой e2e.
- **B. Секреты — переиспользуем Infisical (1.5).** Ansible НЕ хранит секреты приложения. Он кладёт `/etc/assistant/bootstrap.env` (0600, владелец root) с machine-identity Infisical; контейнеры тянут остальное из vault на старте (инвариант). `ansible-vault` шифрует только bootstrap-значения в `group_vars/prod/vault.yml`; пароль vault — вне репозитория (CI-секрет / локально).

**Структура `infra/ansible/`:**
- `ansible.cfg` — inventory=./inventory, roles_path=./roles, retry-файлы off, interpreter auto_silent, host_key_checking управляемый.
- `requirements.yml` — коллекции `community.general`, `ansible.posix`, `community.docker` (версии зафиксированы).
- `inventory/prod.yml` — группа `prod`, один хост; `ansible_host` через переменную (IP-плейсхолдер до выдачи 1.9.1), `ansible_user=deploy`, `ansible_port=22`.
- `group_vars/all.yml` — несекретное: `app_dir=/opt/assistant`, `deploy_user=deploy`, `timezone`, версии docker/compose-plugin, релизный тег.
- `group_vars/prod/vault.yml` — `ansible-vault`, только bootstrap (Infisical machine-identity client_id/secret, адрес Infisical). В репозитории — зашифрованным.
- `site.yml` — порядок ролей `base → docker → app` (в 1.10.1 роли-заглушки, no-op tasks).
- `roles/{base,docker,app}/{tasks,handlers,meta}/main.yml` — заглушки (task-`debug`), чтобы `site.yml` собирался; наполнение в 1.10.2–1.10.4.
- `molecule/default/` — `molecule.yml` (driver docker, image с systemd — `geerlingguy/docker-ubuntu2404-ansible:latest`, privileged/cgroup для systemd), `converge.yml` (прогоняет `site.yml`), `verify.yml` (заглушка ansible-verifier).
- `README.md` — запуск (`ansible-galaxy install -r requirements.yml`, `molecule test`, `ansible-playbook -i inventory/prod.yml site.yml --ask-vault-pass`), где лежит vault-пароль.

**CI (`.github/workflows`):** отдельный job `ansible` (триггер на изменения `infra/ansible/**`): установка `ansible-core`, `ansible-lint`, `yamllint`, `molecule`, `molecule-plugins[docker]`; шаги — `yamllint .` → `ansible-lint` → `ansible-playbook --syntax-check` → `molecule test` (create→converge→idempotence→verify→destroy). Docker на linux-раннере доступен.

**DoD 1.10.1:** структура на месте; `yamllint`/`ansible-lint` чистые; `--syntax-check` проходит; `molecule test` зелёный (converge + **idempotence 0 changed**) на ubuntu:24.04 в CI; секретов в репозитории нет (gitleaks; vault-файл зашифрован, пароль вне репо). Приёмка на реальном VPS — отложена до выдачи сервера (1.9.1) и наполнения ролей (1.10.2+). **Принято 2026-07-15 (коммит b5ffb03): оба CI-workflow зелёные, molecule прошёл.**

#### Детализация 1.10.2 — роль `base` (hardening + deploy) (2026-07-15, перед кодом)

**Объём:** наполнить `roles/base` реальными задачами (в 1.10.1 — заглушка). Роли `docker`/`app` остаются заглушками (их черёд — 1.10.3/1.10.4).

**Решения (разрешают неоднозначность 1.9.2):**
- **Пользователь `deploy` с passwordless sudo.** «Без sudo для приложений» трактуем так: приложение работает в контейнерах rootless (deploy в группе `docker`, добавится в 1.10.3), а sudo нужен только Ansible для провижининга (`become`). Отдельного admin-пользователя не заводим — для одиночного сервера беты избыточно. Публичный ключ deploy = наш существующий `id_ed25519.pub` (кладём в `authorized_keys` deploy; это публичный ключ, не секрет — в group_vars/all.yml).
- **Порядок против самолока.** Bootstrap-прогон под `root` (сейчас так и заходим). Роль: (1) создаёт `deploy` + ключ + sudo NOPASSWD; (2) только ПОСЛЕ этого правит sshd. `PermitRootLogin` → `prohibit-password` (root по ключу остаётся аварийным fallback, паролем — нельзя), `PasswordAuthentication no`. Даже при проблеме с deploy доступ по ключу не теряется. После подтверждения, что deploy заходит с sudo, инвентарь переключаем на `ansible_user=deploy` (он уже прописан в prod.yml).
- **Задачи, несовместимые с Molecule-контейнером** (sysctl в read-only `/proc/sys`, ufw/fail2ban без NET_ADMIN) гейтить условием (`ansible_virtualization_type != 'docker'` / molecule-переменная), чтобы Molecule оставался зелёным на идемпотентности, а на реальном VPS они выполнялись. Полная проверка фаервола/хардненинга — вживую на 31.76.15.130.

**Задачи роли `base` (tasks/main.yml, с тегами, handlers для restart):**
1. `apt` update (cache_valid_time) + пакеты: `ufw`, `fail2ban`, `unattended-upgrades`, базовые утилиты.
2. **deploy:** `ansible.builtin.user` (bash, create_home) → `authorized_key` (публичный ключ из var) → sudoers `/etc/sudoers.d/deploy` (`deploy ALL=(ALL) NOPASSWD:ALL`, `validate: visudo -cf %s`).
3. **sshd hardening** через drop-in `/etc/ssh/sshd_config.d/10-hardening.conf` (handler restart ssh): `PasswordAuthentication no`, `PermitRootLogin prohibit-password`, `PubkeyAuthentication yes`, `KbdInteractiveAuthentication no`, `X11Forwarding no`, `MaxAuthTries 4`. Основной конфиг не трогаем.
4. **ufw** (гейт): default deny incoming / allow outgoing; allow 22/80/443; enable.
5. **fail2ban** (гейт): jail для sshd (разумные bantime/findtime/maxretry), enable+start.
6. **unattended-upgrades:** только security-обновления (`20auto-upgrades` + `50unattended-upgrades`), без авто-reboot.
7. **sysctl** (гейт) `/etc/sysctl.d/60-assistant.conf`: `net.core.somaxconn=1024`, `vm.overcommit_memory=1`.
8. **timezone** из group_vars.

**Molecule:** converge применяет `site.yml` (base с реальными задачами, docker/app-заглушки); проверяем converge + **idempotence 0 changed** (гейты держат идемпотентность в контейнере). `verify.yml` расширить контейнерно-совместимыми проверками: пользователь `deploy` существует, drop-in sshd на месте, sudoers валиден.

**Живая приёмка на 31.76.15.130 (после зелёного Molecule/CI, с ОТДЕЛЬНЫМ подтверждением владельца):** прогон `ansible-playbook -i inventory/prod.yml site.yml` под root из WSL → проверить: `ssh deploy@…` заходит по ключу и `sudo` работает; парольный вход отбивается; ufw active (22/80/443); fail2ban sshd jail активен; sysctl применён; повторный прогон = 0 changed. Root по ключу остаётся (prohibit-password) как fallback на время беты — не потерять доступ.

**DoD 1.10.2:** роль наполнена; Molecule converge+idempotence зелёные в CI; ansible-lint(production)/yamllint/syntax зелёные; секретов нет; живой прогон на VPS: deploy заходит по ключу с sudo, sshd/ufw/fail2ban/unattended-upgrades/sysctl применены, повторный прогон 0 changed, доступ не потерян.

### 1.11. Прод-compose + Caddy + webhook  ≈ 1–1.5 дня
- **1.11.1** `infra/compose.prod.yml`: контейнеры `gateway`, `worker`, `scheduler` (сейчас в dev — процессы на хосте!) + инфра (postgres, pgbouncer, redis×2, infisical, searxng, embeddings, prometheus, grafana, blackbox). Отличия от dev: `restart: unless-stopped`, resource limits, healthcheck+`depends_on`, без биндов на localhost, прод-профили. ≈ 0.5 дня. **Архитектурное — состав/топологию утверждает fable.**
- **1.11.2** Caddy: TLS (Let's Encrypt по домену Б2), маршруты `/webhook/{secret_path}` → gateway (секретный путь поверх secret_token), `/oauth/callback` (задел 4.7), `/tribute/webhook` (задел этап 7); security headers, JSON access-логи (trace_id, этап 6), лимит размера тела. ≈ 0.5 дня.
- **1.11.3** [владелец] DNS A-запись на сервер (по Б2). *Ожидание владельца.*
- **1.11.4** Перевод gateway на **webhook** на проде: `python -m gateway set-webhook` с `PUBLIC_URL`, проверка secret_token и фильтра приватных чатов; отказ от polling в прод-режиме. ≈ 0.25 дня.

### 1.12. Доставка на сервер (CI-деплой)  ≈ 1–1.5 дня
- **1.12.1** CI job: build + push образа в GHCR по тегу (Dockerfile уже есть; нужен workflow-шаг и права GHCR). ≈ 0.25 дня.
- **1.12.2** Прод-Infisical: поднять инстанс на сервере, засеять прод-секреты (перевыпущенные по Б4), проверить machine identities gateway/worker/scheduler (механизм из 1.5). ≈ 0.25–0.5 дня.
- **1.12.3** Deploy-workflow: по ssh → `alembic upgrade head` ролью migrator → `docker compose -f compose.prod.yml pull && up -d` с гейтом по `/healthz`; осознанный деплой без watchtower; стратегия отката (предыдущий тег). ≈ 0.5 дня.
- **1.12.4** Expand-contract на практике: воркеры рестартуют неатомарно — старый код должен работать поверх новой схемы; проверить на паре миграций. ≈ 0.25 дня.

### 1.13. Бэкапы на проде  ≈ 0.5–1 дня
- **1.13.1** [владелец] Выбор S3-провайдера по Б3 (у другого провайдера, чем сервер). *Ожидание владельца.*
- **1.13.2** pgBackRest → S3: ежедневный full + WAL, шифрование репозитория (переиспользовать наработки 1.8). ≈ 0.5 дня.
- **1.13.3** Прод-бэкап Infisical (потеря vault = потеря всех интеграций) на то же S3. ≈ 0.25 дня.
- **1.13.4** Перенос месячной restore-проверки (6.6) на прод-контур. ≈ 0.25 дня.

### Сквозные задачи (не забыть до go-live)  ≈ 0.5 дня
- **1B-x1** [владелец+Claude] Перевыпуск ключей по Б4 и засев в прод-Infisical.
- **1B-x2** Миграция канала уведомлений владельцу с getUpdates (сейчас polling тестового бота) на прод-безопасный механизм — при webhook на боевом токене getUpdates ломается (отмечено в памяти telegram-notify-channel).
- **1B-x3** Прод-мониторинг: токен Grafana, контакт-точка алертов на боевой канал (на dev намеренно без токена, 6.4).
- **1B-x4** Прод-smoke: e2e через реального бота на webhook (онбординг → диалог на V4 Flash → инструмент → запись в usage/tool_calls_log), проверка автозапуска после reboot.

### Роллап оценки
| Блок | Работа (Claude+Codex) | Гейт |
|---|---|---|
| 1.9 провижининг | ~0.25 дня | Б1 (владелец) |
| 1.10 Ansible | ~1–1.5 дня | сервер |
| 1.11 compose.prod+Caddy | ~1–1.5 дня | Б2 (домен) |
| 1.12 CI-деплой | ~1–1.5 дня | сервер, GHCR |
| 1.13 бэкапы | ~0.5–1 дня | Б3 (S3) |
| сквозные | ~0.5 дня | Б4 (ключи) |
| **Итого** | **~4.5–6 дней работы** | + календарное ожидание владельца по Б1–Б4 |

Делегирование: инфра-как-код (Ansible/compose/Caddy/deploy-workflow) — механика по спеке → Codex, приёмка Claude (частично ручная на VPS); топология compose и схема секретов/webhook-cutover — fable; аренда/домен/S3/ключи — владелец.

[DoD 1B] Провижининг с нуля одной командой; повторный прогон Ansible — 0 changed; образ из CI доезжает до сервера, миграции применяются, сервис поднимается через healthcheck-гейт; webhook активен по домену с TLS; бэкап на проде создаётся и восстанавливается; прод-smoke через реального бота зелёный; сервис поднимается сам после reboot.
