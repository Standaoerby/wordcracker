#!/usr/bin/env bash
# scripts/install_systemd_units.sh — sync wordcracker systemd unit
# files from the repo into /etc/systemd/system/ on the prod host.
#
# After S-B2 (docs/v2/decisions.md → 2026-05-24 S-B2) only the
# host-side status_server has a systemd unit; chat / admin run as
# compose services with PID 1 = python and are supervised by Docker
# (D-SB2-6). The script also `systemctl enable`s the status unit so
# it survives a reboot (D-SB2-3).
#
# Usage (as a user with sudo):
#     bash scripts/install_systemd_units.sh
#
# Idempotent. Safe to re-run after a `git pull` that touches
# systemd/*.service.
#
# This script is intentionally separate from scripts/deploy.sh:
# deploy.sh is routine (every deploy); install_systemd_units is
# one-time-per-systemd-change and requires sudo.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not on PATH; this script is for the prod host" >&2
    exit 1
fi

if [[ "$EUID" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
    echo "ERROR: this script needs passwordless sudo (run via sudo or configure NOPASSWD)" >&2
    exit 2
fi

# After S-B2: only the host-side status_server stays a systemd unit.
# wordcracker-chat / wordcracker-admin were removed from systemd
# entirely (D-SB2-6) — they are compose services now.
UNITS=(
    "systemd/wordcracker-status.service"
)

for src in "${UNITS[@]}"; do
    if [[ ! -f "$src" ]]; then
        echo "[install] skip: ${src} not in repo" >&2
        continue
    fi
    dst="/etc/systemd/system/$(basename "$src")"
    echo "[install] $src -> $dst"
    sudo install -m 644 "$src" "$dst"
done

# Detect leftover post-S-B2 unit files on the host and warn the operator.
# This is a one-time cleanup hint, not a hard failure — chat/admin may
# already be stopped but their unit files can linger from before S-B2.
for stale in wordcracker-chat.service wordcracker-admin.service; do
    if [[ -f "/etc/systemd/system/${stale}" ]]; then
        echo "[install] WARN: /etc/systemd/system/${stale} still present (pre-S-B2)" >&2
        echo "[install]       run: sudo systemctl disable --now ${stale%.service} && sudo rm /etc/systemd/system/${stale}" >&2
    fi
done
if [[ -d "/etc/systemd/system/wordcracker-chat.service.d" ]]; then
    echo "[install] WARN: /etc/systemd/system/wordcracker-chat.service.d (drop-in dir) still present" >&2
    echo "[install]       run: sudo rm -rf /etc/systemd/system/wordcracker-chat.service.d" >&2
fi

echo "[install] systemctl daemon-reload"
sudo systemctl daemon-reload

# D-SB2-3: enable so status_server survives reboot. `enable` links the
# unit into multi-user.target.wants/ → fires on next boot. `restart` is
# safe (start if not running, restart if running). The previous nohup'd
# status_server.py — if any — listens on the same :8889 and will block
# the systemd start with EADDRINUSE; the WARN below tells the operator.
echo "[install] systemctl enable wordcracker-status"
sudo systemctl enable wordcracker-status

echo "[install] systemctl restart wordcracker-status"
if ! sudo systemctl restart wordcracker-status; then
    echo "[install] WARN: wordcracker-status restart failed." >&2
    echo "[install]       Likely cause: a manual nohup of status_server.py is still bound to :8889." >&2
    echo "[install]       Find it with: pgrep -f 'python.*status_server.py'  →  kill, then re-run." >&2
    exit 3
fi

echo "[install] OK — systemd units in sync with repo (status only; chat/admin run via compose)"
