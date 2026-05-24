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
#     bash scripts/verify_deployed_image.sh              # expects HEAD short-SHA
#     bash scripts/verify_deployed_image.sh <expected>   # expects given tag
#
# Exit 0 on match across all services; non-zero on first mismatch.
#
# This is the S-B1 + S-B2 acceptance script. Limited scope: docker-level
# tag match only. Runtime self-report (`/health.git_sha`, footer SHA,
# `ARG GIT_SHA` baked into the image) is a separate follow-up block.

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
    if ! command -v git >/dev/null 2>&1; then
        echo "ERROR: no <expected> arg and git not on PATH" >&2
        exit 2
    fi
    EXPECTED="$(git rev-parse --short HEAD)"
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

exit "$rc"
