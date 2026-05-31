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
#     bash scripts/deploy.sh --no-probe-gate # skip post-deploy probe gate (emergency only)
#
# Steps (D-SB1-4 + D-SB2-4 + D-SB4-1..3):
#   1. Resolve target SHA from <ref> or HEAD.
#   2. (forward only) Capture PREVIOUS_SHA from the running gutenberg-lab
#      image BEFORE compose up (D-SB4-3) so a red verify/probe-gate has a
#      rollback target by design, not by hope.
#   3. Build wordcracker-textlab:$SHA (skipped on --rollback if image present).
#   4. Atomically write WC_IMAGE_TAG=$SHA into .env.
#   5. docker compose -f docker-compose.yml up -d --force-recreate
#      gutenberg-lab chat admin. After S-B2 (D-SB2-4) this is the ONLY
#      supervision mechanism — chat/admin are compose services, not
#      systemd-managed `docker exec` clients, so no systemctl restart
#      loop is needed. wordcracker-status (host-side) is not touched on
#      deploy — its code lives on the host and is not part of any image.
#   6. scripts/verify_deployed_image.sh $SHA — docker-tag (S-B1) +
#      /health.git_sha (S-B3 / D-SB3-3) match on all three services.
#   7. (forward only) Probe-gate: predeploy_probe_suite.py against the
#      live chat endpoint with --expected-sha $SHA (D-SB4-1). The
#      --expected-sha check is the deploy-time integration of S-B3's
#      runtime identity surface: 12 probes ran against the wrong image
#      would mean 12/12 PASS against a silent-failure deploy. Refused.
#   8. On red verify OR red probe-gate (forward only) AND PREVIOUS_SHA
#      captured at step 2: roll back to PREVIOUS_SHA (compose up + verify)
#      and exit non-zero. If PREVIOUS_SHA is unavailable (cold start /
#      re-deploy of same SHA): exit non-zero WITHOUT rollback so the
#      operator sees an unambiguous "no rollback target".
#   8b. (forward only, S-B7 F2-DEPLOY-RERECORD) Fixture re-record gate:
#      after verify+probe-gate pass, spin up an ephemeral dev-overlay
#      container and re-run `record_fixtures --skip-heavy`, then
#      `git diff` the recorded fixtures (manifest excluded — its
#      elapsed_s/size_bytes are non-deterministic). Any drift (or a
#      recorder timeout/error) is ADVISORY: it prints a loud WARNING and
#      the deploy continues — the service is already verify+probe green and
#      live, so this is a fix-forward heads-up, not a rollback trigger. This
#      is layer 4 of the stale-fixture defence named in
#      tests/v2/test_v1_contracts.py::FixtureFreshnessGate — it catches
#      depth>=2 / lazy-import v1 edits that the PR-time AST fingerprint
#      cannot reach. Heavy full-corpus scans are skipped so the gate
#      cannot hang the deploy (see HEAVY_BINDINGS in live_args.py).
#   9. Prune all but the last 5 SHA-tagged wordcracker-textlab images.

set -euo pipefail

cd "$(dirname "$0")/.."

# Constants
IMAGE_NAME="wordcracker-textlab"
KEEP_LAST_N_IMAGES=5
ALLOW_DIRTY=0
SKIP_PROBE_GATE=0
MODE="deploy"
REF=""
PYTHON="${PYTHON:-python3}"
CHAT_BASE_URL="${CHAT_BASE_URL:-http://127.0.0.1:8890}"
# S-B7 F2-DEPLOY-RERECORD: hard wall-clock backstop for the fixture
# re-record gate. --skip-heavy already drops the multi-minute corpus
# scans (HEAVY_BINDINGS), so the remaining sweep is well under a minute
# on prod; 600s is a generous ceiling that still guarantees the gate
# cannot hang a deploy indefinitely if a tool wedges. Override per-host
# with WC_RERECORD_BUDGET_SECS.
RERECORD_BUDGET_SECS="${WC_RERECORD_BUDGET_SECS:-600}"

usage() {
    sed -n '2,/^set -euo/p' "$0" | head -30
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
        --no-probe-gate)
            # D-SB4-1: emergency escape hatch. The probe gate normally
            # asserts /health.git_sha + runs the 12-probe suite against
            # the freshly-deployed runtime; --no-probe-gate disables the
            # post-deploy probe phase only (the docker-tag + /health.git_sha
            # verify step is NOT skipped — that one is what blocks the
            # silent-failure deploy class and is non-negotiable).
            SKIP_PROBE_GATE=1
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

