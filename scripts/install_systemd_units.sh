#!/usr/bin/env bash
# scripts/install_systemd_units.sh — sync wordcracker systemd unit
# files from the repo into /etc/systemd/system/ on the prod host.
#
# Run this once after any change to systemd/*.service files. D-SB1-6
# (docs/v2/decisions.md → 2026-05-24 S-B1) requires `-f docker-compose.yml`
# in the unit files, so the first run after S-B1 landing must come from
# this script.
#
# Usage (as a user with sudo):
#     bash scripts/install_systemd_units.sh
#
# This script is intentionally separate from scripts/deploy.sh:
# deploy.sh is a routine operation (every deploy); install_systemd_units
# is a one-time-per-systemd-change operation that requires sudo and
# implies a service restart. Folding them together would mean every
# deploy needs sudo for no behavioural gain on most deploys.

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

UNITS=(
    "systemd/wordcracker-chat.service"
    "systemd/wordcracker-admin.service"
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

echo "[install] systemctl daemon-reload"
sudo systemctl daemon-reload

echo "[install] systemctl restart wordcracker-chat wordcracker-admin"
sudo systemctl restart wordcracker-chat wordcracker-admin || true

echo "[install] OK — systemd units in sync with repo"
