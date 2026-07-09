.PHONY: lint test test-integration gitleaks dev-up dev-down dev-destroy db-roles

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

dev-up:
	docker compose -f infra/docker-compose.dev.yml up -d --wait

dev-down:
	docker compose -f infra/docker-compose.dev.yml down

dev-destroy:
	docker compose -f infra/docker-compose.dev.yml down -v

db-roles:
	docker compose -f infra/docker-compose.dev.yml exec -T postgres psql -U assistant -d assistant -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/10-init-roles.sql
