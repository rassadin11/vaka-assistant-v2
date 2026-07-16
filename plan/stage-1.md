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
- **1.9.2** ✓ (2026-07-16, роль base) Базовый доступ: пользователь `deploy` (rootless-приложения + sudo для Ansible), ssh только по ключам, `PasswordAuthentication no`. Применено на 31.76.15.130.
- **1.9.3** ✓ (2026-07-16, роль base) Hardening: ufw (allow 22/80/443), fail2ban на sshd, unattended-upgrades (security), sysctl (`net.core.somaxconn=1024`, `vm.overcommit_memory=1`). Применено на 31.76.15.130.
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

**DoD 1.10.2:** роль наполнена; Molecule converge+idempotence зелёные в CI; ansible-lint(production)/yamllint/syntax зелёные; секретов нет; живой прогон на VPS: deploy заходит по ключу с sudo, sshd/ufw/fail2ban/unattended-upgrades/sysctl применены, повторный прогон 0 changed, доступ не потерян. **Принято 2026-07-16 (коммиты ef43c37 + 45c3692):** живой прогон под root — 13 changed; deploy по ключу + `sudo -n`; root по ключу как fallback; пароль отклонён; ufw active (22/80/443), fail2ban sshd active, sysctl 1024/1, tz Europe/Moscow, эффективный sshd подтверждены на 31.76.15.130; **второй прогон под deploy через sudo — 0 changed**. Тех-долг: `ansible_virtualization_type` → `ansible_facts['virtualization_type']` до ansible-core 2.24.

#### Детализация 1.10.3 — роль `docker` (Docker Engine + compose) (2026-07-16, перед кодом)

**Объём:** наполнить `roles/docker` реальными задачами (в 1.10.1 — заглушка). Роль `app` остаётся заглушкой (её черёд — 1.10.4).

