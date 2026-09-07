# Runbook профиля «small»

Эксплуатация установки на 2 ГБ для закрытого круга 5–10 человек. План профиля — [plan/small-profile.md](../plan/small-profile.md); прод (16 ГБ, домен, Infisical, Ansible) живёт по другому документу — [docs/runbook-deploy.md](runbook-deploy.md), не путать.

Статус документа: **черновик, написан до появления сервера** (2026-09-07). Всё, что помечено «проверено локально», проверено на dev-машине; всё остальное проверяется при первом развёртывании и правится по факту. Пункт S16 закрывается только после того, как по этому документу кто-то развернул стек с нуля.

## 0. Чем эта установка отличается

| | прод | small |
|---|---|---|
| контейнеров | 17 | 6 |
| Telegram | webhook + Caddy + домен | polling, входящих портов нет |
| секреты | Infisical | два env-файла с правами 600 |
| БД | postgres + PgBouncer | postgres напрямую |
| поиск по фактам и документам | векторный (эмбеддинги) | полнотекстовый Postgres |
| наблюдаемость | Prometheus + Grafana + blackbox | `docker compose logs` + healthcheck |
| бэкапы | pgbackrest | `pg_dump` по расписанию |
| деплой | CI → GHCR → Ansible | `git pull && docker compose up -d --build` |

Открытый порт на машине ровно один — SSH.

## 1. Развёртывание с нуля

### 1.1 Подготовка хоста

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-plugin
sudo mkdir -p /opt/assistant && sudo chown "$USER" /opt/assistant
git clone <repo> /opt/assistant && cd /opt/assistant
sudo infra/small/prepare-host.sh
```

`prepare-host.sh` делает swap 2 ГБ, `vm.swappiness=10`, `vm.overcommit_memory=1` и еженедельный `docker system prune` таймером systemd. Скрипт идемпотентен: после перезагрузки его можно запустить снова и убедиться, что он ничего не меняет — это и есть проверка «настройки пережили ребут».

### 1.2 Секреты

Два файла, оба `chmod 600`, оба вне репозитория:

```bash
sudo mkdir -p /etc/assistant
sudo install -m 600 /dev/null /etc/assistant/small-infra.env
sudo install -m 600 /dev/null /etc/assistant/small.env
```

`/etc/assistant/small-infra.env` — то, что не должно попадать в долгоживущие контейнеры приложения:

```
POSTGRES_PASSWORD=<случайный>
MIGRATOR_DB_PASSWORD=<случайный>
APP_DB_PASSWORD=<случайный>
SERVICE_DB_PASSWORD=<случайный>
MIGRATOR_DATABASE_URL=postgresql+psycopg://migrator:<MIGRATOR_DB_PASSWORD>@postgres:5432/assistant
SEARXNG_SECRET=<случайный>
```

Схема `postgresql+psycopg://` обязательна: с `postgresql://` SQLAlchemy требует psycopg2, которого в образе нет (та же грабля, что и на проде).

`/etc/assistant/small.env` — секреты приложения:

```
TELEGRAM_BOT_TOKEN=<токен бота>
OPENROUTER_API_KEY=<ключ>
GROQ_API_KEY=<ключ для распознавания голоса>
DATABASE_URL=postgresql://app:<APP_DB_PASSWORD>@postgres:5432/assistant
SERVICE_DATABASE_URL=postgresql://service:<SERVICE_DB_PASSWORD>@postgres:5432/assistant
ADMIN_TELEGRAM_IDS=<id владельца>
ALLOWED_TELEGRAM_IDS=<id через запятую>
```

`ALLOWED_TELEGRAM_IDS` — шлагбаум профиля: пользователь не из списка получает отказ и в БД не создаётся. Пустое значение выключает whitelist целиком — на этой установке так делать нельзя.

Пустая переменная считается незаданной: строка `TELEGRAM_BOT_TOKEN=` без значения означает «токена нет», и сервис скажет об этом в лог вместо падения в цикле рестартов.

