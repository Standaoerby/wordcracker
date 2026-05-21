#!/usr/bin/env bash
# v5 deploy smoke check — run on the Server-on-Wheels host after each
# stage of `vault/v5_deploy_guide.md`.
#
# Usage:
#   ./scripts/v5_smoke.sh                # full check
#   ./scripts/v5_smoke.sh --stage 1      # quick check for Stage 1
#
# Exit codes:
#   0  — all green, safe to proceed to next stage
#   1  — at least one check failed; do NOT advance, inspect output
#
# The script is idempotent and READ-ONLY against the running stack.
# It does not flip flags, edit configs, or restart anything.

set -euo pipefail

STAGE="${1:-all}"
RED=$'\033[0;31m'
GRN=$'\033[0;32m'
YEL=$'\033[0;33m'
RST=$'\033[0m'
FAILS=0

say() { printf '%s\n' "$*"; }
ok()  { printf '  %s✓%s %s\n'   "$GRN" "$RST" "$*"; }
bad() { printf '  %s✗%s %s\n'   "$RED" "$RST" "$*"; FAILS=$((FAILS+1)); }
warn(){ printf '  %s!%s %s\n'   "$YEL" "$RST" "$*"; }

CHAT_URL="${CHAT_URL:-http://127.0.0.1:8890}"
LOG_DIR="${WC_V2_LOG_DIR:-/workspace/spgc/derived/v2_logs}"
TODAY="$(date -u +%F)"
LOG_FILE="${LOG_DIR}/queries-${TODAY}.jsonl"


# -----------------------------------------------------------
# Stage 0 — container health (always run)
# -----------------------------------------------------------

say "[0] container health"

if curl -sf "${CHAT_URL}/health" -o /tmp/v5_health.json 2>/dev/null; then
    ok "chat /health responding"
else
    bad "chat /health unreachable at ${CHAT_URL}"
fi

if curl -sf "${CHAT_URL}/" -o /tmp/v5_index.html 2>/dev/null; then
    ok "chat index page loads"
    if grep -q "wordcracker analytics v" /tmp/v5_index.html 2>/dev/null; then
        ok "version footer rendered"
    else
        warn "version footer placeholder NOT substituted — page may be stale"
    fi
    if grep -q "COMPOSER_MAX_BYTES" /tmp/v5_index.html; then
        ok "composer cap JS present"
    else
        bad "composer cap JS missing — B-R14-17 fix not deployed"
    fi
else
    bad "chat index unreachable"
fi


# -----------------------------------------------------------
# Stage 1 checks — observability flags
# -----------------------------------------------------------

if [[ "$STAGE" == "all" || "$STAGE" == "1" ]]; then
    say
    say "[1] v5 observability"

    if [[ -d "$LOG_DIR" ]]; then
        ok "log dir exists at ${LOG_DIR}"
    else
        bad "log dir missing — JSONL won't be written"
    fi

    # Send one trivial query and verify the v5_* fields appear in JSONL
    REQ_ID="v5smoke-$(date +%s)"
    if curl -sf -X POST "${CHAT_URL}/api/chat" \
            -H "Content-Type: application/json" \
            -d "{\"question\":\"привет\",\"history\":[]}" \
            -o /tmp/v5_resp.json 2>/dev/null; then
        ok "chat POST /api/chat responded"
        sleep 1

        if [[ -f "$LOG_FILE" ]]; then
            LAST_LINE="$(tail -1 "$LOG_FILE" 2>/dev/null || true)"
            if [[ -n "$LAST_LINE" ]]; then
                if echo "$LAST_LINE" | grep -q '"v5_trace_id"'; then
                    ok "JSONL has v5_trace_id"
                else
                    bad "v5_trace_id missing from JSONL — WC_V5_PIPELINE not active inside container?"
                fi
                if echo "$LAST_LINE" | grep -q '"v5_pipeline_version"'; then
                    ok "JSONL has v5_pipeline_version"
                else
                    bad "v5_pipeline_version missing"
                fi
                if echo "$LAST_LINE" | grep -q '"v5_budget_max_s"'; then
                    ok "JSONL has v5_budget_max_s"
                else
                    bad "v5_budget_max_s missing"
                fi
            else
                warn "JSONL is empty — no records found"
            fi
        else
            bad "JSONL ${LOG_FILE} does not exist after query"
        fi
    else
        bad "chat POST failed — container down or networking broken"
    fi
fi


# -----------------------------------------------------------
# Stage 2 checks — entity resolver
# -----------------------------------------------------------

if [[ "$STAGE" == "all" || "$STAGE" == "2" ]]; then
    say
    say "[2] v5 entity resolver"

    # «Толстого» — should resolve via RU genitive lemmatization
    RESP="$(curl -sf -X POST "${CHAT_URL}/api/chat" \
                 -H "Content-Type: application/json" \
                 -d '{"question":"когда родился Толстого","history":[]}' \
                 -o - 2>/dev/null || true)"
    if echo "$RESP" | grep -qi "tolstoy\|толстой\|1828"; then
        ok "Толстого (genitive) → Tolstoy"
    else
        warn "Толстого did not surface Tolstoy — check resolver flag"
    fi
fi


# -----------------------------------------------------------
# Stage 3 checks — deterministic renderer
# -----------------------------------------------------------

if [[ "$STAGE" == "all" || "$STAGE" == "3" ]]; then
    say
    say "[3] v5 renderer (deterministic skeleton)"

    # Canonical Burrows Delta 0.4385 should appear byte-exact
    RESP="$(curl -sf -X POST "${CHAT_URL}/api/chat" \
                 -H "Content-Type: application/json" \
                 -d '{"question":"на кого по стилю похож Doyle","history":[]}' \
                 -o - 2>/dev/null || true)"
    if echo "$RESP" | grep -q "0.4385"; then
        ok "canonical Burrows Delta 0.4385 present"
    else
        warn "0.4385 missing — renderer path or tool returned different value"
    fi

    sleep 1
    LAST_LINE="$(tail -1 "$LOG_FILE" 2>/dev/null || true)"
    if echo "$LAST_LINE" | grep -q '"v5_render_view_type":'; then
        ok "JSONL has v5_render_view_type — renderer routed via v5"
    else
        warn "v5_render_view_type missing — WC_V5_RENDERER may not be active"
    fi
fi


# -----------------------------------------------------------
# Summary
# -----------------------------------------------------------

say
if [[ $FAILS -eq 0 ]]; then
    say "${GRN}all green${RST} — safe to advance to next stage"
    exit 0
else
    say "${RED}${FAILS} check(s) failed${RST} — DO NOT advance; inspect output"
    say
    say "Rollback: edit docker-compose.override.yml, remove last v5 flag,"
    say "         then: docker compose up -d gutenberg-lab"
    exit 1
fi
