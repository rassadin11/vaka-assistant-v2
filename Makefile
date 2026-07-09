.PHONY: lint test gitleaks dev-up dev-down dev-destroy

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest -v

# Local secret scan via docker (CI uses gitleaks-action)
gitleaks:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:latest detect --source /repo --no-banner

dev-up:
	docker compose -f infra/docker-compose.dev.yml up -d --wait

dev-down:
	docker compose -f infra/docker-compose.dev.yml down

dev-destroy:
	docker compose -f infra/docker-compose.dev.yml down -v