### 1.3 Запуск

```bash
cd /opt/assistant
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env up -d --build
docker ps --format '{{.Names}}\t{{.Status}}'
```

Порядок задан самим compose: postgres → миграции одноразовым контейнером → gateway и worker. Первая сборка образа занимает несколько минут.

**Терпение на старте.** У gateway и worker `start_period` 90 секунд: только импорт зависимостей стоит ~20 секунд на dev-машине и больше на одном vCPU. `health: starting` в первые полторы минуты — норма, а не поломка.

Ожидаемое состояние: `postgres`, `redis`, `searxng`, `gateway`, `worker` — healthy; `migrate` — `Exited (0)`.

### 1.4 Перевод бота на эту установку

Пока на токене висит webhook, `getUpdates` отвечает 409 и polling не стартует. Порядок:

```bash
# на старом (прод) сервере: погасить стек, чтобы два потребителя не делили один токен
sudo systemctl stop assistant

# на новом сервере: снять webhook (по умолчанию накопленные апдейты сохраняются)
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env \
  run --rm gateway python -m gateway delete-webhook

docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env up -d gateway
```

`--drop-pending` добавлять только осознанно: он выбрасывает всё, что Telegram накопил за время переключения.

Healthcheck gateway — не «процесс жив», а ключ `gw:polling:heartbeat` в Redis, который обновляется, только пока polling реально может получать апдейты. Если контейнер unhealthy, а логи молчат — почти наверняка на токене снова webhook (проверить: `run --rm gateway python -m gateway delete-webhook`).

## 2. Обновление

```bash
cd /opt/assistant
git pull
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env up -d --build
```

Миграции применяет контейнер `migrate` до старта приложения. Откат: `git checkout <прошлый sha>` и та же команда — миграции обратно совместимы с предыдущей версией кода (expand-contract), поэтому откат кода не требует отката схемы.

## 3. Бэкапы

Ночной запуск (crontab пользователя, у которого есть доступ к docker):

```
30 3 * * * cd /opt/assistant && env $(cat /etc/assistant/backup.env | xargs) infra/small/backup.sh >> /var/log/assistant-backup.log 2>&1
```

`/etc/assistant/backup.env` (`chmod 600`):

```
BACKUP_PASSPHRASE=<длинный случайный>
BACKUP_DIR=/var/backups/assistant
BACKUP_KEEP_DAYS=14
BACKUP_SCP_TARGET=user@другая-машина:/backups/assistant     # либо BACKUP_RCLONE_REMOTE
COMPOSE_ENV_FILE=/etc/assistant/small-infra.env
```

Скрипт **откажется работать без внешнего адресата**: дамп, который лежит на том же диске, погибнет вместе с ним. Обход `BACKUP_ALLOW_LOCAL_ONLY=1` — только для учений.

**Пароль от бэкапа хранить отдельно от сервера.** Дамп зашифрован AES-256; потерянный пароль означает потерянный бэкап.

### 3.1 Учения по восстановлению

Раз в месяц (и обязательно после любого изменения схемы):

```bash
cd /opt/assistant
env BACKUP_PASSPHRASE=<...> COMPOSE_ENV_FILE=/etc/assistant/small-infra.env \
  infra/small/restore.sh /var/backups/assistant/<файл>.dump.enc assistant_restore_drill
```

Скрипт создаёт отдельную БД, накатывает миграции, восстанавливает **только данные** и печатает число строк по таблицам. Сверить с боевой БД:

```bash
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env \
  exec -T postgres psql -U assistant -d assistant -Atc \
  "select relname||'='||n_live_tup from pg_stat_user_tables order by relname"
```

Проверено локально 2026-09-07: восстановление в чистую БД проходит без ошибок, число строк совпадает, полнотекстовый поиск по восстановленным фактам работает (генерируемые колонки пересчитываются сами), RLS в восстановленной БД действует — роль `app` с `SET LOCAL app.user_id` видит только свои строки.

