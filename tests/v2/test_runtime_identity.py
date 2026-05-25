"""S-B3 acceptance: runtime build identity (ADR-B3 / D-SB3-1..D-SB3-3).

These tests are the R2 negative-test gate for ADR-B3 (see
``docs/v2/decisions.md`` → 2026-05-25 S-B3). They pin:

* ``scripts.v2.__version__.get_git_sha`` / ``get_build_time`` read
  ``os.environ`` lazily (so ``monkeypatch.setenv`` works between calls),
  with explicit ``"unknown"`` fallback when the env is missing or
  empty — never ``None``, never silent stand-in.
* ``runtime_identity()`` shape stays ``{status, version, git_sha,
  build_time}`` (the /health JSON contract).
* Toggling ``WC_*`` flags does NOT change ``git_sha`` — the failure
  class the pre-S-B3 version-string-encoded-flags bug introduced.
* The Dockerfile bakes ``ARG GIT_SHA`` + ``ENV GIT_SHA=$GIT_SHA``;
  ``scripts/deploy.sh`` passes ``--build-arg GIT_SHA=...`` to
  ``docker build``; ``docker-compose.yml`` mirrors the build-arg
  for casual rebuild paths.
* ``scripts/verify_deployed_image.sh`` probes ``/health.git_sha`` at
  runtime (D-SB3-3), gated by ``WITH_RUNTIME=0`` to skip in offline
  contexts.
* ``chat_server.py`` / ``admin_server.py`` ``/health`` handler emits
  ``runtime_identity()`` (not bare ``b"ok"``).
* The chat UI header chip carries the short SHA — user-visible signal
  that closes the R14 «версия не подтверждена» concern.

File-level greps follow the same anti-drift pattern as
``test_deploy_artifact.py::test_deploy_sh_uses_scoped_dirty_check``
(D-SB1-8): the assertion catches a future change that removes the
contract surface, even if some other test starts passing for the
wrong reason.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_deployed_image.sh"
CHAT_PY = REPO_ROOT / "scripts" / "chat_server.py"
ADMIN_PY = REPO_ROOT / "scripts" / "admin_server.py"


# ---------------------------------------------------------------------------
# Module-level: __version__.py runtime-identity getters
# ---------------------------------------------------------------------------


@pytest.fixture
def version_module():
    """Import ``scripts.v2.__version__`` fresh. The getters read env
    lazily so reload is not required between tests, but importing here
    ensures the module is loaded under pytest's sys.path."""
    return importlib.import_module("scripts.v2.__version__")


