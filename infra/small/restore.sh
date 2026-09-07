#!/usr/bin/env bash
# Restore an encrypted dump produced by backup.sh (plan/small-profile.md S11).
#
# Two jobs, one script:
#   1. disaster recovery -- put a nightly dump back into this install;
#   2. the restore drill that S11 actually requires. A backup nobody has ever
#      restored is a hypothesis, not a backup, so run this against a scratch
#      database regularly, not only when something has already burned down.
#
# It is also the mechanism S18 uses to carry the production data over, which is
# why the schema comes from alembic and only the data comes from the dump:
#   - `alembic upgrade head` first, on an empty database, so tables, RLS
#     policies and the generated full-text columns exist as this version of the
#     code expects them;
#   - `pg_restore --data-only` second. The reverse order breaks on the
#     generated tsvector columns, which cannot be written to directly.
#
# Restoring runs as the postgres superuser inside the container: under the app
# role, row level security would filter other users' rows out and the restore
# would silently bring back only part of the data.
#
# Usage:
#   infra/small/restore.sh <dump.enc> [target-database]
#
# Environment:
#   BACKUP_PASSPHRASE   required, the key the dump was encrypted with.
#   COMPOSE_FILE        default infra/compose.small.yml
#   COMPOSE_ENV_FILE    default /etc/assistant/small-infra.env
#   MIGRATE_SERVICE     compose service that runs alembic (default "migrate").
#   SKIP_MIGRATIONS     set to 1 when the target database already has the schema.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/infra/compose.small.yml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-/etc/assistant/small-infra.env}"
POSTGRES_USER="${POSTGRES_USER:-assistant}"
MIGRATE_SERVICE="${MIGRATE_SERVICE:-migrate}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

encrypted="${1:-}"
target_db="${2:-assistant}"
[ -n "$encrypted" ] || die "usage: restore.sh <dump.enc> [target-database]"
[ -f "$encrypted" ] || die "no such file: $encrypted"
[ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE is required"

compose() {
    docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV_FILE" "$@"
}

plain="$(mktemp)"
trap 'rm -f "$plain"' EXIT

log "decrypting $(basename "$encrypted")"
printf '%s' "$BACKUP_PASSPHRASE" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "$encrypted" -out "$plain" -pass stdin
[ -s "$plain" ] || die "decryption produced an empty file"

log "creating database $target_db if it does not exist"
compose exec -T postgres psql --username "$POSTGRES_USER" --dbname postgres -v ON_ERROR_STOP=1 -Atc \
    "SELECT 1 FROM pg_database WHERE datname = '$target_db'" | grep -q 1 ||
    compose exec -T postgres createdb --username "$POSTGRES_USER" "$target_db"

# init-roles.sql runs only against the database the container initialises, and
# its grants name "assistant" literally. A drill database created here would
# otherwise reject the very first migration with "permission denied for schema
# public" -- Postgres 15 stopped granting that implicitly. These statements are
# the same grants, aimed at the target, and are safe to repeat.
log "granting the roles access to $target_db"
compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$target_db" -v ON_ERROR_STOP=1 -v db="$target_db" <<'SQL'
GRANT CONNECT ON DATABASE :"db" TO migrator, app, service, metrics_ro;
GRANT CREATE ON DATABASE :"db" TO migrator;
GRANT USAGE, CREATE ON SCHEMA public TO migrator;
GRANT USAGE ON SCHEMA public TO app, service, metrics_ro;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_partman;
-- After the extensions, not before: pg_partman keeps its own tables and
-- functions in this schema, and create_parent() writes to them as migrator.
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO migrator;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app, service;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO app, service;
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO metrics_ro;
SQL


if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    log "applying migrations to $target_db"
    # shellcheck disable=SC2016  # the inner shell expands these, not this one
    compose run --rm \
        -e MIGRATIONS_DATABASE_URL_OVERRIDE_DB="$target_db" \
        "$MIGRATE_SERVICE" \
        sh -ec 'url="${MIGRATOR_DATABASE_URL:?}"; \
                exec env MIGRATIONS_DATABASE_URL="${url%/*}/${MIGRATIONS_DATABASE_URL_OVERRIDE_DB}" \
                alembic upgrade head'
fi

log "restoring data into $target_db"
compose exec -T postgres pg_restore \
    --username "$POSTGRES_USER" \
    --dbname "$target_db" \
    --data-only \
    --disable-triggers \
    --no-owner \
    --no-privileges \
    < "$plain"

log "row counts in $target_db"
compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$target_db" -Atc "
SELECT relname || ' = ' || n_live_tup
FROM pg_stat_user_tables
ORDER BY relname;"

log "restore complete -- compare the counts above with the source database"