# --- capture PREVIOUS_SHA for auto-rollback (D-SB4-3, forward mode only) ---
# Read the image tag of the currently-running gutenberg-lab container
# BEFORE compose up replaces it. The tag survives in the local image
# store (KEEP_LAST_N_IMAGES=5 below preserves it), so we have a
# rollback target by construction — not by hope. Falls through to "no
# target" cleanly on cold start / non-wordcracker image / re-deploy of
# the same SHA. The rollback block at the end of this script refuses
# to invoke `--rollback` against an empty target, so the operator
# always sees an unambiguous final state.
PREVIOUS_SHA=""
PREVIOUS_SHA_SOURCE=""
if [[ "$MODE" == "deploy" ]]; then
    prev_cid="$(docker ps --filter "name=^gutenberg-lab$" --format '{{.ID}}' 2>/dev/null | head -1 || true)"
    if [[ -n "$prev_cid" ]]; then
        prev_image="$(docker inspect --format='{{.Config.Image}}' "$prev_cid" 2>/dev/null || true)"
        if [[ "$prev_image" == "${IMAGE_NAME}:"* ]]; then
            prev_candidate="${prev_image#${IMAGE_NAME}:}"
            if [[ -z "$prev_candidate" ]]; then
                echo "[deploy] note: previous image tag is empty — no rollback target" >&2
            elif [[ "$prev_candidate" == "$SHA" ]]; then
                echo "[deploy] note: re-deploying same SHA (${SHA}); no rollback target"
            elif docker image inspect "${IMAGE_NAME}:${prev_candidate}" >/dev/null 2>&1; then
                PREVIOUS_SHA="$prev_candidate"
                PREVIOUS_SHA_SOURCE="docker inspect ${prev_cid:0:12}"
                echo "[deploy] rollback target captured: ${IMAGE_NAME}:${PREVIOUS_SHA}"
            else
                echo "[deploy] note: running image '${prev_image}' has no entry in local image store — rollback unavailable" >&2
            fi
        else
            echo "[deploy] note: running gutenberg-lab image '${prev_image}' is not in the wordcracker-textlab family — no rollback target" >&2
        fi
    else
        echo "[deploy] note: no running gutenberg-lab container — cold start, no rollback target"
    fi
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

# --- verify (D-SB1-5 + D-SB3-3) ---
# verify_deployed_image.sh checks BOTH surfaces of build identity:
#  (a) each compose service's running image tag == $SHA (S-B1)
#  (b) /health.git_sha on chat:8890 and admin:8891 == $SHA (S-B3)
# Failure here means the deploy did NOT stick — drop into rollback
# instead of aborting flat (a flat abort under set -e would leave
# prod on the half-deployed state with no recovery action).
#
# D-SB3-3 amendment 2026-05-25: rollback to a pre-B3 target (e.g.
# `bac0b80` returns /health as plain "ok", no JSON) must not crash
# verify. The verify script supports a degraded mode for non-JSON
# /health, but it is GATED — only legitimate when rolling back to
# an image that predates the JSON surface. We set the gate env var
# here in MODE=rollback only; forward deploys never accept non-JSON
# /health (silent-success class otherwise — a B3 init failure where
# chat starts but serves a non-JSON error would pass verify).
echo "[deploy] verifying running image tag + /health.git_sha ..."
verify_rc=0
if [[ "$MODE" == "rollback" ]]; then
    VERIFY_ALLOW_PRE_B3_DEGRADED=1 \
        bash scripts/verify_deployed_image.sh "${SHA}" || verify_rc=$?
else
    bash scripts/verify_deployed_image.sh "${SHA}" || verify_rc=$?
fi

# --- rollback-mode strict-exit on verify failure (D-SB4-3) ---
# In MODE=rollback the probe-gate AND the rollback-decision blocks
# below both gate on MODE=="deploy" — so without this guard a red
# verify in rollback mode would fall through to the prune step and
# print "OK". That is exactly the silent-success-on-failure class S-B4
# is meant to close. A failed rollback is the loudest possible
# failure: surface the verify exit code immediately to the parent,
# which will report exit 11 ("rollback ALSO failed; host is in a bad
# state").
if [[ "$MODE" == "rollback" && "$verify_rc" -ne 0 ]]; then
    echo "[deploy] ROLLBACK verify failed (rc=${verify_rc}) — host is in a bad state" >&2
    echo "[deploy]   rolled-back tag: ${IMAGE_NAME}:${SHA}" >&2
    echo "[deploy]   available tags:" >&2
    docker image ls "${IMAGE_NAME}" --format '    {{.Tag}}' >&2 || true
    exit "$verify_rc"
