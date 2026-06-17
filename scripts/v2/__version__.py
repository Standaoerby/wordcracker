"""Runtime identity surface for the wordcracker engine.

ANALYTICS_VERSION is the human-readable version label of the v2 stack
(planner→router→renderer→critic) — bumped by hand on each release; the
predeploy harness blocks deploys that didn't bump it.

GIT_SHA and BUILD_TIME are sourced from the process environment. In
prod they are baked into the container image at build time via
`ARG GIT_SHA` / `ARG BUILD_TIME` (Dockerfile) and `--build-arg`
(scripts/deploy.sh) — see docs/v2/decisions.md → 2026-05-25 S-B3
(ADR-B3). Outside a built image (dev shell, tests, ad-hoc python) both
resolve to the explicit string "unknown" — never None, never empty,
never a feature-flag-derived substitute.

`runtime_identity()` is the dict served by `/health` on chat (port
8890) and admin (port 8891). The chat UI header chip pulls the same
short SHA from `get_git_sha()`.

The two value-getters are functions, not module-level constants, so
tests can `monkeypatch.setenv("GIT_SHA", ...)` between calls without
an import-order dance. In prod the env is set in the image layer and
process-lifetime constant, so the tiny per-call overhead is invisible.
"""
import os


ANALYTICS_VERSION = "2.7.25"



_UNKNOWN = "unknown"


def get_git_sha() -> str:
    """Return the GIT_SHA baked into the image, or 'unknown'."""
    return os.environ.get("GIT_SHA") or _UNKNOWN


def get_build_time() -> str:
    """Return the BUILD_TIME baked into the image, or 'unknown'."""
    return os.environ.get("BUILD_TIME") or _UNKNOWN


def runtime_identity() -> dict:
    """Payload for `/health` and any UI surface that shows build identity.

    Shape pinned by tests/v2/test_runtime_identity.py — adding keys is
    fine, removing or renaming breaks /health consumers (verify
    script + manual `curl /health | jq`).
    """
    return {
        "status":     "ok",
        "version":    ANALYTICS_VERSION,
        "git_sha":    get_git_sha(),
        "build_time": get_build_time(),
    }
