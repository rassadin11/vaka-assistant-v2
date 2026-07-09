#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/infra/docker-compose.dev.yml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}" --profile restore)
MARKER_TG_USER_ID="${RESTORE_MARKER_TG_USER_ID:-}"

echo "Restoring latest pgBackRest backup into postgres-restore on localhost:5433..."

cd "${ROOT_DIR}"

"${COMPOSE[@]}" stop postgres-restore >/dev/null 2>&1 || true
"${COMPOSE[@]}" rm -f postgres-restore >/dev/null 2>&1 || true

"${COMPOSE[@]}" run --rm --no-deps --entrypoint bash postgres-restore -lc '
set -Eeuo pipefail
test "${PGDATA}" = "/var/lib/postgresql/data"
find "${PGDATA}" -mindepth 1 -exec rm -rf -- {} +
chown postgres:postgres "${PGDATA}"
gosu postgres pgbackrest --repo=1 --stanza=assistant --delta restore
'

"${COMPOSE[@]}" up -d postgres-restore

for _ in {1..60}; do
    if "${COMPOSE[@]}" exec -T postgres-restore pg_isready -U assistant -d assistant >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

"${COMPOSE[@]}" exec -T postgres-restore pg_isready -U assistant -d assistant >/dev/null

table_count="$("${COMPOSE[@]}" exec -T postgres-restore psql -U assistant -d assistant -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" | tr -d '[:space:]')"
users_exists="$("${COMPOSE[@]}" exec -T postgres-restore psql -U assistant -d assistant -Atc "SELECT to_regclass('public.users') IS NOT NULL;" | tr -d '[:space:]')"

echo "Restored public tables: ${table_count}"

if [[ "${users_exists}" == "t" ]]; then
    users_count="$("${COMPOSE[@]}" exec -T postgres-restore psql -U assistant -d assistant -Atc "SELECT count(*) FROM users;" | tr -d '[:space:]')"
    echo "Restored users rows: ${users_count}"

    if [[ -n "${MARKER_TG_USER_ID}" ]]; then
        marker_count="$("${COMPOSE[@]}" exec -T postgres-restore psql -U assistant -d assistant -Atc "SELECT count(*) FROM users WHERE tg_user_id = ${MARKER_TG_USER_ID};" | tr -d '[:space:]')"
        if [[ "${marker_count}" != "1" ]]; then
            echo "FAIL: marker user tg_user_id=${MARKER_TG_USER_ID} not found in restored DB" >&2
            exit 1
        fi
        echo "PASS: marker user tg_user_id=${MARKER_TG_USER_ID} found"
    fi
fi

echo "PASS: restore-to-dev completed; restored DB is available on localhost:5433"
