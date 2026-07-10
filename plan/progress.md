# Прогресс реализации

Правила: отмечать пункт только после полной приёмки (тесты + e2e + gitleaks); в скобках — дата и ссылка на коммит/PR, если есть. Блокеры — в раздел внизу. Формулировки пунктов — короткие ярлыки, содержание всегда в plan/stage-N.md.

## Этап 0. Подготовка
- [x] 0.1 Документ требований (2026-07-09, plan/requirements.md)
- [x] 0.2 Репозиторий (monorepo, pre-commit, pytest) (2026-07-09, initial commit: uv, ruff, mypy strict, pytest, pre-commit hooks установлены, Dockerfile собирается)
- [x] 0.3 CI (2026-07-09, зелёный прогон: lint, test, gitleaks, docker build — github.com/rassadin11/vaka-assistant-v2, run 29022815257)
- [ ] 0.4 Окружения dev/prod
- [ ] 0.5 Регистрации (BotFather, OpenRouter, Google Cloud + заявка, домен, Tribute)
- [ ] 0.6 Privacy policy опубликована; черновик оферты

## Этап 1A. Локальная инфраструктура
- [x] 1.1 docker-compose dev (`make dev-up`) (2026-07-09, Codex + приёмка Claude; commit 68d0eb8)
- [x] 1.2 PgBouncer × asyncpg smoke-тест (2026-07-09, Codex + приёмка Claude; в CI, commit 357c310)
- [x] 1.3 Роли БД + хелпер SET LOCAL + RLS-тест (2026-07-09, core/db.py + init-roles.sql; RLS-шаблон уточнён в db-schema.md — fail-closed NULLIF; commit 357c310)
- [x] 1.4 Два Redis (2026-07-09, в составе 1.1: noeviction+AOF / allkeys-lru без персистентности, проверено config get)
- [x] 1.5 Infisical + machine identities (2026-07-09, идемпотентный bootstrap, 3 identity верифицированы, core/secrets.py; учётки вне репо; Codex + приёмка Claude; commit de161cc)
- [x] 1.6 Миграция v1 (13 таблиц по db-schema.md) (2026-07-09, Codex + opus-ревью против спеки + приёмка Claude; уточнения спеки: RLS-шаблон fail-closed, user_id NOT NULL; commit 7875092)
- [x] 1.7 Envelope-шифрование oauth_tokens (2026-07-09, core/crypto.py: AES-256-GCM, версионируемый KEK, key_version в AAD; Codex + приёмка Claude; commit 46d4360)
- [x] 1.8 Бэкапы + restore-to-dev.sh (2026-07-09, pgBackRest 2 шифрованных репо 7d/4w + WAL, restore в отдельный контейнер с маркерной проверкой PASS, RDB-снапшоты redis-queue, дамп Infisical; Codex + приёмка Claude; commit 3ab7d4f)

DoD 1A выполнен (2026-07-09): `make dev-up` с нуля до рабочего состояния; миграция v1 ролью migrator; RLS-тест и PgBouncer smoke зелёные (локально и в CI); restore-to-dev.sh восстанавливает бэкап с проверкой.

## Этап 1B. Боевой сервер (к закрытой бете)
- [ ] 1.9 Сервер + hardening
- [ ] 1.10 Ansible
- [ ] 1.11 Caddy
- [ ] 1.12 CI-деплой (GHCR → сервер)
- [ ] 1.13 Бэкапы на проде (S3 у другого провайдера)