def test_get_git_sha_reads_from_env(version_module, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234deadbeef")
    assert version_module.get_git_sha() == "abc1234deadbeef"


def test_get_git_sha_missing_returns_unknown(version_module, monkeypatch):
    """R2 NOT-X mirror: env not set → explicit 'unknown'.

    Pre-S-B3 the version string had no SHA at all and silently fell
    back to feature-flag-derived phrasing. Post-S-B3 the contract is
    "always a string, never None, never empty"; the explicit
    'unknown' sentinel signals "this is a hand-built image with no
    deploy.sh-passed --build-arg".
    """
    monkeypatch.delenv("GIT_SHA", raising=False)
    sha = version_module.get_git_sha()
    assert sha == "unknown"
    assert sha is not None
    assert sha != ""


def test_get_git_sha_empty_string_treated_as_unknown(version_module, monkeypatch):
    """An accidental empty ``--build-arg GIT_SHA=`` (operator typo)
    must not silently pass through. The surface always reports a
    non-empty string, so downstream code never has to handle ``""``.
    """
    monkeypatch.setenv("GIT_SHA", "")
    assert version_module.get_git_sha() == "unknown"


def test_get_build_time_reads_from_env(version_module, monkeypatch):
    monkeypatch.setenv("BUILD_TIME", "2026-05-25T12:34:56Z")
    assert version_module.get_build_time() == "2026-05-25T12:34:56Z"


def test_get_build_time_missing_returns_unknown(version_module, monkeypatch):
    monkeypatch.delenv("BUILD_TIME", raising=False)
    assert version_module.get_build_time() == "unknown"


def test_get_build_time_empty_string_treated_as_unknown(version_module, monkeypatch):
    monkeypatch.setenv("BUILD_TIME", "")
    assert version_module.get_build_time() == "unknown"


def test_runtime_identity_shape(version_module, monkeypatch):
    """``/health`` JSON contract: exactly these four keys, this order
    of meaning. Adding keys is fine in future blocks; renaming or
    removing breaks the verify_deployed_image.sh probe and the
    ``curl /health | jq -r .git_sha`` operator shortcut.
    """
    monkeypatch.setenv("GIT_SHA", "feedface")
    monkeypatch.setenv("BUILD_TIME", "2026-05-25T00:00:00Z")
    ident = version_module.runtime_identity()
    assert set(ident.keys()) == {"status", "version", "git_sha", "build_time"}
    assert ident["status"] == "ok"
    assert ident["version"] == version_module.ANALYTICS_VERSION
    assert ident["git_sha"] == "feedface"
    assert ident["build_time"] == "2026-05-25T00:00:00Z"


def test_runtime_identity_unknown_defaults(version_module, monkeypatch):
    """No env set → identity reports 'unknown' for SHA + build time,
    not None / missing keys."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)
    ident = version_module.runtime_identity()
    assert ident["git_sha"] == "unknown"
    assert ident["build_time"] == "unknown"
    assert ident["status"] == "ok"


def test_git_sha_independent_of_wc_flags(version_module, monkeypatch):
    """R2: flipping any ``WC_*`` flag does NOT change ``git_sha``.

    The pre-S-B3 chat header chip encoded feature-flag-related
    phrasing (``v3.2.0-alphaX``), so flipping flags could move the
    visible "version" without any actual code change. ADR-B3 sources
    SHA solely from ``os.environ["GIT_SHA"]`` (baked at image build
    time via Dockerfile ARG/ENV), which is invisible to flag toggles.
    """
    monkeypatch.setenv("GIT_SHA", "pinnedsha")
    baseline = version_module.get_git_sha()
    for flag, val in [
        ("WC_CRITIC", "off"),
        ("WC_LLM_MODEL", "qwen3:14b"),
        ("WC_OLLAMA_NUM_CTX", "8192"),
        ("WC_CACHE_AST_INVALIDATION", "on"),
    ]:
        monkeypatch.setenv(flag, val)
        current = version_module.get_git_sha()
        assert current == baseline, (
            f"setting {flag}={val} changed git_sha "
            f"(baseline={baseline!r}, got={current!r})"
        )


# ---------------------------------------------------------------------------
# File-level: Dockerfile bakes ARG/ENV
# ---------------------------------------------------------------------------


def test_dockerfile_declares_git_sha_arg():
    """``ARG GIT_SHA[=default]`` must be present (D-SB3-1)."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG\s+GIT_SHA(=\S+)?\s*$", text, re.MULTILINE), (
        "Dockerfile must declare `ARG GIT_SHA[=default]` (ADR-B3 / D-SB3-1)"
    )


def test_dockerfile_exports_git_sha_env():
    """``ENV GIT_SHA=$GIT_SHA`` must be present so the value lands in
    ``os.environ`` of the running Python process (D-SB3-1).

    Accepts both single-var (``ENV GIT_SHA=$GIT_SHA``) and multi-var
    (``ENV GIT_SHA=$GIT_SHA \\\n    BUILD_TIME=$BUILD_TIME``) forms —
    Docker treats them identically.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"\bENV\b[\s\S]{0,160}\bGIT_SHA=\$GIT_SHA\b", text), (
        "Dockerfile must export `ENV GIT_SHA=$GIT_SHA` so the process "
        "inside reads it from os.environ (ADR-B3 / D-SB3-1)"
    )


def test_dockerfile_declares_build_time_arg():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG\s+BUILD_TIME(=\S+)?\s*$", text, re.MULTILINE), (
        "Dockerfile must declare `ARG BUILD_TIME[=default]` (ADR-B3)"
    )


def test_dockerfile_exports_build_time_env():
    """``ENV BUILD_TIME=$BUILD_TIME`` present in any single- or multi-var
    ENV form."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"\bENV\b[\s\S]{0,160}\bBUILD_TIME=\$BUILD_TIME\b", text), (
        "Dockerfile must export `ENV BUILD_TIME=$BUILD_TIME` (ADR-B3)"
    )


# ---------------------------------------------------------------------------
# File-level: deploy.sh forwards GIT_SHA + BUILD_TIME via --build-arg
# ---------------------------------------------------------------------------


def test_deploy_sh_passes_git_sha_build_arg():
    """``deploy.sh`` must pass ``--build-arg GIT_SHA=...`` to
    ``docker build`` (D-SB3-1).

    Without this the image bakes the Dockerfile default ``"unknown"``
    even for deploys executed through the canonical path — the SHA
    surface would silently misreport. Mirror of
    ``test_deploy_sh_uses_scoped_dirty_check`` (D-SB1-8): the test
    fails if a future refactor removes the build-arg, no matter what
    else passes.
    """
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert re.search(r"--build-arg\s+GIT_SHA=", text), (
        "deploy.sh must pass `--build-arg GIT_SHA=...` to docker build "
        "(ADR-B3 / D-SB3-1)"
    )
    assert re.search(r"--build-arg\s+BUILD_TIME=", text), (
        "deploy.sh must pass `--build-arg BUILD_TIME=...` to docker build "
        "(ADR-B3 / D-SB3-1)"
    )


