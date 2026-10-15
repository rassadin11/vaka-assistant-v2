#!/usr/bin/env bash
# Nightly backup for the small profile: dump -> encrypt -> copy off the box ->
# rotate (plan/small-profile.md, sections 4.6 and S11).
#
# pgbackrest is off in this profile: it costs a WAL archive, a repository and a
# stanza to maintain, and on a 5-10 person install a nightly logical dump is
# both enough and far easier to restore. The trade-off is explicit -- recovery
# granularity is one day, not one transaction.
#
# The dump is taken with pg_dump inside the postgres container, so the host
# needs no postgres client. Everything else is coreutils plus openssl.
#
# Usage:
#   infra/small/backup.sh
#
# Environment (typically /etc/assistant/backup.env, mode 600):
#   BACKUP_PASSPHRASE   required. Symmetric key for the encrypted dump.
#   BACKUP_DIR          local directory for dumps (default /var/backups/assistant).
#   BACKUP_KEEP_DAYS    how long local dumps live (default 14).
#   BACKUP_RCLONE_REMOTE  optional rclone target, e.g. "backup:assistant".
#   BACKUP_SCP_TARGET     optional scp target, e.g. "user@host:/backups/assistant".
#   BACKUP_ALLOW_LOCAL_ONLY  set to 1 to run without any off-box destination.
#                            For rehearsals and restore drills only.
#   COMPOSE_FILE        compose file to talk to (default infra/compose.small.yml).
#   COMPOSE_ENV_FILE    env file passed to compose (default /etc/assistant/small-infra.env).
#
# A dump that never leaves the server is not a backup: it dies with the disk it
# lives on. The script therefore *fails* when no off-box destination is
# configured, instead of quietly producing a local-only file.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/infra/compose.small.yml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-/etc/assistant/small-infra.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/assistant}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
POSTGRES_USER="${POSTGRES_USER:-assistant}"
POSTGRES_DB="${POSTGRES_DB:-assistant}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE is required"
if [ -z "${BACKUP_RCLONE_REMOTE:-}" ] && [ -z "${BACKUP_SCP_TARGET:-}" ]; then
    if [ "${BACKUP_ALLOW_LOCAL_ONLY:-0}" != "1" ]; then
        die "no off-box destination: set BACKUP_RCLONE_REMOTE or BACKUP_SCP_TARGET"
    fi
    log "WARNING: no off-box destination; the dump stays on this machine"
fi

compose() {
    docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV_FILE" "$@"
}

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$BACKUP_DIR/assistant-$stamp.dump"
encrypted="$dump.enc"

# A failed run must not leave a half-written dump behind: the next operator
# would find a plausible-looking file that restores to nothing.
cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        rm -f "$dump" "$encrypted"
    fi
    rm -f "$dump"
    return "$status"
}
trap cleanup EXIT

# -Fc is the custom format: already compressed, and pg_restore can read it
# selectively -- which is what makes the data-only restore in S18 possible.
#
# Two bookkeeping tables are left out because the restore path rebuilds them:
# alembic_version is written by `alembic upgrade head`, and pg_partman's
# part_config rows are created by the migration that calls create_parent().
# Restoring them on top fails on duplicate keys -- noise that would hide a real
# failure in the same output.
log "dumping $POSTGRES_DB"
compose exec -T postgres pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    --no-privileges \
    --exclude-table=alembic_version \
    --exclude-table=part_config \
    --exclude-table=part_config_sub \
    > "$dump"

[ -s "$dump" ] || die "pg_dump produced an empty file"

# The dump carries every message, note and document of every user. Encrypt
# before it touches any storage that is not this box.
log "encrypting $(basename "$dump")"
printf '%s' "$BACKUP_PASSPHRASE" | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$dump" -out "$encrypted" -pass stdin
rm -f "$dump"

size="$(du -h "$encrypted" | cut -f1)"
log "encrypted dump ready: $(basename "$encrypted") ($size)"

if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
    log "uploading to rclone remote $BACKUP_RCLONE_REMOTE"
    rclone copy "$encrypted" "$BACKUP_RCLONE_REMOTE" --no-traverse
fi
if [ -n "${BACKUP_SCP_TARGET:-}" ]; then
    log "uploading to $BACKUP_SCP_TARGET"
    scp -q "$encrypted" "$BACKUP_SCP_TARGET"
fi

# Rotation is local only. Remote retention belongs to the remote: a compromised
# or misbehaving server must not be able to delete its own off-box history.
log "removing local dumps older than $BACKUP_KEEP_DAYS days"
find "$BACKUP_DIR" -name 'assistant-*.dump.enc' -type f -mtime "+$BACKUP_KEEP_DAYS" -print -delete

log "backup complete"