**Решения:**
- **Официальный репозиторий Docker**, не `docker.io` из Ubuntu. GPG-ключ в `/etc/apt/keyrings/docker.asc` + apt-репозиторий `download.docker.com/linux/ubuntu {{ ansible_distribution_release }} stable`; пакеты `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, `docker-compose-plugin` пиновыми версиями из group_vars (`docker_version`, `docker_compose_plugin_version`) — воспроизводимость.
- **deploy в группе `docker`** (решение 1.10.2: приложение — в контейнерах, deploy управляет docker без sudo). Осознаём: членство в docker-группе = root-эквивалент; для одиночного сервера беты приемлемо (у deploy уже есть NOPASSWD-sudo). Правка группы применяется к новым сессиям (в живой приёмке проверять из свежего SSH-логина).
- **daemon.json** с прод-настройками: ротация логов (`log-driver: json-file`, `max-size: 10m`, `max-file: 3` — иначе логи контейнеров съедят диск), `live-restore: true` (контейнеры переживают рестарт демона). Хендлер restart docker при изменении файла.
- **Несовместимое с Molecule-контейнером** (запуск/рестарт `dockerd` — в контейнере нет systemd-докера, docker-in-docker не нужен) гейтить по `virtualization_type`, как в base. Установка пакетов/репозитория/GPG/группы/daemon.json — выполняются и в контейнере (идемпотентность проверяется), старт демона — только на реальном VPS.

**Задачи роли `docker` (tasks/main.yml, теги, handler restart docker):**
1. Зависимости репозитория: `ca-certificates`, `curl`, `gnupg` (идемпотентно, часть уже из base).
2. `/etc/apt/keyrings` (0755) + GPG-ключ Docker через `ansible.builtin.get_url` в `/etc/apt/keyrings/docker.asc` (0644).
3. Apt-репозиторий Docker через `ansible.builtin.apt_repository` (`signed-by=/etc/apt/keyrings/docker.asc`).
4. `apt` update + установка пяти пакетов пиновыми версиями (`docker-ce={{ docker_version }}` и т.д.; `docker-compose-plugin={{ docker_compose_plugin_version }}`).
5. `/etc/docker/daemon.json` (`copy` content или `template`, 0644, валидный JSON) → notify Restart docker.
6. deploy в группу `docker` (`ansible.builtin.user` с `append: true`, `groups: docker`).
7. (гейт) сервис `docker` enabled + started.

**Molecule:** converge применяет site.yml (base + docker с реальными задачами, app-заглушка); проверяем converge + **idempotence 0 changed** (гейт старта демона держит идемпотентность в контейнере). `verify.yml` дописать контейнерно-совместимыми проверками: бинарь `docker` присутствует, `docker compose version` (plugin поставлен), deploy состоит в группе `docker`, `/etc/docker/daemon.json` существует и парсится как JSON.

**Живая приёмка на 31.76.15.130 (после зелёного Molecule/CI):** прогон роли под `deploy` с `--become`; проверить: `docker --version` и `docker compose version` нужных версий; `systemctl is-active docker` = active; из **свежего** SSH-логина `docker ps` работает без sudo (членство в группе); daemon.json применён (ротация логов); повторный прогон 0 changed.

**DoD 1.10.3:** роль наполнена; Molecule converge+idempotence зелёные в CI; ansible-lint(production)/yamllint/syntax зелёные; секретов нет; на VPS: Docker Engine + compose-plugin нужных версий, демон active, deploy управляет docker без sudo, повторный прогон 0 changed. **Принято 2026-07-16 (коммиты 739d0a4 + 21325e1):** live под deploy — 6 changed; docker 27.5.1 + compose v2.32.4, демон active, `docker ps` без sudo из свежей сессии, daemon.json (json-file + live-restore) применён; **второй прогон 0 changed**. molecule converge+idempotence+verify зелёные локально, lint/yamllint/syntax/gitleaks зелёные. На приёмке пойман баг get_url (`src`→`url`). Тех-долг: `apt_repository` → `deb822_repository` до ansible-core 2.25.

#### Детализация 1.10.4 — роль `app` (каркас: каталоги + unit) (2026-07-16, перед кодом)

**Объём осознанно урезан до domain/архитектурно-независимого каркаса.** Полная роль app по плану = каталоги + выкладка `compose.prod.yml` + unit/автозапуск + entrypoint секретов Infisical. Сейчас делаем ТОЛЬКО независимое от нерешённого:
- ✅ каталоги приложения;
- ✅ systemd-unit автозапуска (установлен + daemon-reload, **НЕ enabled / НЕ started**);
- ⏸ **выкладка `compose.prod.yml` — отложена до 1.11.1** (топологию прод-compose утверждает fable);
- ⏸ **`/etc/assistant/bootstrap.env` (machine-identity Infisical) — отложен до 1.12.2** (прод-Infisical поднимается там; секреты — перевыпущенные по Б4). Каталог `/etc/assistant` создаём сейчас, файл — нет.

Причина урезки: unit ссылается на будущий `compose.prod.yml` и не стартует, пока его нет; `bootstrap.env` требует прод-Infisical и ключей Б4. Заводить вслепую = брак.

**Решения:**
- **app_dir из group_vars** (`/opt/assistant`), владелец `deploy:deploy`. Сюда ляжет `compose.prod.yml` (1.11.1) и, при необходимости, bind-каталоги (их состав — вместе с compose, 1.11.1).
- **systemd system-unit `assistant.service`**: `Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=/usr/bin/docker compose -f {{ app_dir }}/compose.prod.yml up -d`, `ExecStop=… down`, `WorkingDirectory={{ app_dir }}`, `Requires`/`After=docker.service`, `User={{ deploy_user }}` (deploy в группе docker — без sudo), `WantedBy=multi-user.target`. **НЕ enable / НЕ start** — включение и первый старт в 1.12.3 (когда есть образ из GHCR + compose.prod.yml + секреты). Иначе на reboot systemd поднимет failed-unit.
- **daemon-reload** — handler по изменению unit-файла (без изменения не перечитываем → идемпотентно). Обращается к systemd-шине, которой в molecule-контейнере нет (падает «Failed to connect to bus») → **гейтится по `virtualization_type`** (как base/docker); на реальном VPS выполняется.

**Задачи роли `app` (tasks/main.yml, теги, handler reload systemd):**
1. Каталог `{{ app_dir }}` (state: directory, owner/group `{{ deploy_user }}`, mode 0755).
2. Каталог `/etc/assistant` (owner root, group `{{ deploy_user }}`, mode 0750 — под будущий bootstrap.env).
3. systemd-unit `/etc/systemd/system/assistant.service` (copy/template, 0644, root) → notify Reload systemd. Содержимое — см. решения.
(НЕ добавлять задачи enable/start; НЕ создавать bootstrap.env / compose.prod.yml.)

**handlers/main.yml:** `Reload systemd` — `ansible.builtin.systemd: daemon_reload: true`, гейт по `virtualization_type` (как base/docker; в контейнере нет systemd-шины).

**Molecule `verify.yml` дописать (контейнерно-совместимо):** `{{ app_dir }}` существует, владелец deploy; `/etc/assistant` существует, mode 0750; unit-файл `/etc/systemd/system/assistant.service` существует; `systemctl is-enabled assistant.service` — **не** enabled (мы не включали); содержимое unit содержит `ExecStart` с `{{ app_dir }}/compose.prod.yml`.

**Живая приёмка на 31.76.15.130:** прогон под deploy `--become --tags app`; каталоги с нужным владельцем/правами; `assistant.service` установлен, `systemctl is-enabled` = disabled, `systemctl status` = inactive (не стартуем); повторный прогон 0 changed.

**DoD 1.10.4 (каркас):** роль наполнена в объёме каркаса; Molecule converge+idempotence+verify зелёные; ansible-lint(production)/yamllint/syntax зелёные; секретов нет; на VPS каталоги + unit установлены, сервис не запущен, повторный прогон 0 changed. Отложенные части (compose.prod.yml → 1.11.1, bootstrap.env → 1.12.2, enable/start → 1.12.3) явно задокументированы. **Принято 2026-07-16:** live под deploy — 3 changed (каталоги + unit + daemon-reload); на VPS `/opt/assistant` (deploy:deploy 0755), `/etc/assistant` (root:deploy 0750), unit виден systemd, `is-enabled=disabled`/`is-active=inactive`; второй прогон 0 changed. molecule converge+idempotence+verify + lint/yamllint/syntax зелёные. Приёмка починила: ansible-lint (systemctl→stat wants-симлинка), гейт daemon-reload, литерал `/opt/assistant` в verify (group_vars не грузятся в verify-плее).

### 1.11. Прод-compose + Caddy + webhook  ≈ 1–1.5 дня
- **1.11.1** `infra/compose.prod.yml`: контейнеры `gateway`, `worker`, `scheduler` (сейчас в dev — процессы на хосте!) + инфра (postgres, pgbouncer, redis×2, infisical, searxng, embeddings, prometheus, grafana, blackbox). Отличия от dev: `restart: unless-stopped`, resource limits, healthcheck+`depends_on`, без биндов на localhost, прод-профили. ≈ 0.5 дня. **Архитектурное — состав/топологию утверждает fable.**
- **1.11.2** Caddy: TLS (Let's Encrypt по домену Б2), маршруты `/webhook/{secret_path}` → gateway (секретный путь поверх secret_token), `/oauth/callback` (задел 4.7), `/tribute/webhook` (задел этап 7); security headers, JSON access-логи (trace_id, этап 6), лимит размера тела. ≈ 0.5 дня.
- **1.11.3** [владелец] DNS A-запись на сервер (по Б2). *Ожидание владельца.*
- **1.11.4** Перевод gateway на **webhook** на проде: `python -m gateway set-webhook` с `PUBLIC_URL`, проверка secret_token и фильтра приватных чатов; отказ от polling в прод-режиме. ≈ 0.25 дня.

#### Детализация 1.11.1 — `compose.prod.yml`: топология (2026-07-16, fable, перед кодом)

**Уточнения плана по факту кода:**
- Отдельного контейнера `scheduler` НЕ будет: `SchedulerProcessor` и `OutboxProcessor` запускаются внутри процесса воркера (`worker/__main__.py`), конкурентность безопасна (`FOR UPDATE … SKIP LOCKED`, идемпотентные ключи). Прод-контейнеры приложения: `gateway` + `worker` (масштаб — репликами воркера).
- Прод не собирает образы на сервере: три prebuilt-образа из GHCR — app (`ghcr.io/rassadin11/vaka-assistant-v2`, Dockerfile корня, команда на сервис), кастомный postgres (pg16+partman+pgbackrest, `…-postgres`), embeddings (`…-embeddings`, Dockerfile появится в 1.12.1). **Правка объёма 1.12.1: CI собирает и пушит три образа, не один.** До 1.12 compose обязан быть валидным (`config -q`), но не стартует — unit не enabled, это ок.

**Файлы в репо:** `infra/compose.prod.yml` + `infra/prod/` только для отличающихся от dev конфигов (prometheus prod, grafana datasources prod, postgresql.prod.conf); совпадающие конфиги (redis, pgbouncer, searxng, blackbox, grafana dashboards/alerting) переиспользуются из dev-каталогов без дублирования.

**Секреты и env (механизм):**
- `/etc/assistant/bootstrap.env` — machine-identity Infisical + DSN БД для gateway/worker. Роль app кладёт **шаблон с пустыми значениями** (root:deploy 0640, `force: no`) — иначе `docker compose config` на сервере не проходит (env_file обязателен); боевые значения засеваются в 1.12.2, как решено в 1.10.4.
- `/etc/assistant/infra.env` — инфраструктурные секреты, которые не могут жить в Infisical (курица-яйцо): POSTGRES_PASSWORD, PGBACKREST_*_CIPHER_PASS, ENCRYPTION_KEY/AUTH_SECRET/DB_CONNECTION_URI Infisical, SEARXNG_SECRET, GF_SECURITY_ADMIN_PASSWORD, TELEGRAM_ALERT_*. Значения засеваются в 1.12.2 (ключи Б4); сейчас роль app кладёт **шаблон с пустыми значениями** (root:deploy 0640, `force: no` — боевой файл не перезатирать).
- Пути env_file в compose — через `${ENV_FILE_DIR:-/etc/assistant}/…`: на сервере дефолт, локально/в CI валидация со stub-файлами в scratch-каталоге.
- Несекретная топология (адреса сервисов) — `environment` прямо в compose: redis-queue/redis-cache:6379, `SEARXNG_URL=http://searxng:8080`, embeddings:8000, `INFISICAL_URL=http://infisical:8080`, `LOG_FORMAT=json`. Имена переменных взять из фактических конфигов кода (gateway/config.py, worker/__main__.py, core/db.py, core/secrets.py) — не выдумывать. **DSN БД (`DATABASE_URL`/`SERVICE_DATABASE_URL`) содержат пароли → это секреты, в compose их НЕТ** — доставляются env-файлами /etc/assistant в 1.12.2. Туда же (1.12.2) — прод-пароли ролей Postgres + userlist pgbouncer + пароль metrics_ro для Grafana (в prod-datasource — `$__env{GRAFANA_METRICS_RO_PASSWORD}` из infra.env).

