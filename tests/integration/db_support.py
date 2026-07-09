"""Connection settings shared by integration tests."""

from pathlib import Path

ADMIN_DATABASE_URL = "postgresql://assistant:dev-local-only@127.0.0.1:5432/assistant"
APP_DATABASE_URL = "postgresql://app:dev-local-only@127.0.0.1:6432/assistant"
SERVICE_DATABASE_URL = "postgresql://service:dev-local-only@127.0.0.1:6432/assistant"
MIGRATOR_DATABASE_URL = "postgresql://migrator:dev-local-only@127.0.0.1:5432/assistant"
ROLE_SQL_PATH = Path(__file__).parents[2] / "infra" / "postgres" / "init-roles.sql"
