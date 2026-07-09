PYTHON ?= ./.venv/Scripts/python.exe

.PHONY: lint test test-integration gitleaks dev-kek dev-up dev-down dev-destroy db-roles migrate infisical-bootstrap backup backup-weekly backup-check backup-infisical restore-to-dev

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest -v

test-integration:
	uv run pytest -v -m integration

# Local secret scan via docker (CI uses gitleaks-action)
gitleaks:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:latest detect --source /repo --no-banner

dev-kek:
	python -c "from core.crypto import generate_base64_kek; print(generate_base64_kek())"

dev-up:
	docker compose -f infra/docker-compose.dev.yml up -d --build --wait

dev-down:
	docker compose -f infra/docker-compose.dev.yml down

dev-destroy:
	docker compose -f infra/docker-compose.dev.yml down -v

db-roles:
	docker compose -f infra/docker-compose.dev.yml exec -T postgres psql -U assistant -d assistant -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/10-init-roles.sql

migrate:
	uv run alembic upgrade head

infisical-bootstrap:
	$(PYTHON) infra/infisical/bootstrap.py

backup:
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres sh -c 'test -f /var/lib/pgbackrest/repo1/backup/assistant/backup.info -a -f /var/lib/pgbackrest/repo2/backup/assistant/backup.info || pgbackrest --stanza=assistant stanza-create'
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres pgbackrest --repo=1 --stanza=assistant --type=full backup

backup-weekly:
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres sh -c 'test -f /var/lib/pgbackrest/repo1/backup/assistant/backup.info -a -f /var/lib/pgbackrest/repo2/backup/assistant/backup.info || pgbackrest --stanza=assistant stanza-create'
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres pgbackrest --repo=2 --stanza=assistant --type=full backup

backup-check:
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres pgbackrest --stanza=assistant check
	docker compose -f infra/docker-compose.dev.yml exec -T -u postgres postgres pgbackrest --repo=1 --stanza=assistant info

backup-infisical:
	docker compose -f infra/docker-compose.dev.yml exec -T infisical-db sh -c 'mkdir -p /backups && pg_dump -U infisical -d infisical -Fc -f "/backups/infisical-$$(date -u +%Y%m%dT%H%M%SZ).dump"'

restore-to-dev:
	bash infra/restore-to-dev.sh