## Этап 2. Гейтвей и очередь
- [x] 2.1 Gateway FastAPI (+фильтр приватных чатов) (2026-07-09, webhook+polling+set-webhook, healthz/metrics; Codex до лимита + Claude; commit dfcef17)
- [x] 2.2 Дедуп update_id (после enqueue) (2026-07-09, инвариант оформлен fault-injection-тестом)
- [x] 2.3 Redis Streams, envelope, 16 партиций (2026-07-09, core/envelope.py + core/queue.py, контракт pydantic v1)
- [x] 2.4 Per-user lock (2026-07-09, core/locks.py: NX PX 180s, Lua extend/release только владельцем; Codex + приёмка Claude; commit dfc7295)
- [x] 2.5 Redelivery + DLQ (2026-07-09, XAUTOCLAIM idle>210с, счётчик из XPENDING, DLQ после 3 доставок + уведомления; фикс Claude: reclaim обрабатывает все заклейменные записи; commit dfc7295)
- [x] 2.6 Приоритеты interactive/background (2026-07-09, non-blocking interactive → background block=2s, режим interactive-only; commit dfc7295)
- [x] 2.7 Rate limit per-user (2026-07-09, core/rate_limit.py: Lua token bucket 20/мин burst 5; одно предупреждение за окно fire-and-forget; уточнение 2.7 в stage-2.md — доставка через sender из 2.8; Codex + приёмка Claude; commit 84e3c23)
- [x] 2.8 Отправка ответов (лимиты Telegram) (2026-07-09, core/telegram_sender.py: ~30/с глобально + ~1/с на чат, retry по 429, нарезка 4096; подключено в gateway и worker; Codex + приёмка Claude; commit 84e3c23)
- [x] 2.9 Онбординг беты + welcome//help + таймзона (2026-07-09, worker/onboarding.py: статусы в воркере, админ-команды под service, кнопки городов + текстовый fallback, черновые тексты; попутно фикс 2.10 — обработка всего батча XREADGROUP (мультипартиционный дроп ловил e2e); Codex + приёмка Claude; commit ea44526)
- [x] 2.10 Каркас воркера (+typing, graceful shutdown) (2026-07-09, worker/: Processor+EchoProcessor, инжектируемые колбэки (Telegram — на 2.8), дедуп 2-я линия, reconnect backoff, trace_id-логи; e2e на живом Redis; Codex + приёмка Claude; commit dfc7295)
- [x] 2.11 Тесты контура (нагрузка, chaos, DLQ, деплой) (2026-07-09, tests/contour/: 4 сценария по плану; нагрузочный тест вскрыл дыру 2.4 — skip без ACK ломал per-user порядок; введён predecessor-guard + ожидание лока, механизм зафиксирован в stage-2.md; Codex + приёмка Claude с ужесточением скана до 128; commit 4cd909f)

DoD этапа 2 выполнен (2026-07-09): все тесты 2.11 пройдены на живом окружении (74 passed, включая контурные); порядок сообщений одного пользователя не нарушается при 4 воркерах — проверено нагрузочным тестом 100 msg/с.