**Критичное правило портов: published-порты Docker обходят ufw.** Наружу не публикуется НИЧЕГО (80/443 добавит caddy в 1.11.2). Админ-доступ — только биндами на 127.0.0.1 (SSH-туннель): grafana 3000, prometheus 9090, infisical 8880. Никаких `5432:5432`/`6379:6379` как в dev.

**Состав сервисов** (все: `restart: unless-stopped`, одна default-сеть, логи — ротация из daemon.json):
- `gateway`: app-образ `${APP_IMAGE:-ghcr.io/rassadin11/vaka-assistant-v2:latest}`, `command: python -m gateway serve`, expose 8000 (без publish), env_file bootstrap.env, healthcheck GET /healthz (python -c fetch, curl в образе нет), depends_on healthy: pgbouncer, redis-queue, redis-cache.
- `worker`: тот же образ, `command: python -m worker`, `deploy.replicas: 2`, `WORKER_METRICS_PORT=9100` (expose), env_file bootstrap.env, depends_on healthy: pgbouncer, redis-queue, redis-cache (searxng/embeddings не гейтят — мягкая деградация по плану).
- `postgres`: образ `…-postgres` из GHCR, без publish; volumes как dev (postgres_data, pgbackrest_repo/spool, init-roles.sql, pgbackrest.conf) + `postgresql.prod.conf` (копия dev-конфига с памятью под 16 ГиБ: shared_buffers 2GB, effective_cache_size 6GB; archive/pgbackrest без изменений); POSTGRES_PASSWORD и cipher-pass — из infra.env.
- `pgbouncer`, `redis-queue`, `redis-cache`, `searxng`, `blackbox`: как dev (те же конфиги/healthchecks), без publish; SEARXNG_SECRET из infra.env.
- `embeddings`: образ `…-embeddings`, дефолтный профиль (в проде нужен), volume hf_cache для HF_HOME, healthcheck /healthz.
- `prometheus`: конфиг `infra/prod/prometheus/prometheus.yml` — gateway:8000; воркер-реплики через `dns_sd_configs` (name `worker`, type A, port 9100), не static; blackbox-джоб как dev. Bind 127.0.0.1:9090.
- `grafana`: bind 127.0.0.1:3000; **анонимный доступ выключен** (в отличие от dev), admin-пароль из infra.env; datasources prod (`pgbouncer:6432` вместо host.docker.internal); dashboards/alerting provisioning — dev-файлы как есть; TELEGRAM_ALERT_* из infra.env (боевой канал — 1B-x3).
- `infisical` + `infisical-db` + `infisical-redis`: как dev, все секреты из infra.env, infisical bind 127.0.0.1:8880.
- НЕ включать: postgres-restore (restore-контур на проде — 1.13.4).

