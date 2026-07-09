.PHONY: lint test gitleaks dev-up

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest -v

# Local secret scan via docker (CI uses gitleaks-action)
gitleaks:
	docker run --rm -v "$(CURDIR):/repo" ghcr.io/gitleaks/gitleaks:latest detect --source /repo --no-banner

# Полное dev-окружение появляется на этапе 1.1 (plan/stage-1.md)
dev-up:
	@echo "dev-up: docker-compose окружение реализуется в 1.1 (plan/stage-1.md)"