## Этап 3. Агентное ядро
- [x] 3.1 LLMProvider + OpenRouterProvider (+allowlist) + Mock (2026-07-10, core/llm*.py: pydantic-контракты, data_collection deny + allow_fallbacks false + usage accounting в каждом запросе, cost Decimal, openai только в llm_openrouter; живой e2e-запрос с учётом стоимости; Codex + приёмка Claude; commit 71f1f77)
- [x] 3.2 Агентный цикл (+промежуточные сообщения) (2026-07-10, core/agent.py: лимиты 10 вызовов/120с/бюджет ₽, прогресс-сообщение; наследие этапа 2 закрыто — продление лока, DLQ по фактическим попыткам process(), TaskContext c users.id; тестовый инструмент get_current_time; живой e2e через очередь с DeepSeek; Codex + приёмка Claude; commit 4f09d55)
- [x] 3.3 Malformed tool calls (2026-07-10: спека дописана в stage-3.md перед кодом; MalformedToolCallError в диспетчере — неизвестное имя с перечнем доступных, битый/не-объект JSON, "" = "{}"; в цикле кумулятивный счётчик malformed-раундов, после 2 ретраев stop_reason="malformed" + фолбэк без лишнего вызова LLM; живой e2e — испорченный JSON, DeepSeek самоисправился; Codex + приёмка Claude; commits 536a059+1bd5f91)
- [x] 3.4 Контекст-менеджер + текст промпта + eval ≥18/20 (2026-07-10: спека дописана в stage-3.md перед кодом; core/tokens (tiktoken cl100k), core/context_manager (блоки A–F, бюджеты кодом, группировка ходов без висячих tool-сообщений), core/prompt (PROMPT_VERSION v1, ядро ~560 токенов из 1500), core/summarize; evals/ вне CI — живой eval 20 сценариев, две итерации промпта (устойчивость к инъекции в память, запрет имитации tool-вызовов текстом, «нет инструмента → честный отказ»), allowlist=novita (fp8) зафиксирован в плане, 20/20 дважды подряд; Codex + приёмка Claude; commits 94d60fd+8ee2520)
- [x] 3.5 Семафор + retry + fallback-модель (2026-07-10: спека дописана в stage-3.md перед кодом; core/llm_resilient.py — обёртка ResilientLLMProvider: Lua-семафор sem:openrouter (лимит 8, TTL 180с от утечек, слот не держится во время backoff-сна), retry ×3 с полным джиттером на 429/5xx, circuit breaker cb:openrouter:{model} (≥3 подряд → fallback на cooldown 300с, русский алерт админу с NX-дедупликацией); вшито в worker/__main__.py; живой e2e — ответ через обёртку, семафор освобождён; Codex + приёмка Claude; commits d66a3f7+e47ee91)
- [x] 3.6 Интерфейс роутера моделей (2026-07-10: спека дописана в stage-3.md перед кодом; core/model_router.py — RouteRequest/ModelRoute/ModelRouter + StaticModelRouter (v1 всегда deepseek-chat, множитель 1); вшивка в воркер: бюджет × множитель, guard совпадения модели с настройками; правила роутинга отложены до решения №7 (вопрос отправлен владельцу в Telegram); смоук инициализации воркера; Codex + приёмка Claude; commits 4103936+c3d4a8e)
- [x] 3.7 Персистентность диалога (2026-07-10: спека дописана в stage-3.md перед кодом; core/dialog_store.py — load/save messages+dialog_summaries строго через user_transaction (RLS), полная трасса с tool-сообщениями, tokens при записи, meta {prompt_version, stop_reason}; AgentProcessor собирает контекст A–F из загруженной истории (3.4 вшит в путь агента), fire-and-forget суммаризация с границей по id; app-пул в воркере; живой e2e — диалог с памятью между задачами («как зовут кота?» → из истории БД), попутно вживую сработала обработка malformed 3.3; Codex + приёмка Claude; commits 08f41b6+0d4c3cf)
- [ ] 3.8 Учёт стоимости в usage

## Этап 4. Инструменты
- [ ] 4.1 Скелет реестра и диспетчера (+идемпотентность §1.7)
- [ ] 4.2 Финансы (реестр §5)
- [ ] 4.3 Напоминания + шедулер (§6.1–6.3)
- [ ] 4.4 Память + бенчмарк эмбеддингов + сервис (§7)
- [ ] 4.5 SearXNG + web_search/fetch_page (§8)
- [ ] 4.6 PDF-пайплайн + search_documents (§9)
- [ ] 4.7 Google OAuth + calendar/gmail (§10) — ждёт №5 и верификацию
- [ ] 4.8 Подтверждения mutating_external + outbox
- [ ] 4.9 Голосовые (STT, §11) — ждёт №6
- [ ] /feedback + финальные welcome//help
- [ ] ВЕХА: закрытая бета

## Этап 5. Фоновые задачи и лимиты
- [ ] 5.1 schedule_task (§6.4–6.6)
- [ ] 5.2 Дневные бюджеты ₽ + деградация
- [ ] 5.3 Счётчики тарифных лимитов
- [ ] 5.4 Уведомления 80%

## Этап 6. Наблюдаемость
- [ ] 6.1 Structured logging + trace_id
- [ ] 6.2 Prometheus + Grafana (+продуктовые метрики)
- [ ] 6.3 Sentry
- [ ] 6.4 Алерты
- [ ] 6.5 Runbook
- [ ] 6.6 Правило проверки бэкапа
- [ ] 6.7 Критерии выхода из беты зафиксированы