fi

# --- probe gate (D-SB4-1, forward mode only) ---
# The probe gate is the second half of "is the deploy actually correct".
# It asserts /health.git_sha equals the SHA we just built AND runs the
# 12-probe taxonomy suite against the live chat endpoint. Without the
# --expected-sha check, all 12 probes can be 12/12 PASS against the
# *previous* image after a silent-failure recreate — exactly the
# failure class of runs 2-5 of the 2026-05-22 deploy epic. Refused.
#
# --no-require-version-bump because deploy.sh is post-build: by the
# time we get here we've already decided to ship this SHA. The
# version-bump assertion belongs in CI (`check_version_bump.py`
# against the PR base). Keeping it on here would just noise rollback
# paths that legitimately step the label backwards.
probe_rc=0
if [[ "$verify_rc" -eq 0 && "$MODE" == "deploy" && "$SKIP_PROBE_GATE" -eq 0 ]]; then
    echo "[deploy] probe-gate: ${PYTHON} scripts/predeploy_probe_suite.py --base-url ${CHAT_BASE_URL} --expected-sha ${SHA}"
    "${PYTHON}" scripts/predeploy_probe_suite.py \
        --base-url "${CHAT_BASE_URL}" \
        --expected-sha "${SHA}" \
        --no-require-version-bump \
        || probe_rc=$?
elif [[ "$MODE" == "deploy" && "$SKIP_PROBE_GATE" -eq 1 ]]; then
    echo "[deploy] probe-gate SKIPPED (--no-probe-gate) — emergency escape only"
fi

# --- rollback on red verify OR red probe-gate (forward mode only) ---
if [[ "$MODE" == "deploy" && ( "$verify_rc" -ne 0 || "$probe_rc" -ne 0 ) ]]; then
    echo "[deploy] FAIL — verify_rc=${verify_rc} probe_rc=${probe_rc}" >&2
    if [[ -n "$PREVIOUS_SHA" ]]; then
        echo "[deploy] rolling back to ${IMAGE_NAME}:${PREVIOUS_SHA} (source: ${PREVIOUS_SHA_SOURCE})" >&2
        # Self-invoke in rollback mode. That path re-writes .env,
        # re-runs compose up, re-runs verify against PREVIOUS_SHA. The
        # rollback branch never reaches THIS block (guard above), so no
        # recursion. We always exit non-zero from here — a deploy that
        # had to roll back is, by definition, not "OK".
        rollback_rc=0
        bash "$0" --rollback "${PREVIOUS_SHA}" || rollback_rc=$?
        if [[ "$rollback_rc" -eq 0 ]]; then
            echo "[deploy] rolled back to ${PREVIOUS_SHA}; target ${SHA} did not stick — host is on the previous SHA" >&2
            exit 10
        else
            echo "[deploy] ROLLBACK ALSO FAILED (rc=${rollback_rc}); host is in a bad state — manual recovery required" >&2
            echo "[deploy]   try: bash scripts/deploy.sh --rollback <known-good-sha>" >&2
            echo "[deploy]   available tags:" >&2
            docker image ls "${IMAGE_NAME}" --format '    {{.Tag}}' >&2 || true
            exit 11
        fi
    else
        echo "[deploy] no rollback target captured — manual recovery required" >&2
        echo "[deploy]   .env points at WC_IMAGE_TAG=${SHA} but verify/probe-gate red" >&2
        echo "[deploy]   available tags:" >&2
        docker image ls "${IMAGE_NAME}" --format '    {{.Tag}}' >&2 || true
        exit 12
    fi
fi