**mem_limit** (сервер 15 GiB, лимиты — потолки, не резервы): postgres 3g (shared_buffers 2GB — при 2.5g риск OOM-kill), embeddings 3g, worker 1g×2, gateway 768m, infisical 1g, infisical-db 512m, infisical-redis 256m, grafana 512m, prometheus 512m, searxng 512m, redis-queue 512m, redis-cache 384m, pgbouncer 128m, blackbox 128m (сумма ≈ 12.7g + запас хосту).

**Выкладка (расширение роли app):** copy `compose.prod.yml` → `/opt/assistant/compose.prod.yml` (deploy:deploy 0644); дерево конфигов → `/opt/assistant/config/…` (redis, pgbouncer, searxng, postgres, blackbox, prometheus prod, grafana provisioning: dev dashboards/alerting + prod datasources); volumes в compose ссылаются на `./config/…` (unit имеет WorkingDirectory=/opt/assistant). Источники — файлы репо через путь от playbook_dir, в роли конфиги не дублировать. Шаблон infra.env — см. выше. Molecule verify: файлы выложены, unit не enabled.

**Принято 2026-07-16 (коммиты 5b0ee70 + bcf233b):** Codex-реализация + приёмка fable. На приёмке починено: (1) захардкоженный dev-пароль в DSN воркера — DSN убраны из compose (секреты, доставка 1.12.2); (2) dev-пароль в prod-datasource Grafana — заменён на `$__env{GRAFANA_METRICS_RO_PASSWORD}` из infra.env; (3) postgres mem_limit 2.5g→3g (OOM-риск при shared_buffers 2GB); (4) живой прогон упал — каталог config/postgres не создавался перед пофайловым копированием (molecule в CI поймал то же на 5b0ee70); (5) роль дополнительно кладёт пустой шаблон bootstrap.env (force: no) — иначе `docker compose config` на сервере не проходит. Живая приёмка на 31.76.15.130: файлы выложены (compose + config-дерево + оба env-шаблона 0640 root:deploy), `docker compose -f compose.prod.yml config -q` OK на сервере, unit disabled/inactive, повторный прогон **0 changed**. yamllint/gitleaks чисто, оба CI-workflow зелёные (molecule converge+idempotence+verify).

