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