def test_deploy_sh_build_time_is_utc_iso():
    """``BUILD_TIME`` value is ``date -u`` (UTC) in ISO-8601 form, so
    operators reading ``/health.build_time`` always see UTC."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    # Pattern: BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)" (or equivalent).
    assert re.search(r"BUILD_TIME=.*date\s+-u", text), (
        "deploy.sh must compute BUILD_TIME with `date -u` (UTC), not "
        "local time — otherwise `/health.build_time` interpretation "
        "depends on the host's TZ."
    )


# ---------------------------------------------------------------------------
# File-level: docker-compose.yml mirrors the build-arg for compose-build
# ---------------------------------------------------------------------------


def test_compose_build_args_include_git_sha():
    """``docker-compose.yml``'s ``build.args`` must forward GIT_SHA.

    Closes the dev-only path where someone runs ``docker compose
    build`` (or ``up --build``) instead of ``deploy.sh``. Without the
    compose-side arg, the dev image bakes ``"unknown"`` even though
    ``WC_IMAGE_TAG`` is set in ``.env``. With the arg, the dev image
    carries ``WC_IMAGE_TAG``-derived SHA (or ``"unknown"`` fallback).
    """
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"\bGIT_SHA\b\s*:", text), (
        "docker-compose.yml's build.args must include GIT_SHA so casual "
        "`docker compose build` paths bake a meaningful SHA (ADR-B3)"
    )
    assert re.search(r"\bBUILD_TIME\b\s*:", text), (
        "docker-compose.yml's build.args must include BUILD_TIME (ADR-B3)"
    )


# ---------------------------------------------------------------------------
# File-level: verify_deployed_image.sh runtime probe (D-SB3-3)
# ---------------------------------------------------------------------------


def test_verify_deployed_image_runtime_probe_present():
    """``verify_deployed_image.sh`` must probe ``/health`` and parse
    ``git_sha`` (D-SB3-3). Pre-S-B3 it stopped at the docker-tag
    check; that leaves the "tag correct, process from older image"
    drift class invisible. ADR-B3 closes it with the runtime probe.
    """
    text = VERIFY_SH.read_text(encoding="utf-8")
    assert "/health" in text, (
        "verify_deployed_image.sh must hit /health (ADR-B3 / D-SB3-3)"
    )
    assert "git_sha" in text, (
        "verify_deployed_image.sh must parse `git_sha` out of the "
        "/health JSON (ADR-B3 / D-SB3-3)"
    )


def test_verify_deployed_image_honors_with_runtime_gate():
    """``WITH_RUNTIME=0`` must skip the runtime probe so offline /
    unit-test invocations (services not up) do not falsely fail."""
    text = VERIFY_SH.read_text(encoding="utf-8")
    assert "WITH_RUNTIME" in text, (
        "verify_deployed_image.sh must honour WITH_RUNTIME=0 for "
        "offline contexts (CI / tests where the services are not up)"
    )


# ---------------------------------------------------------------------------
# File-level: /health handlers in chat / admin emit runtime_identity()
# ---------------------------------------------------------------------------


def test_chat_health_handler_calls_runtime_identity():
    """``chat_server.py`` ``/health`` branch must call
    ``runtime_identity()`` (ADR-B3 / D-SB3-2).

    The pre-S-B3 anti-pattern was ``self._send(200, b"ok", "text/plain")``
    in the ``/health`` branch — plain text, no SHA, no way to tell
    "right 200" from "wrong 200" externally.
    """
    text = CHAT_PY.read_text(encoding="utf-8")
    health_block = _slice_health_branch(text)
    assert "runtime_identity" in health_block, (
        "chat_server.py /health branch must call runtime_identity() "
        "(ADR-B3 / D-SB3-2)"
    )


def test_chat_health_handler_no_plain_ok_body():
    """NOT-X mirror: the ``/health`` branch must NOT return plain
    ``b"ok"`` anymore — that's the pre-S-B3 surface."""
    text = CHAT_PY.read_text(encoding="utf-8")
    health_block = _slice_health_branch(text)
    assert 'b"ok"' not in health_block, (
        "chat_server.py /health must not return plain b'ok' (pre-S-B3 shape)"
    )