### 3.2 Настоящее восстановление

```bash
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env stop gateway worker
env BACKUP_PASSPHRASE=<...> COMPOSE_ENV_FILE=/etc/assistant/small-infra.env \
  infra/small/restore.sh /var/backups/assistant/<файл>.dump.enc assistant
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env start gateway worker
```

Восстановление идёт поверх пустой схемы. Если в БД уже есть данные — сначала пересоздать БД, иначе получите конфликты по первичным ключам.

Redis не восстанавливается никогда: там счётчики лимитов и дедупликация, они начинаются с нуля. Единственное последствие — сообщение, обработанное перед аварией, может быть отвечено повторно.

## 4. Что делать при OOM-kill

Симптом: контейнер внезапно рестартует, в `docker inspect` — `"OOMKilled": true`, в `dmesg` — строка от cgroup killer.

```bash
docker inspect -f '{{.Name}} oom={{.State.OOMKilled}} restarts={{.RestartCount}}' \
  $(docker ps -aq --filter "name=personal-assistant-small")
docker stats --no-stream
dmesg -T | tail -30
```

Это **штатный режим отказа**, а не поломка: `mem_limit` на каждом контейнере существует ровно затем, чтобы убили нарушителя, а не хостовый postgres.

Кто именно и почему:

- **worker** — почти всегда OCR. Пик даёт распознавание PDF или фото. Порядок действий: уменьшить `MAX_OCR_PAGES`, затем `OCR_RENDER_DPI` (150 → 120), затем `OCR_MAX_IMAGE_SIDE` (1600 → 1200). Все три — переменные в `small.env`, рестарт воркера. Если повторяется на обычных диалогах — поднять `mem_limit` воркера за счёт SearXNG (192m → 96m), это решение зафиксировано в §2 плана как один из трёх допустимых вариантов.
- **postgres** — тяжёлый запрос с большим `work_mem` × число соединений. Проверить `DATABASE_POOL_MAX_SIZE`: суммарно приложение не должно просить больше `max_connections = 20`.
- **redis** — переполнение `maxmemory 64mb` при `noeviction` выглядит иначе: не OOM-kill, а ошибки `OOM command not allowed` на записи, и очередь встаёт. Означает ключ без TTL. Найти: `redis-cli --scan | head -50`, затем `redis-cli ttl <ключ>`; ключ с `-1` — дефект, его надо чинить в коде, а не увеличивать лимит.

Диагностика памяти без Grafana:

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}'
free -h                      # swap в постоянном использовании = профиль не помещается
docker compose -f infra/compose.small.yml logs --tail 100 worker
```

## 5. Ежедневные операции

```bash
# статус
docker ps --format '{{.Names}}\t{{.Status}}'

# логи (структурный JSON)
docker compose -f infra/compose.small.yml logs -f --tail 100 worker

# рестарт одного сервиса
docker compose -f infra/compose.small.yml --env-file /etc/assistant/small-infra.env restart worker

# место на диске
df -h / && docker system df
```

Алерты в этом профиле — уведомления владельцу в Telegram при dead-letter (механизм `NotifyAdminCallback`, уже в коде). Внешний uptime-пинг ставить некуда: наружу ничего не смотрит. Признак живости — heartbeat gateway и healthcheck'и контейнеров.

## 6. Что ещё не проверено

Заполняется при первом развёртывании:

- [ ] стек поднимается на реальных 2 ГБ и переживает сутки без OOM (S13 — гейт профиля);
- [ ] `prepare-host.sh` пережил перезагрузку;
- [ ] gateway в polling с реальным токеном становится healthy, 409 не воспроизводится (S17);
- [ ] бэкап уезжает на внешнее хранилище и восстанавливается оттуда (S11, серверная часть);
- [ ] перенос данных с прода (S18).
