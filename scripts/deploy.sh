#!/usr/bin/env bash
# scripts/deploy.sh — single-command deploy of wordcracker-textlab.
#
# D-SB1-4 (docs/v2/decisions.md → 2026-05-24 S-B1).
#
# Usage:
#     bash scripts/deploy.sh                 # deploy HEAD
#     bash scripts/deploy.sh <git-ref>       # deploy specific ref
#     bash scripts/deploy.sh --rollback <sha>  # re-run with a previous SHA tag
#     bash scripts/deploy.sh --allow-dirty   # tolerate uncommitted changes (sets tag to SHORT_SHA-dirty)
#
# Steps:
#   1. Resolve target SHA from <ref> or HEAD.
#   2. Build wordcracker-textlab:$SHA (skipped on --rollback if image present).
#   3. Atomically write WC_IMAGE_TAG=$SHA into .env.
#   4. docker compose -f docker-compose.yml up -d --force-recreate gutenberg-lab.
#   5. systemctl restart wordcracker-chat wordcracker-admin (if present).
#   6. scripts/verify_deployed_image.sh $SHA — fail loudly on mismatch.
#   7. Prune all but the last 5 SHA-tagged wordcracker-textlab images.
#
# TODO(ADR-B4): step 0.5 — run scripts/v2/predeploy_check.py here once
# the predeploy harness lands. Until then this script trusts the
# operator's local tests/v2 run.

set -euo pipefail

cd "$(dirname "$0")/.."

# Constants
IMAGE_NAME="wordcracker-textlab"
KEEP_LAST_N_IMAGES=5
ALLOW_DIRTY=0
MODE="deploy"
REF=""

usage() {
    sed -n '2,/^set -euo/p' "$0" | head -20
    exit "${1:-1}"
}

# --- argv parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rollback)
            MODE="rollback"
            REF="${2:-}"
            [[ -n "$REF" ]] || { echo "ERROR: --rollback needs <sha>" >&2; exit 2; }
            shift 2
            ;;
        --allow-dirty)
            ALLOW_DIRTY=1
            shift
            ;;
        -h|--help)
            usage 0
            ;;
        --*)
            echo "ERROR: unknown flag: $1" >&2
            usage 2
            ;;
        *)
            REF="$1"
            shift
            ;;
    esac
done

# --- resolve SHA ---
if [[ "$MODE" == "rollback" ]]; then
    # Rollback uses the SHA as-is; no git resolution. Image must exist.
    SHA="$REF"
    if ! docker image inspect "${IMAGE_NAME}:${SHA}" >/dev/null 2>&1; then
        echo "ERROR: image ${IMAGE_NAME}:${SHA} not found in local store. Cannot rollback." >&2
        echo "       Available tags:" >&2
        docker image ls "${IMAGE_NAME}" --format '  {{.Tag}}' >&2 || true
        exit 3
    fi
    echo "[deploy] mode=rollback target=${SHA}"
else
    # Forward deploy: resolve ref, build image.
    if ! command -v git >/dev/null 2>&1; then
        echo "ERROR: git not found on PATH" >&2
        exit 4
    fi
    REF="${REF:-HEAD}"
    SHA="$(git rev-parse --short "$REF")"
    if [[ -z "$SHA" ]]; then
        echo "ERROR: could not resolve $REF" >&2
        exit 5
    fi

    # Dirty-tree guard.
    if [[ -n "$(git status --porcelain)" ]]; then
        if [[ "$ALLOW_DIRTY" == "1" ]]; then
            SHA="${SHA}-dirty"
            echo "[deploy] WARN: dirty tree, tagging as ${SHA}"
        else
            echo "ERROR: working tree is dirty. Commit, stash, or pass --allow-dirty." >&2
            exit 6
        fi
    fi

    echo "[deploy] mode=forward ref=${REF} sha=${SHA}"
    echo "[deploy] building ${IMAGE_NAME}:${SHA} ..."
    docker build -t "${IMAGE_NAME}:${SHA}" -f Dockerfile .
fi

# --- write .env atomically ---
ENV_FILE=".env"
TMP_ENV="$(mktemp ".env.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

# Preserve any other vars in .env, replace/insert WC_IMAGE_TAG only.
if [[ -f "$ENV_FILE" ]]; then
    grep -v '^WC_IMAGE_TAG=' "$ENV_FILE" > "$TMP_ENV" || true
fi
echo "WC_IMAGE_TAG=${SHA}" >> "$TMP_ENV"
mv "$TMP_ENV" "$ENV_FILE"
trap - EXIT
echo "[deploy] .env updated: WC_IMAGE_TAG=${SHA}"

# --- bring up new container ---
echo "[deploy] docker compose up -d --force-recreate gutenberg-lab ..."
WC_IMAGE_TAG="${SHA}" docker compose -f docker-compose.yml up -d --force-recreate gutenberg-lab

# --- restart systemd units if present ---
if command -v systemctl >/dev/null 2>&1; then
    for unit in wordcracker-chat wordcracker-admin; do
        if systemctl list-unit-files "${unit}.service" --no-legend 2>/dev/null | grep -q "$unit"; then
            echo "[deploy] systemctl restart ${unit}"
            sudo systemctl restart "$unit"
        else
            echo "[deploy] skip: ${unit}.service not installed (run scripts/install_systemd_units.sh)"
        fi
    done
else
    echo "[deploy] skip: systemctl not on PATH (non-prod host?)"
fi

# --- verify ---
echo "[deploy] verifying running image tag ..."
bash scripts/verify_deployed_image.sh "${SHA}"

# --- prune old images, keep last N ---
echo "[deploy] pruning old ${IMAGE_NAME} tags (keep last ${KEEP_LAST_N_IMAGES})..."
# Sort by CreatedAt desc, skip first N, delete the rest.
old_tags="$(docker image ls "${IMAGE_NAME}" --format '{{.Tag}}\t{{.CreatedAt}}' \
    | sort -k2 -r \
    | awk -v keep="${KEEP_LAST_N_IMAGES}" 'NR > keep { print $1 }' \
    | grep -vE '^(dev|latest)$' || true)"
if [[ -n "$old_tags" ]]; then
    echo "$old_tags" | while read -r tag; do
        echo "[deploy] removing ${IMAGE_NAME}:${tag}"
        docker image rm "${IMAGE_NAME}:${tag}" || true
    done
else
    echo "[deploy] nothing to prune"
fi

echo "[deploy] OK — ${IMAGE_NAME}:${SHA} is live"