def test_admin_health_handler_calls_runtime_identity():
    text = ADMIN_PY.read_text(encoding="utf-8")
    health_block = _slice_health_branch(text)
    assert "runtime_identity" in health_block, (
        "admin_server.py /health branch must call runtime_identity() "
        "(ADR-B3 / D-SB3-2)"
    )


def test_admin_health_handler_no_plain_ok_body():
    text = ADMIN_PY.read_text(encoding="utf-8")
    health_block = _slice_health_branch(text)
    assert 'b"ok"' not in health_block, (
        "admin_server.py /health must not return plain b'ok' (pre-S-B3 shape)"
    )


def _slice_health_branch(text: str) -> str:
    """Return a window starting at the ``/health`` branch, big enough to
    cover the handler body (~1 KB to accommodate comments + code).

    The greps above only care about the immediate /health handler body.
    Both servers carry other ``b"ok"`` / non-runtime_identity code
    elsewhere; isolating the window keeps the assertion targeted.
    """
    match = re.search(r'if self\.path == "/health":', text)
    if match is None:
        pytest.fail("server file does not have an `if self.path == \"/health\"` branch")
    start = match.start()
    return text[start:start + 1000]


# ---------------------------------------------------------------------------
# UI: chat header chip carries SHA + build time
# ---------------------------------------------------------------------------


def _safe_import_chat_server():
    """Try to import chat_server; skip the test if heavy deps are
    missing in the test env (rag_query / rag_tools chain). The pure
    pytest harness on dev boxes does carry these, but a minimal CI
    runner might not."""
    try:
        return importlib.import_module("scripts.chat_server")
    except Exception as exc:  # noqa: BLE001 — env-dependent failure
        pytest.skip(f"chat_server import unavailable in this env: "
                    f"{type(exc).__name__}: {exc}")


def test_chat_version_chip_includes_short_git_sha(monkeypatch):
    """``_build_version_strings()`` display includes the short SHA.

    User-visible signal: bumping ``ANALYTICS_VERSION`` without
    rebuilding the image still shows the old SHA → operator sees
    "deploy did not land" from the chat UI alone.
    """
    monkeypatch.setenv("GIT_SHA", "feedface1234567")
    monkeypatch.setenv("BUILD_TIME", "2026-05-25T00:00:00Z")
    cs = _safe_import_chat_server()
    display, tooltip = cs._build_version_strings()
    # `_build_version_strings` truncates to 7-char short SHA (git's
    # default `git rev-parse --short` length).
    assert "feedfac" in display, (
        f"version chip display must include the 7-char short git_sha; "
        f"got display={display!r}"
    )
    assert "feedface1234567" in tooltip, (
        f"version chip tooltip must include the full git_sha; "
        f"got tooltip={tooltip!r}"
    )
    assert "2026-05-25T00:00:00Z" in tooltip, (
        f"tooltip must include build_time; got tooltip={tooltip!r}"
    )


def test_chat_version_chip_unknown_when_env_missing(monkeypatch):
    """R2 NOT-X mirror: no ``GIT_SHA`` env → display shows
    ``unknown``, never silent stand-in or feature-flag-derived
    phrasing."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)
    cs = _safe_import_chat_server()
    display, tooltip = cs._build_version_strings()
    assert "unknown" in display, (
        f"display must contain 'unknown' when GIT_SHA env is absent; "
        f"got {display!r}"
    )
    assert "unknown" in tooltip, (
        f"tooltip must contain 'unknown' when env is absent; got {tooltip!r}"
    )


def test_chat_version_chip_independent_of_wc_flags(monkeypatch):
    """R2: chip content does NOT change when ``WC_*`` flags toggle.

    The chip is rendered solely from ``ANALYTICS_VERSION`` + the
    env-baked SHA + build_time. Flipping any feature flag does not
    touch those inputs — the chip stays byte-identical.
    """
    monkeypatch.setenv("GIT_SHA", "pinned1")
    monkeypatch.setenv("BUILD_TIME", "2026-05-25T00:00:00Z")
    cs = _safe_import_chat_server()
    display_baseline, tooltip_baseline = cs._build_version_strings()
    for flag, val in [
        ("WC_CRITIC", "off"),
        ("WC_LLM_MODEL", "alt-model:99"),
        ("WC_OLLAMA_NUM_CTX", "65536"),
        ("WC_CACHE_AST_INVALIDATION", "on"),
    ]:
        monkeypatch.setenv(flag, val)
        display, tooltip = cs._build_version_strings()
        assert display == display_baseline, (
            f"flipping {flag}={val} changed version chip display "
            f"(baseline={display_baseline!r}, got={display!r})"
        )
        assert tooltip == tooltip_baseline, (
            f"flipping {flag}={val} changed version chip tooltip"
        )
