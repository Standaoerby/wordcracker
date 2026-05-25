#!/usr/bin/env bash
# scripts/smoke_s_b2.sh — S-B1 + S-B2 end-to-end acceptance smoke.
#
# Runs the REAL deploy mechanism, not its middle step:
#
#   STEP 1.  bash scripts/deploy.sh HEAD
#            = full build (Docker layer cache makes no-op rebuild fast)
#              + atomic .env tag bump
#              + compose up -d --force-recreate gutenberg-lab chat admin
#              + deploy.sh's own internal verify (D-SB1-4 step 6)
#              Smoke inherits any non-zero exit (set -e).
#
#   STEP 2.  bash scripts/verify_deployed_image.sh
#            = belt-and-suspenders re-check that all three app
#              services run the expected SHA tag (D-SB1-5 / D-SB2-1).
#              Fail loudly on any partial-deploy drift.
#
#   STEP 3.  Poll /health (or /api for jupyter) on all three app
#            services until each returns 200, or fail after the budget.
#            chat:8890/health, admin:8891/health, jupyter:8888/api.
#
# Green smoke = "deploy.sh end-to-end + verify passed AND all three
# services healthy from a real recreate". This is the S-B2 acceptance
# gate ("up --force-recreate + systemctl restart collapse into one
# mechanism") and the S-B1 acceptance gate ("verify_deployed_image.sh
# exit 0") at the same time.
#
# Usage:
#     bash scripts/smoke_s_b2.sh
#
# Run from the prod host (or any host with docker + the wordcracker
# stack). Requires a clean git tree (deploy.sh refuses dirty trees
# without --allow-dirty; smoke does not paper over that — commit /
# stash first).
#
# Budget: 180s upper bound for the polling step. Cold-boot worst case
# = ollama start_period (90s) + chat warmup (~60s) + slack. Warm
# restart usually clears in 5-10 s.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "[smoke] STEP 1/3 — bash scripts/deploy.sh HEAD"
echo "[smoke]            (build + .env bump + force-recreate + deploy.sh internal verify)"
bash scripts/deploy.sh HEAD

# D-SB4-2: read the SHA deploy.sh just wrote into .env and pass it
# explicitly to the belt-and-suspenders verify. Before D-SB4-2 the
# verify_deployed_image.sh fallback was `git rev-parse --short HEAD`,
# which would silently agree with the deploy as long as the host's HEAD
# hadn't moved since deploy.sh resolved it — leaving a window where a
# concurrent `git pull` on the host between the two calls could flip
# the expectation under verify's feet. .env is the durable
# source of truth for "what tag we just deployed".
if [[ ! -f .env ]]; then
    echo "[smoke] FAIL: .env missing after deploy.sh — cannot pass explicit SHA to verify" >&2
    exit 5
fi
EXPECTED_SHA="$(grep '^WC_IMAGE_TAG=' .env | tail -1 | cut -d= -f2-)"
if [[ -z "$EXPECTED_SHA" ]]; then
    echo "[smoke] FAIL: .env has no WC_IMAGE_TAG line — deploy.sh did not write it" >&2
    exit 5
fi

echo
echo "[smoke] STEP 2/3 — bash scripts/verify_deployed_image.sh ${EXPECTED_SHA}"
echo "[smoke]            (re-check: gutenberg-lab + chat + admin all on the same SHA tag)"
bash scripts/verify_deployed_image.sh "${EXPECTED_SHA}"

echo
echo "[smoke] STEP 3/3 — poll chat:8890/health, admin:8891/health, jupyter:8888/api"

BUDGET_SECS=180
START_TS="$(date +%s)"

probe() {
    local url="$1"
    curl -sf --max-time 2 "$url" >/dev/null
}

# (service, probe-url) pairs. Jupyter has no /health by convention;
# /api returns 200 JSON when the server is up.
declare -A probe_url=(
    [chat]="http://127.0.0.1:8890/health"
    [admin]="http://127.0.0.1:8891/health"
    [jupyter]="http://127.0.0.1:8888/api"
)
declare -A done_probe=( [chat]=0 [admin]=0 [jupyter]=0 )

while true; do
    now="$(date +%s)"
    elapsed=$((now - START_TS))
    if (( elapsed > BUDGET_SECS )); then
        echo "[smoke] FAIL: poll budget ${BUDGET_SECS}s exhausted" >&2
        for svc in chat admin jupyter; do
            if [[ "${done_probe[$svc]}" != "1" ]]; then
                echo "[smoke]   ${svc} (${probe_url[$svc]}) never returned 200" >&2
            fi
        done
        echo "[smoke]   docker compose ps:" >&2
        docker compose -f docker-compose.yml ps >&2 || true
        exit 4
    fi

    for svc in chat admin jupyter; do
        if [[ "${done_probe[$svc]}" == "1" ]]; then
            continue
        fi
        if probe "${probe_url[$svc]}"; then
            done_probe[$svc]=1
            echo "[smoke] OK: ${svc} healthy at ${elapsed}s (${probe_url[$svc]})"
        fi
    done

    if [[ "${done_probe[chat]}" == "1" \
       && "${done_probe[admin]}" == "1" \
       && "${done_probe[jupyter]}" == "1" ]]; then
        echo
        echo "[smoke] OK: deploy.sh + verify passed AND all three services healthy"
        echo "[smoke]     within ${elapsed}s (budget ${BUDGET_SECS}s)"
        exit 0
    fi

    sleep 2
done
