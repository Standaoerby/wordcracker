#!/usr/bin/env bash
# scripts/verify_deployed_image.sh — assert each app service's running
# container is from the expected SHA-tagged image.
#
# D-SB1-5 + D-SB2-1 (docs/v2/decisions.md → 2026-05-24 S-B1 / S-B2).
# After S-B2 there are three app services sharing the same image tag:
# gutenberg-lab (jupyter), chat (8890), admin (8891). All must report
# the same expected SHA.
#
# Usage:
#     bash scripts/verify_deployed_image.sh <expected>   # explicit SHA (preferred)
#     bash scripts/verify_deployed_image.sh              # falls back to $WC_IMAGE_TAG
#
# D-SB4-2: there is NO git-HEAD fallback. When called with no arg AND
# no WC_IMAGE_TAG in env, the script exits 2 with "refusing to fall
# back to git HEAD". Previously the script grabbed
# `git rev-parse --short HEAD` of the host repo, which silently
# compared the running image against whatever the host happened to
# have checked out — wrong after a host-side `git pull` advanced HEAD
# past the deployed SHA. deploy.sh always passes the SHA it built;
# smoke_s_b2.sh sources .env's WC_IMAGE_TAG and passes it explicitly.
#
# Exit 0 on match across all services; non-zero on first mismatch.
#
# This script verifies TWO surfaces of build identity (ADR-B3 / S-B3):
#   1. Docker-tag: each compose service's running image tag matches
#      the expected SHA (the S-B1 + S-B2 contract).
#   2. Runtime: each app /health reports the same git_sha (the
#      ADR-B3 / D-SB3-3 contract). Closes the "tag correct but the
#      running process is from an earlier image" gap.
#
# WITH_RUNTIME=0 skips step 2 (useful for offline/unit-test contexts
# where the services are not actually up). Default WITH_RUNTIME=1.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_NAME="wordcracker-textlab"
# D-SB2-1: all three app services share the same image tag. Verify
# every one — a partial deploy where chat/admin are on the new tag but
# gutenberg-lab is on the old (or vice versa) is exactly the silent
# drift this script exists to catch.
SERVICES=("gutenberg-lab" "chat" "admin")

EXPECTED="${1:-}"
if [[ -z "$EXPECTED" ]]; then
    # D-SB4-2: no git-HEAD fallback. Use WC_IMAGE_TAG from env (deploy.sh
    # exports it; smoke_s_b2.sh sources .env's value and passes it
    # explicitly via $1). If neither surface supplies a value, fail
    # loudly — the previous behaviour silently compared against the
    # host's working-tree HEAD, which diverges from "what was deployed"
    # after any `git pull` on the host between the deploy and the
    # verify run.
    EXPECTED="${WC_IMAGE_TAG:-}"
fi
if [[ -z "$EXPECTED" ]]; then
    cat >&2 <<'EOF'
ERROR: refusing to fall back to git HEAD for the expected SHA.
       Pass the expected tag explicitly:
           bash scripts/verify_deployed_image.sh <sha>
       or export WC_IMAGE_TAG=<sha> in the environment (deploy.sh and
       the .env-driven path do this automatically; if you're running
       verify by hand, source .env or pass the SHA as the first arg).
EOF
    exit 2
fi

resolve_running_image() {
    local service="$1"
    local running_image=""

    # Prefer `docker compose ps --format json` (compose v2+); fall back
    # to `docker inspect` of the container id.
    if docker compose -f docker-compose.yml ps "$service" --format json 2>/dev/null | head -c 1 | grep -q '\['; then
        # JSON array form (compose v2.21+)
        running_image="$(docker compose -f docker-compose.yml ps "$service" --format json \
            | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data[0]["Image"] if data else "")')"
    elif docker compose -f docker-compose.yml ps "$service" --format json 2>/dev/null | head -c 1 | grep -q '{'; then
        # JSONL form (older compose v2)
        running_image="$(docker compose -f docker-compose.yml ps "$service" --format json \
            | python3 -c 'import json,sys; line=sys.stdin.readline(); print(json.loads(line)["Image"] if line else "")')"
    fi

    if [[ -z "$running_image" ]]; then
        local cid
        cid="$(docker compose -f docker-compose.yml ps -q "$service" 2>/dev/null | head -1)"
        if [[ -z "$cid" ]]; then
            echo "FAIL: ${service} container not running" >&2
            return 3
        fi
        running_image="$(docker inspect --format='{{.Config.Image}}' "$cid")"
    fi

    printf '%s\n' "$running_image"
}