**DoD 1.11.1:** compose.prod.yml в репо, `docker compose -f infra/compose.prod.yml config -q` зелёный локально (stub env-файлы) — проверку добавить в CI рядом с существующей валидацией compose; роль app выкладывает compose+конфиги+шаблон infra.env; molecule converge+idempotence+verify и ansible-lint(production)/yamllint/syntax зелёные; на VPS: файлы на месте, `docker compose … config -q` проходит, unit по-прежнему disabled/inactive, повторный прогон 0 changed; published-портов наружу нет (только 127.0.0.1); секретов в репо нет (gitleaks).

#### Детализация 1.11.2 — Caddy (2026-07-16, fable, перед кодом)

**Смена домена (правка Б2):** владелец выбрал и отрепоинтил **vaka-assistant.ru** (apex A → 31.76.15.130, подтверждено резолвом с самого VPS 2026-07-16), а не vakachat.ru из прежней записи Б2. `www`-записи нет — Caddy обслуживает только apex (www опционально добавить позже вместе с DNS-записью). Privacy policy (вторая половина Б2) — по-прежнему за владельцем, публиковаться будет на `https://vaka-assistant.ru/...`.

**Решения:**
- Сервис `caddy` в compose.prod.yml: пин `caddy:2-alpine`, ports `80:80`, `443:443` + `443:443/udp` (HTTP/3) — единственные published-порты наружу; volumes: `./config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro` + named volumes `caddy_data` (сертификаты; терять нельзя — rate limit LE) и `caddy_config`; `restart: unless-stopped`, mem_limit 256m; **без depends_on** — Caddy живёт независимо от приложения (upstream down → 502), это позволяет принять TLS до появления образов приложения.
- `infra/prod/caddy/Caddyfile`: глобально — `email` владельца для ACME; сайт `vaka-assistant.ru` — маршруты `/webhook/*`, `/oauth/callback` (задел 4.7), `/tribute/webhook` (задел этапа 7) → `reverse_proxy gateway:8000`; всё остальное → `respond 404`. **Секретный путь webhook в Caddyfile НЕ фигурирует** — валидация пути и secret_token остаётся в gateway (секрет не покидает Infisical). `/healthz` наружу не публикуем (меньше поверхность; blackbox ходит изнутри).
- Security headers: HSTS (`max-age=31536000`), `X-Content-Type-Options nosniff`, `X-Frame-Options DENY`, `Referrer-Policy no-referrer`. Лимит тела: `request_body max_size 1MB` (webhook-апдейты Telegram маленькие). Access-логи: JSON в stdout (ротация — daemon.json), формат с полями по этапу 6.
- Роль app: каталог `infra/prod/caddy` добавляется в цикл выкладки конфигов (`config/caddy`); molecule verify — файл Caddyfile выложен.
- **Живая приёмка без приложения:** на сервере `docker compose -f compose.prod.yml up -d caddy` (только caddy; unit по-прежнему disabled — включение в 1.12.3, но caddy остаётся запущенным, сертификат уже в volume); проверка: `curl -sI https://vaka-assistant.ru/` → валидный LE-сертификат + 404; `/webhook/x` → 502 (upstream ещё не существует — это ожидаемо и правильно); HTTP→HTTPS редирект (Caddy делает сам); повторный прогон роли 0 changed.

