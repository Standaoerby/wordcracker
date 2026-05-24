#!/usr/bin/env bash
# scripts/smoke_s_b2.sh — S-B2 acceptance smoke.
#
# D-SB2-8 (docs/v2/decisions.md → 2026-05-24 S-B2). Force-recreate the
# three app services and poll /health on chat (8890) + admin (8891)
# until both return 200, or fail after the budget.
#
# Usage:
#     bash scripts/smoke_s_b2.sh           # uses current WC_IMAGE_TAG
#     bash scripts/smoke_s_b2.sh <sha>     # exports WC_IMAGE_TAG=<sha> first
#
# Exit 0 if both /health endpoints return 200 within the budget. Non-zero
# on first failure. Run from the prod host (or any host with the compose
# stack up — no docker daemon hit on dev/CI; the test is integration).
#
# Budget: 180s upper bound. Cold-boot worst case = ollama start_period
# (90s) + chat warmup (~60s) + slack. Warm restart usually clears in
# 5-10 s.

set -euo pipefail

cd "$(dirname "$0")/.."

TAG_OVERRIDE="${1:-}"
if [[ -n "$TAG_OVERRIDE" ]]; then
    export WC_IMAGE_TAG="$TAG_OVERRIDE"
fi

if [[ -z "${WC_IMAGE_TAG:-}" ]]; then
    # No env var, no arg — try .env in CWD as deploy.sh would.
    if [[ -f ".env" ]] && grep -q '^WC_IMAGE_TAG=' .env; then
        # shellcheck disable=SC1091
        set -a; source .env; set +a
    fi
fi
if [[ -z "${WC_IMAGE_TAG:-}" ]]; then
    echo "ERROR: WC_IMAGE_TAG is unset and no .env carries it. Pass <sha> or `bash scripts/deploy.sh` first." >&2
    exit 2
fi

echo "[smoke] tag=${WC_IMAGE_TAG}"
echo "[smoke] docker compose up -d --force-recreate gutenberg-lab chat admin"
docker compose -f docker-compose.yml up -d --force-recreate gutenberg-lab chat admin

BUDGET_SECS=180
START_TS="$(date +%s)"

probe() {
    local name="$1" port="$2"
    if curl -sf --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
        return 0
    fi
    return 1
}

declare -A done_probe=( [chat]=0 [admin]=0 )
declare -A port_of=( [chat]=8890 [admin]=8891 )

while true; do
    now="$(date +%s)"
    elapsed=$((now - START_TS))
    if (( elapsed > BUDGET_SECS )); then
        echo "[smoke] FAIL: budget ${BUDGET_SECS}s exhausted" >&2
        for svc in chat admin; do
            if [[ "${done_probe[$svc]}" != "1" ]]; then
                echo "[smoke]   ${svc} /health on :${port_of[$svc]} never returned 200" >&2
            fi
        done
        echo "[smoke]   docker compose ps:" >&2
        docker compose -f docker-compose.yml ps >&2 || true
        exit 4
    fi

    for svc in chat admin; do
        if [[ "${done_probe[$svc]}" == "1" ]]; then
            continue
        fi
        if probe "$svc" "${port_of[$svc]}"; then
            done_probe[$svc]=1
            echo "[smoke] OK: ${svc} /health 200 at ${elapsed}s"
        fi
    done

    if [[ "${done_probe[chat]}" == "1" && "${done_probe[admin]}" == "1" ]]; then
        echo "[smoke] OK: both /health endpoints up within ${elapsed}s (budget ${BUDGET_SECS}s)"
        exit 0
    fi

    sleep 2
done