rc=0
# VERIFY_SKIP_TAG_CHECK=1 — test-only knob. Skips the docker-tag
# loop entirely so runtime tests (mock HTTP /health) can exercise
# the /health-parse path without a docker daemon. Production never
# sets this; only the test harness at
# tests/v2/test_verify_deployed_image.py.
if [[ "${VERIFY_SKIP_TAG_CHECK:-0}" != "1" ]]; then
    for service in "${SERVICES[@]}"; do
        if ! running_image="$(resolve_running_image "$service")"; then
            rc=3
            continue
        fi

        if [[ "$running_image" != "${IMAGE_NAME}:"* ]]; then
            echo "FAIL: ${service} running image '${running_image}' is not in the ${IMAGE_NAME} family" >&2
            rc=4
            continue
        fi
        running_tag="${running_image#${IMAGE_NAME}:}"

        if [[ "$running_tag" != "$EXPECTED" ]]; then
            cat >&2 <<EOF
FAIL: ${service} deployed image tag does not match expected SHA.
  expected: ${IMAGE_NAME}:${EXPECTED}
  running:  ${IMAGE_NAME}:${running_tag}
EOF
            rc=5
            continue
        fi

        echo "OK: ${service} running ${IMAGE_NAME}:${running_tag}"
    done
fi

# ADR-B3 / D-SB3-3: runtime self-report cross-check. After all
# docker-tag checks pass, fetch /health from chat (8890) and admin
# (8891) and assert the JSON `git_sha` matches ${EXPECTED}. A
# mismatch means the docker tag was bumped but the running process
# is from an earlier image — exactly the failure class that wasted
# runs 2-5 of the 2026-05-22 deploy epic. Skip with WITH_RUNTIME=0
# (offline / unit-test contexts).
#
# D-SB3-3 amendment 2026-05-25 — first prod deploy attempt of S-F1
# exposed two runtime gaps:
#   (Bug 1) verify hit /health once, ~12 s after compose up. chat
#           warmup is ~76 s (chromadb 18 s + v2 dispatch 58 s) — the
#           one-shot curl always failed false. Fix: poll the
#           compose-defined HEALTHCHECK (.State.Health.Status) until
#           "healthy" or 180 s budget exhausted, THEN curl /health.
#           Docker's healthcheck inside the container already gates
#           on /health 200 OK, so the moment status flips to healthy
#           the curl from outside is also good.
#   (Bug 2) `python -c 'json.loads(...)'` crashed on pre-B3 plain
#           "ok" body (rollback target bac0b80 is pre-B3 — /health
#           is plain text there). `set -e` aborted the script; the
#           operator saw "ROLLBACK ALSO FAILED" while the host was
#           in fact fine on bac0b80. Fix: wrap json.loads in
#           try/except — non-JSON body is degraded mode (200 OK is
#           sufficient, git_sha not enforced). GATED to rollback
#           mode only via VERIFY_ALLOW_PRE_B3_DEGRADED=1 (deploy.sh
#           exports this only on its --rollback path). Forward
#           deploys ALWAYS require JSON /health with git_sha; non-
#           JSON in forward mode is exit 8 — the silent-success
#           class that ADR-B4's --expected-sha gate exists to close
#           is left closed. Closes the rollback-to-pre-B3 dead-end
#           without re-opening the forward-deploy silent-success
#           vector.
# VERIFY_HEALTHCHECK_BUDGET_S env (default 600 — S-P2c-followup: the
# E6/E11 page-cache + retrieval touch-warms add ~340s to _warmup) sizes the poll
# budget. Tests in tests/v2/test_verify_deployed_image.py drive
# the poll via a `docker` PATH shim that returns controlled
# `.State.Health.Status` values — no env-knob escape hatch in
# the verifier itself.

# Poll `docker inspect ... .State.Health.Status` until "healthy"
# or the budget runs out. Compose defines a healthcheck on chat
# and admin (docker-compose.yml lines ~161/190): python urllib
# urlopen('http://127.0.0.1:<port>/health', timeout=2), interval
# 30s, retries 3, start_period 60s/30s — so by the time docker
# flips status to "healthy" the runtime curl from outside the
# container also succeeds. Returns 0 on healthy, non-zero on
# timeout or "missing container".
poll_until_healthy() {
    local service="$1"
    local container_name="$2"
    local budget="${VERIFY_HEALTHCHECK_BUDGET_S:-600}"
    local interval=5
    local elapsed=0
    local status

    while [[ $elapsed -lt $budget ]]; do
        # `.State.Health` is absent if no HEALTHCHECK is declared
        # for the image — fall back to "running" so we don't loop
        # forever on services that legitimately have no healthcheck.
        status="$(docker inspect \
            --format='{{if .State.Health}}{{.State.Health.Status}}{{else if .State.Running}}running-no-healthcheck{{else}}stopped{{end}}' \
            "$container_name" 2>/dev/null || echo "missing")"
        case "$status" in
            healthy|running-no-healthcheck)
                return 0
                ;;
            missing)
                echo "FAIL: ${service} container ${container_name} not found by 'docker inspect'" >&2
                return 1
                ;;
            stopped)
                echo "FAIL: ${service} container ${container_name} is not running" >&2
                return 1
                ;;
        esac
        echo "[verify] ${service}: docker health=${status}, waiting (${elapsed}s/${budget}s)" >&2
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done
    echo "FAIL: ${service} did not become healthy within ${budget}s (last status: ${status})" >&2
    return 1
}