**Принято 2026-07-16 (коммит a4a12a2):** Codex-реализация, приёмка fable без правок. Живая приёмка: роль app доставила config/caddy (2 changed → 0 changed), `docker compose up -d caddy` на сервере — healthy; сертификат Let's Encrypt получен вживую (issuer LE/YE1, до 2026-10-14), `https://vaka-assistant.ru/` → 404 с полным набором security-заголовков (HSTS, nosniff, DENY, no-referrer, Server скрыт), HTTP→HTTPS 308, `/webhook/*` → 502 (upstream ожидаемо отсутствует до 1.12); проверено и с сервера, и с внешней машины. gitleaks чисто, оба CI-workflow зелёные. Caddy оставлен запущенным (серт в caddy_data).

**DoD 1.11.2:** Caddyfile в репо, сервис в compose (`config -q` зелёный локально и на сервере); наружу опубликованы только 80/443 caddy; TLS-сертификат Let's Encrypt получен вживую на vaka-assistant.ru, HTTP→HTTPS редирект работает, маршруты проксируют на gateway (пока 502), остальное 404 с security-заголовками; molecule/lint/CI зелёные; секретов в диффе нет.

### 1.12. Доставка на сервер (CI-деплой)  ≈ 1–1.5 дня
- **1.12.1** CI job: build + push образа в GHCR по тегу (Dockerfile уже есть; нужен workflow-шаг и права GHCR). ≈ 0.25 дня.
- **1.12.2** Прод-Infisical: поднять инстанс на сервере, засеять прод-секреты (перевыпущенные по Б4), проверить machine identities gateway/worker/scheduler (механизм из 1.5). ≈ 0.25–0.5 дня.
- **1.12.3** Deploy-workflow: по ssh → `alembic upgrade head` ролью migrator → `docker compose -f compose.prod.yml pull && up -d` с гейтом по `/healthz`; осознанный деплой без watchtower; стратегия отката (предыдущий тег). ≈ 0.5 дня.
- **1.12.4** Expand-contract на практике: воркеры рестартуют неатомарно — старый код должен работать поверх новой схемы; проверить на паре миграций. ≈ 0.25 дня.

