# Runbook — что делать при инцидентах

Для оператора сервиса (сегодня — владелец). Формат каждого сценария: симптом/алерт → диагностика → действия → проверка. Все команды рабочие для dev-стека (Windows, Git Bash, из корня репозитория); prod-варианты (systemd, Ansible, боевые хосты) — в [runbook-deploy.md](runbook-deploy.md) (карта прода, деплой/откат, секреты, диагностика на сервере); места, где команда отличается на проде, помечены **[prod: 1B]**.

## 0. Где смотреть

- **Grafana** — http://localhost:3000 (дашборды «Технический», «Продуктовый»; алерты — Alerting → Alert rules). Анонимный вход Viewer; админ-операции — под admin.
- **Prometheus** — http://localhost:9090 (сырые метрики, Status → Targets).
- **Алерты в Telegram** — приходят администратору, только если Grafana запущена с `TELEGRAM_ALERT_BOT_TOKEN`/`TELEGRAM_ALERT_CHAT_ID` (в dev по умолчанию выключено; см. infra/README.md). Плюс отдельные алерты circuit breaker'а LLM (3.5) и DLQ (2.5) — их шлёт сам воркер.
- **Логи** — gateway/worker/шедулер пишут JSON в stdout (`ts`, `level`, `service`, `trace_id`, `message`). Контейнеры стека: `docker logs personal-assistant-dev-<svc>-1`. Каждая задача пользователя связана единым `trace_id` — поиск инцидента начинать с него (раздел 7).
- Очередь — Redis Streams в **redis-queue** (host-порт 6379): `q:interactive:{0..15}`, `q:background:{0..15}`, DLQ — `q:dlq`; consumer-группы `g:interactive`/`g:background`. Кэш/лимиты — **redis-cache** (host-порт 6380).

`redis-cli` на машине не установлен — ходить через контейнер:

```bash
docker exec personal-assistant-dev-redis-queue-1 redis-cli XLEN q:interactive:0
```

## 1. Падение воркеров — алерт `TargetDown` (instance host.docker.internal:9100)

Симптомы: алерт TargetDown по воркеру; сообщения пользователям не отвечаются; `queue_depth` растёт.

Диагностика:

```bash
# жив ли процесс воркера (dev: запускается на хосте)
tasklist //FI "IMAGENAME eq python.exe" | head          # [prod: 1B — systemctl status worker]
# отдаёт ли метрики
curl -s http://127.0.0.1:9100/metrics | head -3
# последние ошибки в логе воркера (JSON, level=ERROR)
```

Действия:

1. Перезапустить воркер: `uv run python -m worker` (env как при обычном запуске dev). **[prod: 1B — systemd restart]**
2. Задачи НЕ теряются: доставка at-least-once — незаACKнутые записи переигрываются reclaim'ом (idle > 210 с), после 3 неудачных доставок уходят в DLQ с уведомлением админу.

Проверка: алерт перешёл в resolved; очередь разгребается:

```bash
docker exec personal-assistant-dev-redis-queue-1 redis-cli XPENDING q:interactive:0 g:interactive
```

`pending` по партициям падает до 0; тестовое сообщение боту получает ответ.

## 2. Переполнение очереди — алерт `InteractiveQueueBacklog` (> 100 за 5 мин)

Диагностика — где скопилось и почему:

```bash
# реальный бэклог по партициям: в выводе XINFO GROUPS смотреть lag (не доставлено) + pending (не заACKнуто)
for i in $(seq 0 15); do docker exec personal-assistant-dev-redis-queue-1 redis-cli XINFO GROUPS q:interactive:$i; done
# зависшие в обработке (не заACKнутые)
docker exec personal-assistant-dev-redis-queue-1 redis-cli XPENDING q:interactive:0 g:interactive
# XLEN — НЕ бэклог: записи после ACK не удаляются, это счётчик всех сообщений за историю (до trim 100k)
```

Причины и действия:

- **Воркеры лежат** → сценарий 1.
- **Воркеры живы, но всё в pending** — зависли на локах/LLM: смотреть логи по trace_id зависших задач; reclaim сам переиграет через 210 с; при массовом зависании — перезапуск воркеров.
- **Наплыв** — поднять ещё один-два воркера (`uv run python -m worker` в отдельных терминалах; партиции разберут сами). Воркер в режиме приоритета обрабатывает interactive прежде background (2.6) — фоновые подождут.

DLQ (`q:dlq`) — разобрать после стабилизации:

```bash
docker exec personal-assistant-dev-redis-queue-1 redis-cli XRANGE q:dlq - + COUNT 10
```

