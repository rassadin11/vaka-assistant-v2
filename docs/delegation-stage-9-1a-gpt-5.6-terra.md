# Делегирование GPT-5.6 Terra: этап 9.1A

Ты работаешь в репозитории `D:\claude-projects\personal-assistant` и реализуешь
только backend-фундамент Telegram Mini App, батч 9.1A. Пиши код и тесты прямо в
рабочем дереве. Не делай commit и не запускай deploy.

## Обязательное чтение до изменений

1. `AGENTS.md` целиком.
2. Блок «Принятые архитектурные решения» в `implementation_plan_v1.md`.
3. `plan/stage-9.md` целиком.
4. `plan/stage-9-architecture.md` целиком.
5. `plan/stage-9-handoff.md`, особенно батч A.
6. `plan/db-schema.md` и `plan/progress.md`.

Плановые документы уже изменены владельцем/оркестратором и находятся в dirty
worktree. Не редактируй и не удаляй `plan/**`, `docs/**`, `AGENTS.md`, `.agents/**`,
`.codex/**`, `implementation_plan_v1.md` и `tools_registry_v1.md`. Сохрани любые
чужие изменения. Если спецификация противоречива или для задачи нужна новая
таблица/колонка, остановись и сообщи, не принимай решение молча.

## Цель батча

Создать тестируемый FastAPI backend `webapp/` с Telegram initData auth,
короткоживущей сессией, app-role/RLS проверкой, per-user rate limit, trace_id,
метриками, healthcheck и миграцией безопасного auth resolver. Frontend,
Docker/compose/Caddy/Ansible, кнопки бота, экранные API и deployment в этот батч не
входят.

## Обязательная реализация

### 1. Миграция auth resolver

Добавь обратно совместимую Alembic migration функции
`public.webapp_resolve_user(bigint)` строго по `plan/db-schema.md`:

- `SECURITY DEFINER`, `STABLE`, фиксированный безопасный `search_path`;
- точный SELECT `users.id, users.status` по tg_user_id;
- owner `migrator`;
- `REVOKE ALL ... FROM PUBLIC`;
- `GRANT EXECUTE ... TO app`;
- downgrade удаляет только функцию.

Никаких новых таблиц/колонок/ролей. Webapp никогда не получает service pool.

### 2. Python package и settings

Добавь `webapp` в project packages/mypy и основной import surface. Добавь ровно
одну session dependency: `itsdangerous` (не PyJWT). Обнови `uv.lock` штатно.

Создай package примерно такого назначения (точную разбивку можно улучшить):

- `webapp/__main__.py` — serve entrypoint;
- `webapp/app.py` — factory/lifespan/routes;
- `webapp/settings.py` — typed env settings без секретов в repr/log;
- `webapp/auth.py` — чистые initData/session функции;
- `webapp/dependencies.py` — bearer/request user/RLS guard;
- `webapp/errors.py`, `webapp/metrics.py` — единые ошибки и метрики.

Используй существующие `core.db`, `core.tracing`, logging и Prometheus patterns.
Архитектура должна позволять dependency injection fake pool/Redis/clock в tests.

### 3. Telegram initData

`POST /app/api/auth` принимает JSON `{ "init_data": "..." }` либо другое одно
чётко типизированное поле; выбери snake_case и закрепи тестами. Ограничь размер
request body/строки на уровне приложения.

Строго реализуй алгоритм из архитектуры: query parsing, отсутствие дублей
обязательных полей, data-check-string без hash, HMAC-SHA256, constant-time
compare, auth_date не старше 1 часа и не из будущего кроме малого clock skew,
валидный user JSON и integer id. Никогда не логируй initData/hash/user payload.

После HMAC вызови только `webapp_resolve_user` через app pool. Это единственный
запрос до `user_transaction`. Unknown и non-active -> 403; invalid/expired -> 401.

### 4. Session

Используй `itsdangerous.URLSafeTimedSerializer`, salt
`telegram-webapp-session-v1`, TTL 12 часов, payload только `{v: 1, sub: uuid}`.
Ключ `WEBAPP_SESSION_SECRET` обязателен и не совпадает логически с bot token.
Ошибки подписи/версии/TTL -> 401.

### 5. Protected request and RLS smoke

Добавь `GET /app/api/me`: bearer auth -> `user_transaction(sub)` -> SELECT текущей
строки users -> повторная проверка status=active -> JSON только
`{timezone, plan}`. Не возвращай UUID, tg_user_id или Telegram profile. Unknown
под RLS/non-active -> 403.

Организуй dependency/request context так, чтобы будущие domain handlers могли
использовать один открытый connection и актуальные timezone/plan без вложенных
pool acquire.

### 6. Rate limit

Применяй к защищённым API отдельный Redis token bucket: key
`rl:webapp:{user_id}`, 60/min, burst 20. Можно безопасно обобщить существующий
`core/rate_limit.py`, но публичное поведение gateway, его key и тесты должны
остаться байт-в-байт совместимыми. Redis failure обрабатывай явно и в соответствии
с существующим стилем проекта; не используй process-local state.

### 7. HTTP, trace и metrics

- `/app/healthz` без auth;
- `/app/metrics` Prometheus exposition;
- trace_id на каждый request, `X-Trace-Id` response header;
- error envelope `{error:{code,message,trace_id}}`;
- API unknown route возвращает JSON 404, не HTML;
- нормализованные metric labels, без user_id/raw path/error text;
- auth failures по безопасной причине;
- structured logs без Authorization/initData/описаний данных.

Не добавляй CORS/cookies/dev auth bypass/static SPA.

## Инварианты проекта

- Python 3.12, strict mypy, English code/identifiers/docstrings, Russian user text.
- RLS только `SET LOCAL app.user_id` через `user_transaction`; session-level SET
  запрещён из-за PgBouncer transaction mode.
- State только Postgres/Redis; webapp stateless.
- Секреты только из env/Infisical contract, никаких `.env` и значений в repo.
- Тесты не ходят в сеть и не тратят токены.
- Не меняй LLMProvider/contracts, tools или gateway без необходимости rate-limit
  refactor.
- Не запускай команды prod deploy, Ansible, BotFather, prod Infisical, SSH или git
  push.

## Тесты и DoD батча

Добавь unit tests минимум для:

- initData valid/tampered/missing hash/user/auth_date/duplicate/malformed JSON;
- expired/future auth_date и constant clock injection;
- session valid/tampered/expired/wrong version;
- auth active/unknown/non-active;
- protected endpoint missing/invalid token, current status/timezone/plan;
- error envelope/trace header/JSON 404;
- webapp key namespace и 429.

Добавь integration tests миграции:

- app может execute resolver;
- PUBLIC не имеет EXECUTE;
- resolver возвращает только нужного пользователя/status;
- обычные user tables без SET LOCAL остаются fail-closed;
- `/me`/эквивалентный DB path изолирует двух пользователей.

Запусти и доведи до зелёного:

- `uv run ruff check .`;
- `uv run ruff format --check .`;
- `uv run mypy`;
- релевантные tests, затем полный `uv run pytest -v` (integration могут skip без
  поднятого окружения).

Не отмечай `progress.md`: окончательную приёмку и отметку делает оркестратор.

## Финальный отчёт

Верни:

1. список изменённых файлов;
2. краткие архитектурные решения;
3. выполненные команды и результаты;
4. известные ограничения/то, что сознательно оставлено батчу 9.1B;
5. любые сомнения, которые должен проверить оркестратор.