#### Детализация 1.12.1 — сборка образов в CI (2026-07-16, fable, перед кодом)

**Объём (правка 1.11.1 действует):** три образа в GHCR, не один. Репозиторий публичный → пакеты GHCR делаем **публичными** (сервер тянет без docker login; код и так открыт). Права — `GITHUB_TOKEN` с `packages: write`.

**Решения:**
- Отдельный workflow `images.yml` (не в основной CI): push в `main` + `workflow_dispatch`. Теги каждого образа: `latest` + `sha-<short_sha>` (деплой 1.12.3 пинует sha; откат = предыдущий sha).
- **app** (`ghcr.io/rassadin11/vaka-assistant-v2`): существующий корневой Dockerfile, собирается на каждый push в main.
- **postgres** (`…-postgres`): контекст `infra/postgres/` (тот же build, что dev). Job гейтится по `paths: infra/postgres/**` + dispatch.
- **embeddings** (`…-embeddings`): новый `Dockerfile.embeddings` в корне — python3.12-slim + uv, `uv sync --frozen --no-dev --group embeddings` (+ пин transformers <5 уже в pyproject), копируются `embeddings/` и метаданные проекта, CMD `uvicorn embeddings.app:app --host 0.0.0.0 --port 8000`. **Модель e5-large в образ НЕ запекается** (иначе образ ~5 ГБ): скачивается при первом старте в volume `hf_cache` → в compose.prod поднять `start_period` healthcheck embeddings до 600s (первый старт качает ~2.2 ГБ).
- Кэш сборки: `docker/build-push-action` с gha-кэшем.

**DoD 1.12.1:** workflow зелёный; три пакета видны в GHCR как public; `docker pull` всех трёх образов с VPS проходит без логина; app-образ запускает `python -m gateway --help`-эквивалент (smoke в workflow: `docker run --rm <image> python -c "import gateway, worker, core, tools"`); compose.prod: start_period embeddings обновлён; секретов в диффе нет.

**Принято 2026-07-16 (коммит 64da04f):** Codex-реализация (workflow + Dockerfile.embeddings + start_period), приёмка fable. Нюансы приёмки: локальная валидационная сборка embeddings-образа у Codex упёрлась в переполненный диск C: машины владельца — остановлена, реальная проверка перенесена в Actions (штатная среда сборки); для первой сборки postgres-образа добавлен комментарий-триггер в infra/postgres/Dockerfile (paths-гейт). Живая приёмка: workflow Images зелёный (build+push+smoke-импорт), все три образа скачаны на VPS `docker pull` анонимно (пакеты public). Инцидент машины владельца: C: был заполнен в ноль — почищены uv-кэш и docker build-кэш; рекомендация владельцу — перенести данные Docker Desktop на D:.

#### Детализация 1.12.2 — прод-Infisical + секреты (2026-07-16, fable, перед кодом)

**Уточнение по факту кода:** gateway/worker читают секреты из окружения (`EnvSecretsProvider`); `InfisicalSecretsProvider` в entrypoints не вшит. Механизм прода — **entrypoint секретов** (заявлен ещё в плане роли app): новый модуль `core/secrets_entrypoint.py`, запуск `python -m core.secrets_entrypoint -- <команда…>`: логин machine identity (INFISICAL_CLIENT_ID/SECRET/PROJECT_ID/ENV из окружения), выборка всех секретов пути `/` целевого окружения, мёрж в env (**существующие переменные окружения приоритетнее** — топология из compose не перетирается), `os.exec*` команды. Ошибка логина/выборки = немедленный выход ≠ 0 (fail-closed, restart policy перезапустит). Юнит-тесты с мок-транспортом (без сети, инвариант). Compose: `command: python -m core.secrets_entrypoint -- python -m gateway serve` (аналогично worker).