# --- fixture re-record gate (S-B7 F2-DEPLOY-RERECORD, forward mode only) ---
# Reached only when verify AND probe-gate are green (a red either one
# rolled back + exited above). Closes the manual-re-record step that
# bit twice on E27: re-run record_fixtures against the just-deployed
# image and fail the deploy if the committed contract fixtures drifted.
#
# Mechanism: an ephemeral dev-overlay container (`run --rm`). The dev
# overlay (docker-compose.dev.yml) bind-mounts ./scripts, so fixtures
# the recorder writes inside the container land on the host repo and a
# host-side `git diff` sees them. The container also inherits the prod
# /data/* corpus + chroma bind-mounts from the base file, so v1 runs
# against real corpus. WC_IMAGE_TAG=$SHA pins the overlay to the image
# we just shipped. `run --rm` does NOT touch the running gutenberg-lab /
# chat / admin services.
#
# --skip-heavy drops the HEAVY_BINDINGS full-corpus scans (recorder
# prints which) so the gate runs in well under a minute; `timeout` is a
# hard backstop so a wedged tool fails the gate instead of hanging the
# deploy. The diff EXCLUDES _manifest.json: its elapsed_s / size_bytes
# are non-deterministic and would false-fail every deploy. The fixture
# JSON content carries the real drift signal (and is exactly what the
# AST-fingerprint gate at depth>=2 cannot see).
#
# ADVISORY gate (does NOT exit/rollback): a re-record failure (timeout or
# recorder error) or a fixture-drift finding prints a loud WARNING and the
# deploy continues. Rationale: the service is already verify+probe green and
# live by this point, so a drift signal is a "fix-forward, re-record soon"
# heads-up, not a reason to abort a healthy deploy. Crucially, the recorder
# can hit the deploy's `timeout` wall (rc=124) even on a fully-successful
# recording (the v1 tools leave a non-daemon chromadb-telemetry thread that
# hangs interpreter shutdown — the os._exit fix in record_fixtures.py closes
# this, but the WARN keeps a future regression from ever blocking a deploy).
# The drift signal keeps its full value as a logged warning.
RERECORD_FIXTURES_DIR="scripts/v2/contracts/fixtures"
if [[ "$MODE" == "deploy" ]]; then
    echo "[deploy] fixture re-record gate (advisory): ephemeral dev-overlay container, record_fixtures --skip-heavy (budget ${RERECORD_BUDGET_SECS}s)"
    rerecord_rc=0
    WC_IMAGE_TAG="${SHA}" timeout "${RERECORD_BUDGET_SECS}" \
        docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm -T -w /workspace gutenberg-lab \
        "${PYTHON}" -m scripts.v2.contracts.record_fixtures --skip-heavy \
        || rerecord_rc=$?
    if [[ "$rerecord_rc" -ne 0 ]]; then
        echo "[deploy] WARN: fixture re-record gate — recorder errored or timed out (rc=${rerecord_rc}, >${RERECORD_BUDGET_SECS}s). ADVISORY: deploy continues (service is already verify+probe green and live)." >&2
        # Restore any partial fixture writes so the working tree stays
        # clean for the NEXT deploy's dirty-check (a left-dirty tracked
        # file would block it).
        git checkout -- "${RERECORD_FIXTURES_DIR}" 2>/dev/null || true
    # git diff over the fixture JSONs, EXCLUDING the volatile manifest.
    elif ! git diff --exit-code -- "${RERECORD_FIXTURES_DIR}" ":(exclude)${RERECORD_FIXTURES_DIR}/_manifest.json"; then
        echo "[deploy] WARN: FIXTURE DRIFT — committed v1 contract fixtures look stale vs live v1 output. ADVISORY: deploy continues; re-record soon:" >&2
        git --no-pager diff --stat -- "${RERECORD_FIXTURES_DIR}" ":(exclude)${RERECORD_FIXTURES_DIR}/_manifest.json" >&2
        echo "[deploy]   re-record on this host and commit:" >&2
        echo "[deploy]     docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm gutenberg-lab python -m scripts.v2.contracts.record_fixtures" >&2
        echo "[deploy]     git add ${RERECORD_FIXTURES_DIR} && git commit -m 'chore: re-record v1 fixtures'" >&2
        git checkout -- "${RERECORD_FIXTURES_DIR}" 2>/dev/null || true
    else
        # Clean state: discard the re-recorded files (including the volatile
        # manifest) so the deployed host's tree matches HEAD exactly.
        git checkout -- "${RERECORD_FIXTURES_DIR}" 2>/dev/null || true
        echo "[deploy] fixture re-record gate OK — no contract drift"
    fi
fi

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
