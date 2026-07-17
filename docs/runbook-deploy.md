# Runbook: деплой и эксплуатация прода

Собрано по факту живых приёмок 1.11–1.12 (2026-07-16). Обновлять при каждом изменении процедур.

## Карта прода

| Что | Значение |
|-----|----------|
| Сервер | 31.76.15.130 (Ubuntu 24.04), SSH-пользователь `deploy` (ключевой доступ, пароли выключены) |
| Домен | `vaka-assistant.ru` → Caddy (TLS Let's Encrypt), наружу только 80/443 |
| Приложение | `/opt/assistant` (compose.prod.yml, конфиги), systemd-юнит `assistant.service` (enabled, поднимает стек после reboot) |
| Конфиги/секреты | `/etc/assistant/`: `infra.env` (интерполяция compose, симлинк `/opt/assistant/.env`), `bootstrap.env` (identity Infisical для app), `deploy.env` (пин образа `APP_IMAGE=`), `deploy.env.prev` (для отката), `migrator.env` (DSN миграций), `prod-seed.env` (источник досева, root 0600) |
| Образы | GHCR `ghcr.io/rassadin11/vaka-assistant-v2` (+`-caddy`, `-searxng`); собирает workflow **Images** на push в main; теги `latest` и `sha-<full_sha>`; pull анонимный |
| Стек | 17 контейнеров: gateway, worker, postgres (+pgvector), pgbouncer, redis-queue, redis-cache, infisical (+его postgres+redis), caddy, searxng, embeddings (TEI), prometheus, grafana, blackbox, node-exporter, cadvisor |
| Секреты приложения | Infisical (в стеке, `http://infisical:8080`), проект prod; контейнеры читают через `core/secrets_entrypoint` при старте |
| Боты | **@vAssistantv2testbot** — ассистент (webhook, бета); **@vAssistantv2workbot** — канал сводок/алертов владельцу (chat_id 587963165), webhook НЕ ставить (сломает getUpdates) |
| Машина владельца | ключи API в `C:/Users/Artem/.assistant/bootstrap.env`; SSH-ключ CI-деплоя `C:/Users/Artem/.assistant/deploy-ci-key` (pub уже в authorized_keys deploy) |
| Модель | `deepseek/deepseek-chat` (OpenRouter, data_collection=deny) → prompt v1; v2-flash включится при переходе на `deepseek/deepseek-v4-flash` |

## Штатный деплой новой версии

GitHub → Actions → **Deploy** → Run workflow → `image_tag` = `latest` или `sha-<full_sha>` (пин).
Workflow сам: валидирует тег → сохраняет текущий `deploy.env` в `.prev` → пишет новый пин → `docker compose pull` → миграции (`alembic upgrade head` одноразовым контейнером под ролью migrator) → `systemctl restart assistant` → ждёт healthy gateway+caddy до 5 минут.
Секреты workflow: `DEPLOY_HOST`, `DEPLOY_SSH_KEY` (добавлены владельцем).

**Откат**: перезапустить Deploy с тегом из `/etc/assistant/deploy.env.prev` (посмотреть: `ssh deploy@31.76.15.130 sudo cat /etc/assistant/deploy.env.prev`). Миграции вперёд-совместимы (expand-contract), откат кода без отката БД.

## Ручной деплой с сервера (если Actions недоступен)

```bash
ssh deploy@31.76.15.130
sudo cp /etc/assistant/deploy.env /etc/assistant/deploy.env.prev
echo 'APP_IMAGE=ghcr.io/rassadin11/vaka-assistant-v2:<TAG>' | sudo tee /etc/assistant/deploy.env
cd /opt/assistant
docker compose -f compose.prod.yml --env-file .env --env-file /etc/assistant/deploy.env pull
docker run --rm --network personal-assistant-prod_default \
  --env-file /etc/assistant/migrator.env \
  ghcr.io/rassadin11/vaka-assistant-v2:<TAG> \
  sh -ec 'export MIGRATIONS_DATABASE_URL=${MIGRATOR_DATABASE_URL:?}; exec alembic upgrade head'
sudo systemctl restart assistant
docker ps --format '{{.Names}}\t{{.Status}}'   # все должны стать healthy (embeddings — до 10 мин при первом старте)
```

Грабля: DSN в `migrator.env` обязан быть со схемой `postgresql+psycopg://` (psycopg v3; с `postgresql://` SQLAlchemy требует psycopg2, которого нет в образе).

## Изменения инфраструктуры (compose, конфиги, юнит)

Правки только через Ansible-роль в репо (`infra/ansible/roles/app`), катить с машины владельца:

```bash
docker build -q -t local/ansible-runner - <<'EOF'
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq openssh-client >/dev/null && pip install -q ansible-core
EOF
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "D:\claude-projects\personal-assistant:/work" -v "C:\Users\Artem\.ssh:/keys:ro" \
  -w /work/infra/ansible -e ANSIBLE_HOST_KEY_CHECKING=False local/ansible-runner \
  bash -c "mkdir -p /root/.ssh && cp /keys/id_* /root/.ssh/ && chmod 600 /root/.ssh/id_* ; \
    ansible-galaxy collection install -r requirements.yml >/dev/null; \
    ansible-playbook -i inventory/prod.yml site.yml -e prod_server_ip=31.76.15.130 --tags app"
```

Прогонять дважды: второй прогон должен дать `changed=0` (идемпотентность). Роль НЕ трогает содержимое `deploy.env` и секретные env-файлы (`force: no`) — только раскладывает шаблоны при отсутствии.

## Секреты (добавить/обновить в Infisical)

1. Дописать `KEY=value` в `/etc/assistant/prod-seed.env` на сервере (передавать значения только через SSH stdin, не через чат/коммиты).
2. Досеять (идемпотентно; ключи перечислять ВСЕ нужные, не только новые):

```bash
ssh deploy@31.76.15.130
git clone -q --depth 1 https://github.com/rassadin11/vaka-assistant-v2 /tmp/repo-b
sudo docker run --rm --network personal-assistant-prod_default \
  -v /tmp/repo-b:/repo:ro -v /etc/assistant:/etc/assistant \
  -e INFISICAL_URL=http://infisical:8080 -e BOOTSTRAP_ADMIN_EMAIL=admin@vaka-assistant.ru \
  -e BOOTSTRAP_ORG_NAME='Personal Assistant' -e BOOTSTRAP_ENV_NAME=Production -e BOOTSTRAP_ENV_SLUG=prod \
  -e BOOTSTRAP_INPUT_ENV_PATH=/etc/assistant/prod-seed.env \
  -e BOOTSTRAP_OUTPUT_ENV_PATH=/etc/assistant/infisical-prod.env \
  -e BOOTSTRAP_SEED_KEYS=DATABASE_URL,SERVICE_DATABASE_URL,WEBHOOK_SECRET_PATH,TELEGRAM_WEBHOOK_SECRET_TOKEN,PUBLIC_URL,TELEGRAM_BOT_TOKEN,OPENROUTER_API_KEY,GROQ_API_KEY,ADMIN_TELEGRAM_IDS \
  -w /repo ghcr.io/rassadin11/vaka-assistant-v2:latest python infra/infisical/bootstrap.py
rm -rf /tmp/repo-b
sudo systemctl restart assistant   # контейнеры перечитывают секреты только при старте
```

## Webhook Telegram

Ставится один раз (и после смены токена/домена/секретного пути):

```bash
ssh deploy@31.76.15.130 sudo docker run --rm --network personal-assistant-prod_default \
  --env-file /etc/assistant/bootstrap.env ghcr.io/rassadin11/vaka-assistant-v2:latest \
  python -m core.secrets_entrypoint -- python -m gateway set-webhook
```

Проверка: `getWebhookInfo` токеном бота — url `https://vaka-assistant.ru/webhook/…`, `last_error_message` пуст.

## Диагностика

```bash
ssh deploy@31.76.15.130
systemctl status assistant
docker ps --format '{{.Names}}\t{{.Status}}'                  # здоровье стека
docker logs --since 30m personal-assistant-prod-gateway-1     # приём webhook (POST /webhook/… 200)
docker logs --since 30m personal-assistant-prod-worker-1      # обработка (JSON-логи с trace_id)
docker exec personal-assistant-prod-postgres-1 psql -U assistant -d assistant \
  -c "select tool_name, result_status, latency_ms, created_at from tool_calls_log order by id desc limit 10"
```

Grafana — `https://vaka-assistant.ru/grafana/` (алерты и так приходят в канал владельца).

## Известные грабли

- `migrator.env`: схема DSN `postgresql+psycopg://` (см. выше).
- Windows/Git Bash: перед docker-командами с абсолютными путями — `MSYS_NO_PATHCONV=1`.
- `embeddings` при первом старте на новой машине качает модель — healthy может стать через ~10 минут; healthy-гейт деплоя ждёт только gateway+caddy.
- Секретные env-файлы на сервере создаются ролью как пустые шаблоны и НЕ перезаписываются — значения живут только на сервере; при пересоздании сервера их нужно засеять заново (источник — машина владельца + генерация паролей на месте).
- Alembic требует `alembic.ini` в образе — он копируется в Dockerfile; при смене расположения миграций править и Dockerfile.
