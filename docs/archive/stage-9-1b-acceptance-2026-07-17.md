# Приёмка этапа 9.1B — frontend и инфраструктурная упаковка Mini App

Дата: 2026-07-17
Статус: принят локально
Исполнитель: GPT-5.6 Terra
Прод-развёртывание: не выполнялось

## Объём принятой части

Пакет 9.1B завершил локальную реализацию платформенного каркаса Mini App:

- Vite + Preact + TypeScript frontend shell;
- Telegram WebApp adapter и API-клиент для auth/bootstrap;
- утверждённая коричнево-белая light/dark палитра и mobile safe-area layout;
- раздача SPA и content-hashed assets из FastAPI;
- multi-stage Docker image без Node toolchain в runtime;
- декларативные dev/prod Compose, Caddy, Prometheus, Infisical, Ansible и CI;
- локальная сборка и контейнерный health smoke без обращения к серверу.

Экраны календаря и финансов намеренно оставлены disabled placeholders: их реализация относится к 9.2 и 9.3.

## Изменённые области

- `miniapp/` — package/lockfile, Vite/TypeScript/ESLint/Vitest, Telegram adapter, API client, shell и стили;
- `webapp/app.py` — SPA fallback, immutable cache для assets и `no-cache` для index;
- `tests/test_webapp_app.py` — проверка SPA/API routing и cache policy;
- `Dockerfile` — Node build-stage и копирование готовой статики в Python runtime;
- `infra/docker-compose.dev.yml`, `infra/compose.prod.yml` — сервис `webapp` и healthcheck;
- `infra/prod/caddy/Caddyfile` — `/app` route, CSP и точечное снятие `X-Frame-Options`;
- `infra/prometheus/`, `infra/prod/prometheus/` — scrape `/app/metrics`;
- `infra/infisical/bootstrap.py` — identity `webapp` и имена необходимых секретов;
- `infra/ansible/` — шаблон `/etc/assistant/webapp.env` и Molecule assertion;
- `.github/workflows/` — frontend job, image smoke import и deploy health gate;
- `.gitignore` — локальный npm cache.

## Зафиксированные решения

- Сессионный токен существует только в памяти вкладки, persistent browser storage не используется.
- Production frontend не имеет dev-auth bypass и загружает только официальный Telegram script.
- Токены light theme точно совпадают с утверждёнными: фон `#F7F3ED`, карточка `#FFFCF8`, основной коричневый `#6B5142` и остальные значения из архитектурного документа.
- Telegram `colorScheme` выбирает light/dark mode, но произвольные `themeParams` не перезаписывают палитру и не могут разрушить контраст.
- `/app/api/*` никогда не подменяется SPA; index не получает долгий cache, hashed assets получают immutable cache на год.
- Dev webapp включается профилем `webapp`, чтобы существующий `make dev-up` не начал требовать ещё не созданную локальную machine identity.
- Prod-конфигурация подготовлена, но identity/secret values должны быть созданы отдельным activation-шагом перед первым deploy.

## Автоматические проверки

- frontend ESLint — успешно;
- TypeScript typecheck — успешно;
- Vitest — 2 passed;
- Vite production build — успешно (`16.83 kB` JS, `1.99 kB` CSS до gzip);
- `uv run ruff check .` — успешно;
- `uv run ruff format --check .` — 146 файлов отформатированы;
- `uv run mypy` — успешно, 73 исходных файла;
- полный `pytest` с доступным Windows basetemp — 300 passed, 31 skipped;
- dev Compose с профилем webapp — `config -q` успешно;
- prod Compose с placeholder-переменными — `config -q` успешно;
- Caddy `validate` в официальном локальном контейнере — valid configuration;
- `git diff --check` — чисто.

Полный Gitleaks по 163 коммитам был чист на приёмке 9.1A непосредственно перед этим пакетом. Повторное монтирование всего приватного репозитория в контейнер было отклонено управляемой политикой безопасности; вместо него новые области 9.1B проверены локальным pattern scan, совпадений с форматами токенов и приватных ключей нет. Реальные значения секретов не добавлялись.

## Локальный container smoke

Основной Dockerfile собран в образ `personal-assistant:webapp-stage9`. Проверено:

- Node binary отсутствует в runtime;
- `core`, `gateway`, `worker`, `tools`, `webapp` импортируются;
- `webapp/static/index.html` и hashed JS/CSS assets присутствуют;
- временный webapp-контейнер подключился к локальным PostgreSQL/PgBouncer и Redis;
- `GET /app/healthz` вернул HTTP 200;
- временный контейнер удалён, локальные инфраструктурные сервисы остановлены с сохранением данных.

## Дефекты, найденные при приёмке

- Два похожих, но неточных light-theme цвета заменены точными утверждёнными токенами.
- Удалено прямое применение Telegram `themeParams`, которое могло сделать интерфейс слишком ярким или нарушить контраст.
- Vitest на Windows переведён в однопроцессный forks-режим после зависания дефолтного worker pool.
- Первый полный pytest дал только ошибку доступа sandbox к системному `%TEMP%`; повтор с доступным `--basetemp` прошёл полностью.
- Первые Docker build clients были остановлены после потери вывода. Контрольный build из основного процесса показал реальную причину долгого ожидания — export/unpack Windows Docker Desktop — и завершился успешно.

## Ограничения и следующий шаг

Не выполнялись push, CI на GitHub, Ansible apply, Infisical API, BotFather, изменение Telegram menu/webhook или deploy на VPS. Живой запуск внутри Telegram Desktop/mobile и проверка заголовков в реальном WebView остаются activation gate после реализации 9.2–9.4 и отдельной команды владельца на деплой.

Следующая часть: 9.2 — общая доменная логика напоминаний, calendar API и frontend-календарь с RLS/DST/cron-регрессией.
