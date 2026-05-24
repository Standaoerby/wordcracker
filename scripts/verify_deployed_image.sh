#!/usr/bin/env bash
# scripts/verify_deployed_image.sh — assert the running gutenberg-lab
# container is from the expected SHA-tagged image.
#
# D-SB1-5 (docs/v2/decisions.md → 2026-05-24 S-B1).
#
# Usage:
#     bash scripts/verify_deployed_image.sh              # expects HEAD short-SHA
#     bash scripts/verify_deployed_image.sh <expected>   # expects given tag
#
# Exit 0 on match, non-zero with a clear diff on mismatch.
#
# This is the S-B1 acceptance script. Limited scope: docker-level tag
# match only. Runtime self-report (`/health.git_sha`, footer SHA,
# `ARG GIT_SHA` baked into the image) is ADR-B3 territory.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_NAME="wordcracker-textlab"
SERVICE="gutenberg-lab"

EXPECTED="${1:-}"
if [[ -z "$EXPECTED" ]]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "ERROR: no <expected> arg and git not on PATH" >&2
        exit 2
    fi
    EXPECTED="$(git rev-parse --short HEAD)"
fi

# Resolve the running image of the gutenberg-lab service.
# Prefer `docker compose ps --format json` (compose v2+); fall back to
# `docker inspect` of the container id.

running_image=""
if docker compose -f docker-compose.yml ps "$SERVICE" --format json 2>/dev/null | head -c 1 | grep -q '\['; then
    # JSON array form (compose v2.21+)
    running_image="$(docker compose -f docker-compose.yml ps "$SERVICE" --format json \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data[0]["Image"] if data else "")')"
elif docker compose -f docker-compose.yml ps "$SERVICE" --format json 2>/dev/null | head -c 1 | grep -q '{'; then
    # JSONL form (older compose v2)
    running_image="$(docker compose -f docker-compose.yml ps "$SERVICE" --format json \
        | python3 -c 'import json,sys; line=sys.stdin.readline(); print(json.loads(line)["Image"] if line else "")')"
fi

if [[ -z "$running_image" ]]; then
    # Last-ditch fallback: docker inspect via container id from compose.
    cid="$(docker compose -f docker-compose.yml ps -q "$SERVICE" 2>/dev/null | head -1)"
    if [[ -z "$cid" ]]; then
        echo "FAIL: ${SERVICE} container not running" >&2
        exit 3
    fi
    running_image="$(docker inspect --format='{{.Config.Image}}' "$cid")"
fi

# Strip name prefix; what remains is the tag.
if [[ "$running_image" != "${IMAGE_NAME}:"* ]]; then
    echo "FAIL: running image '${running_image}' is not in the ${IMAGE_NAME} family" >&2
    exit 4
fi
running_tag="${running_image#${IMAGE_NAME}:}"

if [[ "$running_tag" != "$EXPECTED" ]]; then
    cat >&2 <<EOF
FAIL: deployed image tag does not match expected SHA.
  expected: ${IMAGE_NAME}:${EXPECTED}
  running:  ${IMAGE_NAME}:${running_tag}
EOF
    exit 5
fi

echo "OK: ${SERVICE} running ${IMAGE_NAME}:${running_tag}"