По каждой записи: смотреть `error`/`trace_id`, причину чинить; переигровка — отправить пользователю просьбу повторить запрос (автоматической переигровки из DLQ нет, ключ идемпотентности защитит от дублей side-effect'ов); мусорные записи списывать `XDEL q:dlq <id>`.

Проверка: `sum(queue_depth{queue="interactive"})` в Prometheus вернулась к ~0, алерт resolved.

## 3. Недоступность LLM API — алерт `LLM429RatioHigh`, Telegram-алерты circuit breaker

Что происходит автоматически (3.5): retry ×3 с джиттером на 429/5xx; после ≥3 подряд ошибок по модели — circuit breaker, переключение на fallback-модель на 300 с + однократный Telegram-алерт админу; семафор ограничивает одновременные запросы (8).

Вмешиваться, когда: алерты повторяются дольше ~15 минут, или лежат и primary и fallback (пользователи получают отказ агентного цикла).

Диагностика:

```bash
# статус OpenRouter руками: валиден ли ключ, есть ли кредит
curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"
# состояние breaker'а и семафора
docker exec personal-assistant-dev-redis-cache-1 redis-cli KEYS "cb:openrouter:*"
docker exec personal-assistant-dev-redis-cache-1 redis-cli SCARD sem:openrouter
```

Действия:

- Ключ протух/кредит кончился → пополнить/перевыпустить в кабинете OpenRouter, обновить секрет в Infisical, перезапустить воркеры.
- Провайдер лежит (инцидент на стороне OpenRouter/allowlist-провайдера) → ждать, breaker сам держит fallback; при долгом инциденте можно временно сменить allowlist/модель в конфиге.
- Подозрение на утечку слотов семафора (SCARD близок к 8 при нулевом трафике) → у элементов TTL 180 с, само рассосётся; форс-очистка: `DEL sem:openrouter` (безопасно — пересоздастся).

Проверка: живой запрос боту отвечает; доля 429 в Grafana падает; `cb:openrouter:*` исчезли.

## 4. Недоступность сервисов — алерты `ServiceProbeFailed` / `EmbeddingsProbeFailed`

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E "searxng|postgres|embeddings"
docker restart personal-assistant-dev-searxng-1        # или другой упавший
docker logs personal-assistant-dev-searxng-1 --since 10m | tail -20
```

Особенности деградации (сервисы падают «мягко», задачи пользователей не рушатся):

- **searxng** — web_search возвращает понятный отказ, остальное работает. Не критично, чинить в рабочем порядке.
- **embeddings** (severity warning) — профиль `ml`: в dev контейнер может быть просто не запущен (`docker compose -f infra/docker-compose.dev.yml --profile ml up -d embeddings`). Без него не работают память (remember_fact/автоинжект) и поиск по документам — задачи продолжают отвечать без них.
- **webapp** (Mini App) — бот работает без него; из Telegram не открываются экраны «Календарь»/«Финансы». Проверка: `/app/healthz` (прод: `https://vaka-assistant.ru/app/healthz`, ожидается 200); контейнер `webapp` в стеке — `docker restart` при зависании; ошибки — в JSON-логах контейнера по `trace_id`. Прод-диагностика — runbook-deploy.md.
- **postgres** — критично: не работает ничего (диалоги, RLS, инструменты, Mini App). Если контейнер не поднимается или данные повреждены — сценарий 5.
- Если весь стек лёг (перезагрузка машины, рестарт Docker Desktop): `make dev-up` поднимает всё; зависшие контейнеры в статусе Created — `docker rm` и повторить.

## 5. Восстановление из бэкапа

Бэкапы (1.8): pgBackRest — repo1 (retention 7 дней) и repo2 (4 недели), оба шифрованные, + непрерывный WAL; RDB-снапшоты redis-queue; дамп Infisical.

```bash
make backup           # полный бэкап в repo1 (ежедневный)
make backup-weekly    # полный бэкап в repo2 (еженедельный)
make backup-check     # health-check стенза + список бэкапов
make backup-infisical # дамп БД Infisical
```

Репетиция восстановления (обязательная ежемесячная, 6.6) — восстанавливает последний бэкап в ОТДЕЛЬНЫЙ контейнер и проверяет маркер:

```bash
make restore-to-dev   # в конце должно напечатать PASS
```

Реальное восстановление основной БД (данные повреждены/удалены): остановить gateway/воркеры/шедулер → снять свежий бэкап-до-инцидента, если БД ещё жива (`make backup`) → восстановить из pgBackRest поверх основного кластера (процедура — по мотивам `infra/restore-to-dev.sh`, точечный restore с `--set`/PITR по времени) → поднять стек, прогнать `make backup-check`, живой запрос боту. **[prod: 1B — отработать и вписать точную команду поверх боевого кластера]**

⚠️ Ловушка: `docker compose --profile restore down` гасит ВЕСЬ dev-стек, не только restore-контейнер (infra/README.md).

Redis-queue после потери: очередь допустимо потерять (пользователь повторит запрос), RDB-снапшот вернёт последнее сохранённое состояние; redis-cache восстанавливать не нужно (кэш и счётчики с TTL, fail-open).

## 6. Общий сбор контекста при инциденте

1. Взять `trace_id`: из жалобы пользователя (время + tg_user_id → найти в логах gateway) или из алерта/DLQ-записи.
2. Логи всех сервисов фильтровать по нему — вся цепочка gateway → очередь → воркер → LLM/инструменты связана одним значением.
3. В БД смотреть под service-ролью: `messages` (диалог), `tool_calls_log` (вызовы инструментов и их результаты), `usage` (стоимость задачи).
4. Итог инцидента фиксировать в docs/dev-log.md (что случилось, причина, что изменили).
