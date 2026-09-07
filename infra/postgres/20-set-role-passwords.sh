#!/bin/bash
# Set real passwords for the database roles on a fresh install (small profile).
#
# init-roles.sql creates migrator/app/service with a password literal that is
# fine for a dev container and wrong anywhere else. It is mounted into prod as
# well, so this script -- mounted only by infra/compose.small.yml -- fixes the
# passwords right after that file runs instead of editing it (see
# plan/small-profile.md, S9 clarifications).
#
# Docker's postgres entrypoint runs everything in /docker-entrypoint-initdb.d in
# name order, and only on first initialisation of the data directory. Rotating
# later is a plain ALTER ROLE against a running server.

set -euo pipefail

require() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        # Failing here leaves the cluster uninitialised, which is loud. Starting
        # with the default password would be quiet and much worse.
        echo "$name is required to initialise the small profile database" >&2
        exit 1
    fi
}

require MIGRATOR_DB_PASSWORD
require APP_DB_PASSWORD
require SERVICE_DB_PASSWORD

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v migrator_password="$MIGRATOR_DB_PASSWORD" \
    -v app_password="$APP_DB_PASSWORD" \
    -v service_password="$SERVICE_DB_PASSWORD" <<'SQL'
ALTER ROLE migrator WITH PASSWORD :'migrator_password';
ALTER ROLE app WITH PASSWORD :'app_password';
ALTER ROLE service WITH PASSWORD :'service_password';
SQL

echo "role passwords set for migrator, app, service"