if [[ "${WITH_RUNTIME:-1}" == "1" && "$rc" == "0" ]]; then
    # Map service → (port, container-name). container-name is what
    # docker compose uses for the running container (see
    # docker-compose.yml container_name: directives).
    declare -A SVC_PORT=( [chat]=8890 [admin]=8891 )
    declare -A SVC_CONTAINER=( [chat]=wordcracker-chat [admin]=wordcracker-admin )

    for svc in chat admin; do
        port="${SVC_PORT[$svc]}"
        container="${SVC_CONTAINER[$svc]}"

        # Bug-1 fix: wait for compose's healthcheck to flip to
        # healthy before curling /health.
        if ! poll_until_healthy "$svc" "$container"; then
            rc=6
            continue
        fi

        body="$(curl -sf --max-time 5 "http://127.0.0.1:${port}/health" || true)"
        if [[ -z "$body" ]]; then
            # Docker said healthy but curl from host returned empty —
            # genuinely anomalous (the healthcheck itself curls
            # /health, so if it's healthy /health should respond).
            echo "FAIL: ${svc} /health returned empty despite healthy container (port ${port})" >&2
            rc=6
            continue
        fi

        # Bug-2 fix: guard json.loads. Pre-B3 images return plain
        # text "ok" (no JSON envelope) — that's the rollback target
        # shape today. Non-JSON => degraded mode. Otherwise read
        # git_sha out of the JSON dict.
        sha="$(printf '%s' "$body" | python3 -c '
import json, sys
body = sys.stdin.read()
try:
    data = json.loads(body)
except (json.JSONDecodeError, ValueError):
    print("<non-json-body>")
    sys.exit(0)
if isinstance(data, dict):
    print(data.get("git_sha", "<no-git_sha-field>"))
else:
    print("<non-dict-json>")
')"

        case "$sha" in
            "<non-json-body>")
                # Pre-B3 image (e.g. bac0b80) — /health is plain text.
                # GATED: degraded mode is ONLY legitimate in rollback
                # to a pre-B3 target. Forward deploy of a B3+ image
                # must emit JSON with git_sha; non-JSON in forward
                # mode is the silent-success class --expected-sha was
                # meant to close (e.g. B3 init failed, chat process
                # serving a generic error string), and accepting it
                # would re-introduce exactly that failure mode.
                # deploy.sh sets VERIFY_ALLOW_PRE_B3_DEGRADED=1 only
                # on its rollback path (MODE=rollback).
                if [[ "${VERIFY_ALLOW_PRE_B3_DEGRADED:-0}" == "1" ]]; then
                    echo "OK: ${svc} /health returned non-JSON body (pre-B3 rollback target — degraded check, 200 OK sufficient, git_sha not enforced)"
                else
                    cat >&2 <<EOF
FAIL: ${svc} /health returned non-JSON body, but this is a forward deploy.
  B3+ images MUST emit JSON {"git_sha":"...",...} on /health.
  Non-JSON in forward mode is the silent-success class that ADR-B4's
  --expected-sha gate closes — refused here for the same reason.
  body: ${body}
  surface: http://127.0.0.1:${port}/health
  (Degraded mode is set by deploy.sh only on its rollback path
   via VERIFY_ALLOW_PRE_B3_DEGRADED=1; not for forward deploys.)
EOF
                    rc=8
                fi
                ;;
            "<non-dict-json>"|"<no-git_sha-field>")
                # JSON parsed but the shape is wrong (top-level not a
                # dict, or no git_sha key). This is neither pre-B3 nor
                # B3+ — degenerate; fail loud so the bad image can't
                # masquerade as either.
                cat >&2 <<EOF
FAIL: ${svc} /health is JSON but malformed (${sha}).
  body: ${body}
  surface: http://127.0.0.1:${port}/health
EOF
                rc=7
                ;;
            "$EXPECTED")
                echo "OK: ${svc} /health.git_sha=${sha}"
                ;;
            *)
                cat >&2 <<EOF
FAIL: ${svc} /health.git_sha does not match expected SHA.
  expected: ${EXPECTED}
  runtime:  ${sha}
  surface:  http://127.0.0.1:${port}/health
EOF
                rc=7
                ;;
        esac
    done
fi

exit "$rc"