## Этап 7. Онбординг и биллинг
- [ ] 7.1 Tribute webhook — ждёт №8
- [ ] 7.2 Триал (7 дней ИЛИ 50 сообщ.)
- [ ] 7.3 Grace-период
- [ ] 7.4 Тариф v1 — финал лимитов по №11
- [ ] 7.5 GDPR экспорт/удаление — ждёт №9
- [ ] 7.6 Публичная оферта
- [ ] 7.7 Отключение ручного одобрения
- [ ] ВЕХА: публичный запуск

## Отложенные обсуждения (статус — мастер-план)
- [ ] №10 Google-заявка (этап 0) | [ ] №3 хостер+152-ФЗ | [ ] №5 OAuth | [ ] №6 STT | [ ] №7 роутинг (набросок к 3.6) | [ ] №8 Tribute | [ ] №9 хранение | [~] №11 лимиты (черновик готов, финал перед 7.4)

## Ревизии плана (регламент п.5)
- [x] После этапа 2 (2026-07-10: контракты 2↔3 стыкуются без изменений; в stage-3.md 3.2 внесены обязательные наследия этапа 2 — продление лока в цикле, DLQ-порог по фактическим попыткам process(), прокидывание users.id из onboarding-резолва; сроки и состав этапов 3+ без правок; блокеры 0.5/0.6/№10 достигли дедлайна — требуют действий владельца, см. Блокеры)
- [ ] После этапа 4

## Передача сессии (2026-07-10, ночь)

- Этап 2 закрыт целиком; ревизия после этапа 2 выполнена; 3.1–3.7 приняты. Следующий пункт: **3.8 (учёт стоимости в таблицу usage; учесть известный долг — при таймауте AgentLoop возвращается Decimal(0) вместо фактически потраченного)**. После 3.8 — проверка DoD этапа 3 целиком. Вопросы владельцу (№7 роутинг, блокеры домен/хостер/privacy/Google) отправлены в Telegram-канал 2026-07-10. Канал уведомлений владельцу: Telegram-бот @vAssistantv2workbot (chat_id в памяти Claude) — сводка после каждого принятого пункта, чтение указаний на чекпоинтах. Правило: лимиты Codex кончились → передача сюда, коммит, стоп (не подменять Codex Claude'ом). Урок ночного автозапуска 03:00: фоновая сессия зависла на запросе разрешения на Edit — для автозапусков нужен режим с автоподтверждением правок.
- Блокеры 0.5/0.6/№10 на дедлайне — ждут владельца (домен, №3 хостер/юрисдикция, privacy policy, заявка Google; критический путь к 4.7).
- Приёмка любого диффа: дифф целиком → ruff/mypy strict (запускать `uv run mypy` без аргументов — с `.` ломается на tests/) → pytest (unit без окружения: integration должны скипаться; с окружением: всё) → gitleaks (git-mode, `MSYS_NO_PATHCONV=1`) → пуш → зелёный CI → отметка здесь → дописать docs/dev-log.md (sonnet-сабагент) → переиндексировать codebase-memory.

## Блокеры
- 0.5 (частично): BotFather (тест+бой) и OpenRouter — готово 2026-07-09, токены в локальном bootstrap.env до Infisical (1.5); проверить лимит расходов в OpenRouter. Остаются: Google Cloud + consent screen, домен, Tribute (к этапу 7).
- 0.6/№10: privacy policy публикуется после выбора домена; заявка в Google — после публикации. Дедлайн «не позже конца этапа 2» НАСТУПИЛ (2026-07-10, этап 2 закрыт): нужны действия владельца — выбрать/купить домен, решить №3 (хостер/юрисдикция влияет на privacy policy), после публикации политики — подать заявку Google (верификация — недели, критический путь к 4.7). Этап 3 можно вести параллельно, но каждый день задержки заявки сдвигает 4.7.
- Перед публичным запуском: перевыпустить OpenRouter-ключ и боевой токен бота (засветились в чате при передаче 2026-07-09).