**Вскрытые дефекты топологии 1.11.1 (исправить в 1.12.2):**
1. Общий `POSTGRES_PASSWORD` из infra.env уезжает и в основной postgres, и в infisical-db (одинаковый пароль + лишние секреты в чужих контейнерах). Решение: **убрать `env_file` у инфра-сервисов**, перейти на compose-интерполяцию `${VAR:?}` с отдельными ключами (`INFISICAL_DB_PASSWORD`, `INFISICAL_ENCRYPTION_KEY`, `INFISICAL_AUTH_SECRET`, `INFISICAL_DB_CONNECTION_URI`, `SEARXNG_SECRET`, `GF_…`, `PGBACKREST_…`); источник интерполяции — **symlink `/opt/assistant/.env` → `/etc/assistant/infra.env`** (ставит роль app; compose автоматически читает `.env` в WorkingDirectory юнита; deploy читает файл через группу). env_file остаётся только у gateway/worker (bootstrap.env = identity). CI-валидация: stub `.env` с фиктивными значениями всех обязательных ключей (`:?` с пустым stub падает — это осознанно, список обязательных ключей проверяется).
2. `init-roles.sql` и `pgbouncer/userlist.txt` копируются из dev **с dev-паролями ролей**. Решение: пароли ролей БД (`app`, `service`, `metrics_ro`, superuser `assistant`) на проде генерируются при засеве; после первого старта postgres — `ALTER ROLE … PASSWORD` (идемпотентный скрипт засева); **прод-userlist** генерируется из этих паролей и живёт в `/etc/assistant/pgbouncer-userlist.txt` (вне репо), compose монтирует его вместо репо-копии (`${ENV_FILE_DIR:-/etc/assistant}/pgbouncer-userlist.txt`); CI-stub — пустой файл. DSN `DATABASE_URL`/`SERVICE_DATABASE_URL` с этими паролями кладутся в Infisical (env prod) — и **убираются из шаблона bootstrap.env** (identity-only, как решено в 1.10.4).
3. Grafana prod-datasource использует `$__env{GRAFANA_METRICS_RO_PASSWORD}` — переменная приходит в контейнер grafana интерполяцией (п.1), ключ уже в infra.env.

**Прод-bootstrap Infisical:** параметризовать `infra/infisical/bootstrap.py` (сейчас dev-константы): INFISICAL_URL, ENV_NAME/SLUG, пути input/output env-файлов, имена org/project — через переменные окружения с текущими dev-дефолтами (dev-поведение байт-в-байт). Запуск на сервере: `docker compose up -d infisical-db infisical-redis infisical` → bootstrap против `http://127.0.0.1:8880` → 3 identities (gateway/worker/scheduler; используем gateway+worker, scheduler живёт в воркере) → client_id/secret в `/etc/assistant/bootstrap.env`.

**Засев секретов (порядок на сервере, выполняю я по чек-листу):** (1) сгенерировать infra.env (openssl rand; TELEGRAM_ALERT_* — пусто до 1B-x3); (2) поднять infisical-стек, прод-bootstrap; (3) первый старт postgres + ALTER ROLE + прод-userlist; (4) засеять в Infisical (env prod): OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, GROQ_API_KEY (**перевыпущенные — Б4, владелец**), DATABASE_URL, SERVICE_DATABASE_URL, ADMIN_TELEGRAM_IDS, WEBHOOK_SECRET_PATH/TELEGRAM_WEBHOOK_SECRET_TOKEN (сгенерировать), PUBLIC_URL=https://vaka-assistant.ru, USD_RUB_RATE и прочие несекретные настройки — по перечню env-переменных из кода. **Передача ключей Б4:** владелец кладёт значения в `C:\Users\Artem\.assistant\prod-secrets.env` на своей машине (НЕ в Telegram-чат) — забираю оттуда и сею.

**Делегирование:** код (entrypoint + параметризация bootstrap.py + правки compose/роли/CI-stub) — Codex; генерация значений и засев на сервере — Claude руками (секреты делегатам не передавать); Б4 — владелец.

**DoD 1.12.2:** юнит-тесты entrypoint зелёные (без сети); dev-поведение bootstrap.py не изменилось; `docker compose config -q` зелёный в CI со stub-значениями; на сервере: infisical-стек healthy, прод-bootstrap выполнен, identities в bootstrap.env, секреты в Infisical (env prod), пароли ролей БД боевые, прод-userlist на месте; `python -m core.secrets_entrypoint -- env` на сервере (из app-образа, с identity воркера) печатает переменные из Infisical; секретов в репо нет (gitleaks).

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
