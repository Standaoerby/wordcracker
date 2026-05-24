#!/usr/bin/env bash
# W-18 pre-deploy gate — runs in front of `docker compose up` /
# `systemctl restart wordcracker-chat`. Exits non-zero if any of the
# three checks fails, so the deploy step (whatever drives this) blocks
# the rollout. Stdlib-only Python — no extra deps on the deploy host.
#
# Pipeline (each step is a hard gate):
#
#   1. version-bump vs baseline (catches the 2026-05-24 failure mode
#      — deploy went out without ANALYTICS_VERSION moving).
#   2. 12-probe taxonomy suite against the live chat API (catches
#      any PASS->FAIL regression vs the previous baseline).
#
# Exit codes propagate from the underlying tools (1 / 2 / 3 / 4 / 5)
# so a calling deploy script can map them with a single `case`.
#
# Usage on the prod host (Ubuntu):
#
#     # default: probes against https://slovoeb.net, baseline-vs-current bump
#     ./scripts/predeploy_gate.sh
#
#     # local container at 127.0.0.1:8890
#     WC_PROBE_BASE_URL=http://127.0.0.1:8890 ./scripts/predeploy_gate.sh
#
#     # skip the live probe run (only the bump check — useful in CI)
#     ./scripts/predeploy_gate.sh --no-probes
#
#     # update baseline if everything is clean
#     ./scripts/predeploy_gate.sh --update-baseline
#
# Environment:
#
#     WC_PROBE_BASE_URL — chat API root (default https://slovoeb.net)
#     PYTHON            — python interpreter (default: python3)
set -eu

# --------------------------------------------------------------------------
# Locate repo + interpreter
# --------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
BASE_URL="${WC_PROBE_BASE_URL:-https://slovoeb.net}"

# --------------------------------------------------------------------------
# CLI parsing — pass-through unknown flags to the probe runner
# --------------------------------------------------------------------------

NO_PROBES=0
PROBE_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --no-probes) NO_PROBES=1 ;;
    *)           PROBE_ARGS+=("$arg") ;;
  esac
done

# --------------------------------------------------------------------------
# 1. Mandatory version-bump gate
# --------------------------------------------------------------------------

echo "[predeploy-gate] step 1/2: version-bump check (vs baseline)" >&2
"$PYTHON" "$REPO_ROOT/scripts/check_version_bump.py" --against baseline || {
  rc=$?
  echo "[predeploy-gate] BLOCKED — version-bump check failed (exit $rc)" >&2
  exit $rc
}

# --------------------------------------------------------------------------
# 2. 12-probe taxonomy suite vs baseline
# --------------------------------------------------------------------------

if [ "$NO_PROBES" -eq 1 ]; then
  echo "[predeploy-gate] step 2/2: 12-probe suite SKIPPED (--no-probes)" >&2
  echo "[predeploy-gate] OK" >&2
  exit 0
fi

echo "[predeploy-gate] step 2/2: 12-probe suite against $BASE_URL" >&2
"$PYTHON" "$REPO_ROOT/scripts/predeploy_probe_suite.py" \
  --base-url "$BASE_URL" \
  "${PROBE_ARGS[@]}" || {
  rc=$?
  echo "[predeploy-gate] BLOCKED — probe suite failed (exit $rc)" >&2
  exit $rc
}

echo "[predeploy-gate] OK — all gates green, safe to deploy" >&2
