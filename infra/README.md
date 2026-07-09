# Dev-инфраструктура

Локальное окружение поднимается командой:

```bash
make dev-up
```

Состав сервисов:

| Сервис | Порт на хосте | Назначение |
| --- | ---: | --- |
| PostgreSQL 16 + pgvector | 5432 | Основная БД приложения |
| PgBouncer | 6432 | Пул подключений к основной БД в transaction mode |
| redis-queue | 6379 | Очереди Redis Streams, AOF включен |
| redis-cache | 6380 | Кэш, allkeys-lru, без persistence |
| Infisical | 8880 | Локальный vault UI |
| Infisical Postgres | не опубликован | Отдельная БД Infisical |
| Infisical Redis | не опубликован | Отдельный Redis Infisical |

Dev-учетные данные основной БД:

```text
user: assistant
password: dev-local-only
database: assistant
```

Проверка подключения через PgBouncer:

```bash
psql "postgresql://assistant:dev-local-only@localhost:6432/assistant"
```

Проверка Redis:

```bash
redis-cli -p 6379 PING
redis-cli -p 6380 PING
```

Infisical UI доступен по адресу: http://localhost:8880

## Infisical bootstrap

Локальный Infisical инициализируется dev-only скриптом:

```bash
make infisical-bootstrap
```

Скрипт создает начального администратора, проект `personal-assistant`, окружение `dev`,
machine identities `gateway`, `worker`, `scheduler`, выдает им read-доступ к секретам
и проверяет Universal Auth login + чтение секретов.

Входные dev-секреты можно положить вне репозитория в
`C:\Users\Artem\.assistant\bootstrap.env`: `TELEGRAM_BOT_TOKEN_PROD`,
`TELEGRAM_BOT_TOKEN_TEST`, `OPENROUTER_API_KEY`. Если файла нет, эти значения
просто пропускаются. `OAUTH_KEK` генерируется автоматически и хранится в Infisical.

Учетные данные администратора и client ID/secret machine identities записываются только в
`C:\Users\Artem\.assistant\infisical-dev.env`. Это локальные dev-only секреты: не
копируйте их в репозиторий, issue, логи или документацию.

## Роли БД

Основная БД использует три dev-роли приложения:

- `migrator` — владелец таблиц и роль для Alembic;
- `app` — рабочая роль приложения, не владелец таблиц, RLS применяется;
- `service` — роль с `BYPASSRLS` только для процессов поперёк пользователей.

При создании свежего Docker volume роли применяются автоматически из
`infra/postgres/init-roles.sql`, потому что файл смонтирован в
`/docker-entrypoint-initdb.d/10-init-roles.sql`.

Если volume уже был инициализирован раньше, init-скрипты Postgres повторно не запускаются.
В этом случае примените роли вручную:

```bash
make db-roles
```

Интеграционные тесты также выполняют этот idempotent-скрипт перед проверками, чтобы уже
существующий локальный volume не требовал пересоздания.

Остановить окружение без удаления данных:

```bash
make dev-down
```

Полный сброс с удалением Docker volumes:

```bash
make dev-destroy
```

## Миграции БД

PostgreSQL собирается из локального образа `infra/postgres/Dockerfile`: он основан на
`pgvector/pgvector:pg16` и дополнительно устанавливает `postgresql-16-partman`, потому
что миграция v1 использует `pg_partman` для месячных партиций `tool_calls_log` и `usage`.
После смены образа для старого dev-volume выполните `make dev-destroy`, затем `make dev-up`.

Миграции запускаются ролью `migrator` напрямую в PostgreSQL на `:5432`, не через PgBouncer:

```bash
make migrate
```

URL можно переопределить через `MIGRATIONS_DATABASE_URL`; значение по умолчанию —
`postgresql+psycopg://migrator:dev-local-only@127.0.0.1:5432/assistant`.

## Локальные бэкапы и restore-to-dev

Основная БД использует pgBackRest со stanza `assistant`. Репозиторий хранится в Docker
volume `pgbackrest_repo`, WAL архивируется через `archive_mode=on` и
`archive_command='pgbackrest --stanza=assistant archive-push %p'`. Внутри volume есть два
локальных repo: `repo1` для ежедневных full-бэкапов и `repo2` для еженедельных full-бэкапов.
Оба repo зашифрованы `aes-256-cbc`; passphrase берется из `PGBACKREST_REPO1_CIPHER_PASS`
и `PGBACKREST_REPO2_CIPHER_PASS`, а для dev есть явные небезопасные defaults
`dev-only-pgbackrest-cipher-passphrase` и
`dev-only-pgbackrest-weekly-cipher-passphrase`. Не используйте их в production.

Retention в dev-конфиге: для daily repo `repo1-retention-full=7`,
`repo1-retention-full-type=count`; для weekly repo `repo2-retention-full=4`,
`repo2-retention-full-type=count`. WAL retention в обоих repo привязан к full-бэкапам
через `repoN-retention-archive-type=full`. Это локальный механизм для stage 1A;
S3/offsite настройки относятся к stage 1B.

Снять full-бэкап основной БД:

```bash
make backup
```

Команда создает stanza, если его еще нет, затем запускает full backup внутри контейнера
`postgres` в daily repo (`repo1`). Еженедельный full-бэкап в repo с retention 4 запускается
отдельно:

```bash
make backup-weekly
```

Проверить WAL archive и состояние репозитория:

```bash
make backup-check
```

Восстановить последний pgBackRest-бэкап в отдельный одноразовый Postgres:

```bash
make restore-to-dev
```

Скрипт `infra/restore-to-dev.sh` очищает только отдельный volume `postgres_restore_data`,
восстанавливает туда последний backup и поднимает сервис `postgres-restore` из compose
profile `restore` на `localhost:5433`. Основной volume `postgres_data` этим скриптом не
монтируется на запись и не изменяется.

После восстановления скрипт выполняет sanity query: считает таблицы в `public`, а если
таблица `users` существует, считает строки в ней. Для проверки конкретной marker-строки
можно передать Telegram user id:

```bash
RESTORE_MARKER_TG_USER_ID=900000001 make restore-to-dev
```

На Windows запускайте restore через Git Bash или другую bash-совместимую оболочку.
Make target вызывает `bash infra/restore-to-dev.sh`, поэтому `bash` должен быть доступен
в `PATH`. Восстановленный контейнер остается запущенным на `:5433`, чтобы можно было
проверить данные вручную:

```bash
psql "postgresql://assistant:dev-local-only@localhost:5433/assistant"
```

Останавливать restore-контейнер только адресно:

```bash
docker compose -f infra/docker-compose.dev.yml stop postgres-restore
docker compose -f infra/docker-compose.dev.yml rm -f postgres-restore
```

Внимание: `docker compose --profile restore down` останавливает ВЕСЬ dev-стек
(профиль лишь добавляет сервис к списку), не используйте его для остановки
одного restore-контейнера.

Дамп БД Infisical сохраняется отдельно в Docker volume `infisical_backups`:

```bash
make backup-infisical
```

Файл создается внутри контейнера `infisical-db` в `/backups` в формате custom pg_dump:
`infisical-YYYYMMDDTHHMMSSZ.dump`.
