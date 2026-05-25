#!/usr/bin/env bash
# scripts/deploy.sh — single-command deploy of wordcracker-textlab.
#
# D-SB1-4 + D-SB2-4 (docs/v2/decisions.md → 2026-05-24 S-B1 / S-B2).
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
#   4. docker compose -f docker-compose.yml up -d --force-recreate
#      gutenberg-lab chat admin. After S-B2 (D-SB2-4) this is the ONLY
#      supervision mechanism — chat/admin are compose services, not
#      systemd-managed `docker exec` clients, so no systemctl restart
#      loop is needed. wordcracker-status (host-side) is not touched on
#      deploy — its code lives on the host and is not part of any image.
#   5. scripts/verify_deployed_image.sh $SHA — fail loudly on mismatch.
#   6. Prune all but the last 5 SHA-tagged wordcracker-textlab images.
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

    # --- pre-flight dirty-check, scoped to image-relevant paths (D-SB1-8) ---
    # Block when the working tree could diverge from the deployed image:
    #   (a) ANY modified/staged tracked file (`git diff HEAD --`) — these
    #       are part of any future commit and would silently diverge prod
    #       from the SHA the build is tagged with.
    #   (b) Untracked files INSIDE paths the Dockerfile actually COPYs
    #       into the image (parsed from Dockerfile, not hardcoded). An
    #       untracked file outside the COPY-set does not enter the image
    #       and cannot diverge prod — so it does not block.
    # --allow-dirty stays the escape hatch and tags the build <sha>-dirty.
    # Algorithm mirror: tests/v2/test_deploy_artifact.py D-SB1-8 cases.

    # Parse COPY sources from Dockerfile (line-based; no continuations).
    # `COPY [--flag=...] src1 [src2 ...] dest` → emit src1, src2, ...
    mapfile -t COPY_SOURCES < <(awk '
        /^COPY[[:space:]]/ {
            for (i = 2; i < NF; i++) {
                if ($i ~ /^--/) continue
                print $i
            }
        }
    ' Dockerfile)

    if [[ "${#COPY_SOURCES[@]}" -eq 0 ]]; then
        echo "ERROR: parsed zero COPY sources from Dockerfile; refusing to skip dirty-check" >&2
        exit 7
    fi

    dirty_blockers=()

    # (a) modified/staged tracked anywhere
    if ! git diff --quiet HEAD --; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && dirty_blockers+=("modified/staged: $line")
        done < <(git diff --name-only HEAD --)
    fi

    # (b) untracked inside COPY paths only
    while IFS= read -r line; do
        [[ -n "$line" ]] && dirty_blockers+=("untracked in COPY scope: $line")
    done < <(git ls-files --others --exclude-standard -- "${COPY_SOURCES[@]}")

    if [[ "${#dirty_blockers[@]}" -gt 0 ]]; then
        if [[ "$ALLOW_DIRTY" == "1" ]]; then
            SHA="${SHA}-dirty"
            echo "[deploy] WARN: image-relevant paths are dirty (${#dirty_blockers[@]} blocker(s)), tagging as ${SHA}" >&2
            printf '[deploy]   %s\n' "${dirty_blockers[@]}" >&2
        else
            echo "ERROR: image-relevant paths are dirty (would diverge prod from HEAD):" >&2
            printf '  %s\n' "${dirty_blockers[@]}" >&2
            echo "Commit, stash, or pass --allow-dirty (tags <sha>-dirty)." >&2
            echo "Scope: COPY sources from Dockerfile = ${COPY_SOURCES[*]}" >&2
            exit 6
        fi
    fi

    echo "[deploy] mode=forward ref=${REF} sha=${SHA}"
    # ADR-B3 / D-SB3-1: bake GIT_SHA + BUILD_TIME into the image so
    # the running process can self-report identity at /health and in
    # the UI. BUILD_TIME is UTC ISO-8601 (second precision). Both
    # values are visible at `docker inspect --format='{{.Config.Env}}'`
    # and at `curl /health | jq -r .git_sha`.
    BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "[deploy] building ${IMAGE_NAME}:${SHA} (build_time=${BUILD_TIME}) ..."
    docker build \
        --build-arg GIT_SHA="${SHA}" \
        --build-arg BUILD_TIME="${BUILD_TIME}" \
        -t "${IMAGE_NAME}:${SHA}" -f Dockerfile .
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

# --- bring up new containers ---
# D-SB2-4: single mechanism. `--force-recreate` recreates all three app
# services with the new image tag in one command. No systemctl follow-up
# is needed because chat/admin are compose services (D-SB2-6), not
# `docker exec` clients of gutenberg-lab. Services are named explicitly
# to prevent the footgun where `up -d --force-recreate gutenberg-lab`
# alone would silently leave chat/admin on the old image.
echo "[deploy] docker compose up -d --force-recreate gutenberg-lab chat admin ..."
WC_IMAGE_TAG="${SHA}" docker compose -f docker-compose.yml up -d --force-recreate gutenberg-lab chat admin

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
