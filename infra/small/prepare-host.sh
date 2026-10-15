#!/usr/bin/env bash
# One-time host preparation for the small profile (plan/small-profile.md S15).
# Target: Debian 12 minimal, 2 GB RAM, 1-2 vCPU, root or sudo.
#
# Everything here has to survive a reboot -- that is the whole point of the
# task, and the reason nothing is set with a bare `sysctl -w` or `swapon`.
#
# Idempotent: safe to run again after a kernel upgrade or a rebuild.
#
# Usage:
#   sudo infra/small/prepare-host.sh

set -euo pipefail

SWAP_FILE="${SWAP_FILE:-/swapfile}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
SYSCTL_FILE="/etc/sysctl.d/60-assistant-small.conf"
PRUNE_UNIT="/etc/systemd/system/docker-prune.service"
PRUNE_TIMER="/etc/systemd/system/docker-prune.timer"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"

# --- swap -------------------------------------------------------------------
# Not working memory: with mem_limit set on every container, the cgroup killer
# takes the offending container first. Swap is what keeps the host itself
# alive through a spike instead of the kernel OOM-killer picking postgres.
if [ -f "$SWAP_FILE" ]; then
    log "swap file $SWAP_FILE already exists, leaving it alone"
else
    log "creating ${SWAP_SIZE_MB}MB swap at $SWAP_FILE"
    # fallocate can produce a sparse file that swapon rejects on some
    # filesystems; dd is slower and always works.
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MB" status=none
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
fi

swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE" || swapon "$SWAP_FILE"

if grep -q "^$SWAP_FILE " /etc/fstab; then
    log "fstab already mounts $SWAP_FILE"
else
    log "adding $SWAP_FILE to /etc/fstab"
    printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> /etc/fstab
fi

# --- kernel settings --------------------------------------------------------
log "writing $SYSCTL_FILE"
cat > "$SYSCTL_FILE" <<'SYSCTL'
# Small deployment profile (plan/small-profile.md, section 2).

# Swap is a safety net, not a tier of memory: only reach for it under real
# pressure. Anything higher and the box starts paging the worker's Python heap
# out during idle, which shows up as seconds-long latency on the next message.
vm.swappiness = 10

# Redis forks to rewrite its AOF, and the child briefly looks like it needs a
# full copy of the parent's memory. With strict accounting on a 2 GB box that
# fork fails and Redis stops persisting -- silently, until a restart loses data.
vm.overcommit_memory = 1
SYSCTL
sysctl --quiet --load "$SYSCTL_FILE"

# --- disk -------------------------------------------------------------------
# On a small VPS the disk runs out before the memory does: every rebuild leaves
# a dangling image layer and a build cache, and nothing collects them.
log "installing weekly docker prune timer"
cat > "$PRUNE_UNIT" <<'UNIT'
[Unit]
Description=Reclaim disk from unused Docker images, containers and build cache
Documentation=file:///opt/assistant/plan/small-profile.md

[Service]
Type=oneshot
# Volumes are deliberately NOT pruned: postgres_data and redis_data live there.
ExecStart=/usr/bin/docker system prune --force --all --filter "until=168h"
UNIT

cat > "$PRUNE_TIMER" <<'TIMER'
[Unit]
Description=Weekly Docker cleanup

[Timer]
OnCalendar=Sun 04:30
Persistent=true

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now docker-prune.timer

# --- report -----------------------------------------------------------------
log "swap:"
swapon --show
log "sysctl:"
sysctl vm.swappiness vm.overcommit_memory
log "prune timer:"
systemctl list-timers docker-prune.timer --no-pager || true
log "host prepared -- reboot once and re-run this script to confirm it is a no-op"
