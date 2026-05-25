"""R2 negative gates for verify_deployed_image.sh runtime path.

Closes two bugs surfaced 2026-05-25 by the first prod S-F1 deploy
attempt (host healthy on rolled-back bac0b80, but verify blocked
the deploy):

* **Bug 1.** verify_deployed_image.sh hit /health ONCE immediately
  after `compose up` (~12 s elapsed). chat warmup is ~76 s
  (chromadb 18 s + v2 dispatch 58 s) — the one-shot curl never
  caught the responding window. Forward deploys failed → auto-
  rollback fired.

* **Bug 2.** verify crashed under `set -euo pipefail` on the
  rollback target's /health body: bac0b80 is a pre-B3 image and
  returns plain text "ok" (no JSON envelope). The inline
  `python -c 'json.loads(...)'` raised JSONDecodeError; the shell
  aborted; the operator saw "ROLLBACK ALSO FAILED" while the host
  was in fact fine on bac0b80.

Fix (docs/v2/decisions.md → D-SB3-3 amendment 2026-05-25):

* `poll_until_healthy(service, container_name)` — `docker inspect
  ... .State.Health.Status` polled until "healthy" or
  `VERIFY_HEALTHCHECK_BUDGET_S` (default 180) seconds. Compose's
  HEALTHCHECK already gates on /health 200 OK from inside the
  container, so the moment status flips healthy the curl from
  outside also succeeds.

* `json.loads` wrapped in try/except. Non-JSON body → "pre-B3
  degraded" — 200 OK is the contract that existed before B3, and
  forcing JSON on rollback targets makes any pre-B3 image
  un-verifiable. JSON-but-malformed (top-level not a dict, or no
  git_sha) → loud fail (rc=7).

These tests use a local HTTP server on 8890/8891 + a controlled
`docker` PATH shim. They are Linux-only because (a) bash scripts
need bash, (b) the polling-progression test uses a tmpdir-shim
trick that's awkward on Windows shells. The S-B5 quarantine
discipline applies: explicit @skipif with a reason, no silent
skip.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SH = REPO_ROOT / "scripts" / "verify_deployed_image.sh"


# Linux-only: bash scripts + PATH shim. The CI runner (ubuntu-latest)
# covers this; local dev on Windows is the documented quarantine
# (S-B5 ADR-B7 — explicit skipif + reason, no silent skip).
_LINUX_ONLY = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="verify_deployed_image.sh integration test — needs bash + PATH "
           "shim trick; Linux CI covers (S-B5 quarantine pattern).",
)


# ---------------------------------------------------------------------------
# Static parse gates — these always run, even on Windows. They confirm the
# SHAPE of the fix is present in the script (defensive against a future
# refactor that silently reintroduces the one-shot curl or the unguarded
# json.loads).
# ---------------------------------------------------------------------------


def _read_verify_sh() -> str:
    return VERIFY_SH.read_text(encoding="utf-8")


def test_verify_has_poll_until_healthy_function():
    """Bug 1 fix shape: a polling function over docker healthcheck
    is defined. Pin the function name + the docker inspect call
    inside it so a refactor that drops polling fails this test."""
    text = _read_verify_sh()
    assert "poll_until_healthy" in text, (
        "verify_deployed_image.sh must define poll_until_healthy() — "
        "the one-shot curl after compose up was Bug 1 (2026-05-25 "
        "first prod deploy attempt: chat warmup ~76 s, verify hit "
        "at ~12 s and false-failed)."
    )
    # The polling body must read docker's own healthcheck status.
    assert "State.Health.Status" in text, (
        "poll_until_healthy must read .State.Health.Status from "
        "docker inspect — compose-defined HEALTHCHECK already gates "
        "on /health 200 OK; polling that surface inherits the "
        "correct start_period (60 s for chat) and avoids a "
        "redundant /health curl loop."
    )


def test_verify_has_budget_and_sleep():
    """Bug 1 fix shape: the poll has a wall-clock budget and yields
    between iterations. Without `sleep` the loop becomes a hot spin
    and busy-waits the docker socket; without a budget it would
    block deploy indefinitely on a genuinely-stuck container.
    """
    text = _read_verify_sh()
    assert "VERIFY_HEALTHCHECK_BUDGET_S" in text, (
        "poll_until_healthy needs a configurable budget env var "
        "(default 180 s) — pin the name so deploys can override "
        "and tests can shrink it to keep runtimes bounded."
    )
    # Look for `sleep` invocation inside the poll function body
    # (positive: pin that the script does yield between probes).
    assert re.search(r"\bsleep\s+", text), (
        "poll_until_healthy must `sleep` between iterations — "
        "hot-spin would saturate the docker socket."
    )


def test_verify_guards_json_loads():
    """Bug 2 fix shape: json.loads is wrapped in a try/except. Pre-
    B3 images return plain "ok" on /health; the unguarded
    `python -c 'json.loads(...)'` aborted the script via
    JSONDecodeError + `set -euo pipefail`.
    """
    text = _read_verify_sh()
    # Locate the inline python block(s) that consume the body. There
    # must be at least one `try:` paired with `json.loads` so a
    # future commit cannot silently revert to the unguarded form.
    # Multiline search across heredoc-ish blocks; a simple substring
    # check is enough for this gate.
    assert "JSONDecodeError" in text, (
        "verify_deployed_image.sh must guard json.loads with a "
        "JSONDecodeError handler — pre-B3 rollback targets return "
        "plain text /health, and an unguarded loads under "
        "`set -euo pipefail` aborts the script (Bug 2, 2026-05-25)."
    )


def test_verify_has_degraded_mode_marker():
    """Bug 2 fix shape: degraded mode is explicit and labelled —
    when /health is not JSON, the script logs `pre-B3` or
    `non-JSON`. Future commits that re-tighten the contract
    (e.g. "all targets must serve JSON") flip this test red,
    forcing the trade-off to be re-litigated.
    """
    text = _read_verify_sh()
    assert "non-JSON" in text or "pre-B3" in text, (
        "verify_deployed_image.sh must label its pre-B3 fallback "
        "explicitly so operators reading the log see why git_sha "
        "was skipped (and so a refactor can't silently re-tighten "
        "the contract and lock out rollbacks)."
    )


def test_verify_gates_degraded_mode_to_rollback_env():
    """Critical gate (user-requested 2026-05-25): degraded mode
    is NOT an unconditional fallback — it is gated by an env var
    set ONLY by deploy.sh on its rollback path. Forward deploys
    that hit non-JSON /health must FAIL (exit 8), not degrade.

    This static gate pins:
      (a) the env var name (`VERIFY_ALLOW_PRE_B3_DEGRADED`)
          appears in the script
      (b) a non-zero exit (rc=8) is the alternative path when the
          env is unset
    A regression that drops the gate and re-introduces
    unconditional degrade silently un-closes the silent-success
    class --expected-sha was meant to close in ADR-B4.
    """
    text = _read_verify_sh()
    assert "VERIFY_ALLOW_PRE_B3_DEGRADED" in text, (
        "verify_deployed_image.sh must gate degraded-non-JSON-body "
        "handling behind VERIFY_ALLOW_PRE_B3_DEGRADED. Without the "
        "gate, forward-deploy non-JSON /health silently passes — "
        "exactly the silent-success class --expected-sha closes."
    )
    assert "rc=8" in text, (
        "the forward-mode rejection of non-JSON /health must set "
        "rc=8 (distinct from rc=6 'curl empty' and rc=7 'sha "
        "mismatch') so operators can distinguish failure modes."
    )


def test_deploy_sh_sets_degrade_env_only_in_rollback():
    """The matching half of the gate: deploy.sh sets
    `VERIFY_ALLOW_PRE_B3_DEGRADED=1` only inside its MODE=rollback
    branch, never on the forward path.

    Greps deploy.sh for the env-var assignment and asserts it
    appears immediately under an `if [[ "$MODE" == "rollback" ]]`
    block. A future refactor that hoists the env into a shared
    section above (i.e. setting it for both forward AND rollback
    invocations) flips this red — degrade is rollback-only by
    contract.
    """
    deploy_sh = REPO_ROOT / "scripts" / "deploy.sh"
    text = deploy_sh.read_text(encoding="utf-8")
    assert "VERIFY_ALLOW_PRE_B3_DEGRADED=1" in text, (
        "deploy.sh must set VERIFY_ALLOW_PRE_B3_DEGRADED=1 when "
        "calling verify on its rollback path (otherwise rollback "
        "to a pre-B3 target fails on the non-JSON /health body, "
        "which is exactly the bug this amendment exists to fix)."
    )
    # Locate the env-var assignment line and confirm the immediately
    # preceding 12 lines contain a MODE==rollback condition.
    lines = text.splitlines()
    target_idx = None
    for i, line in enumerate(lines):
        if "VERIFY_ALLOW_PRE_B3_DEGRADED=1" in line:
            target_idx = i
            break
    assert target_idx is not None
    preceding = "\n".join(lines[max(0, target_idx - 12):target_idx])
    assert re.search(r'MODE.*==\s*"rollback"', preceding), (
        f"VERIFY_ALLOW_PRE_B3_DEGRADED=1 must live inside a "
        f"MODE==rollback conditional. Preceding 12 lines:\n"
        f"{preceding}"
    )


# ---------------------------------------------------------------------------
# Runtime tests — Linux only. Each test spins up a tiny HTTP server on
# 8890/8891 with a controlled body + a `docker` PATH shim that returns a
# controlled `.State.Health.Status`. The script's docker-tag loop is bypassed
# via VERIFY_SKIP_TAG_CHECK=1 (test-only env knob).
# ---------------------------------------------------------------------------


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(body: bytes, delay_s: float = 0.0):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            if delay_s > 0:
                time.sleep(delay_s)
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args, **kwargs):  # noqa: ARG002
            return  # silence
    return _H


def _start_health_servers(body_chat: bytes, body_admin: bytes,
                          delay_s: float = 0.0):
    s_chat = _ThreadedHTTPServer(
        ("127.0.0.1", 8890), _make_handler(body_chat, delay_s),
    )
    s_admin = _ThreadedHTTPServer(
        ("127.0.0.1", 8891), _make_handler(body_admin, delay_s),
    )
    threading.Thread(target=s_chat.serve_forever, daemon=True).start()
    threading.Thread(target=s_admin.serve_forever, daemon=True).start()
    return (s_chat, s_admin)


def _stop_servers(servers) -> None:
    for s in servers:
        s.shutdown()
        s.server_close()


def _write_docker_shim(tmpdir: Path, *,
                       health_sequence: list[str]) -> Path:
    """Write a `docker` shell shim into `tmpdir` (returned as the
    binary path; the caller prepends `tmpdir` to PATH).

    On each `docker inspect ...` call the shim returns the next
    item from `health_sequence`. After the sequence is exhausted
    the last item sticks (so [`starting`, `starting`, `healthy`]
    yields starting/starting/healthy/healthy/... ).

    Any other `docker` subcommand → no-op exit 0 (VERIFY_SKIP_TAG_CHECK
    means the script never calls them anyway, but defensive).
    """
    counter_file = tmpdir / ".docker_shim_counter"
    counter_file.write_text("0")
    sequence_literal = " ".join(
        f"'{s}'" for s in health_sequence
    )
    shim = tmpdir / "docker"
    shim.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Test shim for verify_deployed_image.sh runtime tests.
        sequence=({sequence_literal})
        counter_file="{counter_file}"
        case "$1" in
            inspect)
                count=$(cat "$counter_file" 2>/dev/null || echo 0)
                idx=$count
                if (( idx >= ${{#sequence[@]}} )); then
                    idx=$((${{#sequence[@]}} - 1))
                fi
                echo "${{sequence[$idx]}}"
                echo $((count + 1)) > "$counter_file"
                ;;
            *)
                # Any other subcommand (compose, image, ps...) is a
                # no-op in tests — VERIFY_SKIP_TAG_CHECK=1 keeps the
                # script from touching them.
                ;;
        esac
    """))
    shim.chmod(0o755)
    return shim


def _run_verify(expected_sha: str, *,
                docker_shim_dir: Path,
                env_extra: dict | None = None,
                timeout_s: int = 60) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{docker_shim_dir}:{env.get('PATH', '')}"
    env["VERIFY_SKIP_TAG_CHECK"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(VERIFY_SH), expected_sha],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


@_LINUX_ONLY
class TestVerifyRuntime:

    def test_slow_service_polling_succeeds(self, tmp_path):
        """Bug 1 R2 — service is "starting" for the first two
        polls, then "healthy". Verify must poll, wait, and succeed
        — the pre-fix one-shot path would have failed on the first
        non-healthy status (or, before that, on the curl that
        precedes the healthcheck flip).
        """
        servers = _start_health_servers(
            body_chat=b'{"git_sha":"5044173"}',
            body_admin=b'{"git_sha":"5044173"}',
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            # First two inspects → "starting", then "healthy" sticks.
            _write_docker_shim(
                shim_dir,
                health_sequence=["starting", "starting", "healthy"],
            )
            # Tight test budget so the assert is bounded; the script
            # default is 180 s for prod.
            result = _run_verify(
                "5044173",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "30"},
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 0, (
            f"polling slow service should succeed, got rc="
            f"{result.returncode}\nstdout:\n{result.stdout}"
            f"\nstderr:\n{result.stderr}"
        )
        # Pin the polling shape: stderr should have at least one
        # "waiting" message proving the poll actually waited.
        assert "waiting" in result.stderr, (
            "expected at least one '[verify] ... waiting' line in "
            "stderr (the poll progress log). The shim returned "
            "'starting' twice; if no 'waiting' appears, the poll "
            "isn't running."
        )
        assert "OK: chat /health.git_sha=5044173" in result.stdout
        assert "OK: admin /health.git_sha=5044173" in result.stdout

    def test_pre_b3_plain_ok_body_in_rollback_mode_degrades(self, tmp_path):
        """Bug 2 R2 — /health returns plain text "ok" (pre-B3
        rollback target). With `VERIFY_ALLOW_PRE_B3_DEGRADED=1`
        (the gate deploy.sh exports on MODE=rollback), verify
        accepts non-JSON as degraded success: 200 OK is sufficient
        when git_sha surface doesn't yet exist. Verify must NOT
        crash on json.loads."""
        servers = _start_health_servers(
            body_chat=b"ok", body_admin=b"ok",
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            _write_docker_shim(shim_dir, health_sequence=["healthy"])
            result = _run_verify(
                "5044173",
                docker_shim_dir=shim_dir,
                env_extra={
                    "VERIFY_HEALTHCHECK_BUDGET_S": "10",
                    # Simulate deploy.sh rollback-mode behaviour.
                    "VERIFY_ALLOW_PRE_B3_DEGRADED": "1",
                },
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 0, (
            f"pre-B3 plain 'ok' body in rollback mode should DEGRADE "
            f"to success, got rc={result.returncode}\nstdout:\n"
            f"{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "non-JSON" in result.stdout or "pre-B3" in result.stdout, (
            "expected the degraded-mode marker in stdout so operators "
            "see why git_sha was skipped"
        )

    def test_forward_mode_plain_ok_body_fails_loud(self, tmp_path):
        """User-requested gate (2026-05-25): non-JSON `/health` in
        FORWARD mode (the env var is NOT set) is the silent-success
        class that --expected-sha was meant to close — verify must
        reject it, NOT degrade.

        Pinned scenario: B3+ image's chat process starts but its
        init failed silently and `/health` returns a generic error
        string ("ok", "starting", "error"...) without a JSON
        envelope. Pre-gating, verify would degrade and accept the
        deploy. Post-gating, verify fails with exit 8 and the
        operator sees the forward-mode silent-success guard fire.
        """
        servers = _start_health_servers(
            body_chat=b"ok", body_admin=b"ok",
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            _write_docker_shim(shim_dir, health_sequence=["healthy"])
            # NO VERIFY_ALLOW_PRE_B3_DEGRADED — this is a forward deploy.
            result = _run_verify(
                "5044173",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "10"},
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 8, (
            f"forward mode + non-JSON /health must exit 8 (silent-"
            f"success guard), got rc={result.returncode}\nstdout:\n"
            f"{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "forward deploy" in result.stderr, (
            "expected the 'forward deploy' marker in stderr explaining "
            "why non-JSON was rejected"
        )

    def test_malformed_json_in_forward_mode_fails_loud(self, tmp_path):
        """Defensive — malformed JSON body (truncated dict) in
        forward mode fails loud (exit 8), same path as plain "ok".
        Verifies the json.loads guard is still in place (no crash)
        AND that the gate fires for any non-parseable body, not
        just plain text. A future regression that drops the
        try/except surfaces here as a CRASH (rc != 8) — distinct
        from "guard works but body wasn't acceptable" (rc == 8).
        """
        servers = _start_health_servers(
            body_chat=b'{"git_sha":',  # truncated
            body_admin=b'{"git_sha":',
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            _write_docker_shim(shim_dir, health_sequence=["healthy"])
            # Forward mode — gate active.
            result = _run_verify(
                "5044173",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "10"},
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 8, (
            f"malformed JSON in forward mode must exit 8 (gated "
            f"silent-success refusal), not crash and not degrade; "
            f"got rc={result.returncode}\nstderr:\n{result.stderr}"
        )

    def test_b3_json_with_matching_sha(self, tmp_path):
        """Positive — happy path: B3+ image, JSON body, git_sha
        matches expected. Pins that the runtime check still
        actually checks git_sha when JSON is well-formed.
        """
        servers = _start_health_servers(
            body_chat=b'{"git_sha":"abc1234","build_time":"x"}',
            body_admin=b'{"git_sha":"abc1234"}',
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            _write_docker_shim(shim_dir, health_sequence=["healthy"])
            result = _run_verify(
                "abc1234",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "10"},
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 0, (
            f"valid B3 JSON with matching sha must succeed, got rc="
            f"{result.returncode}\nstdout:\n{result.stdout}"
            f"\nstderr:\n{result.stderr}"
        )
        assert "OK: chat /health.git_sha=abc1234" in result.stdout
        assert "OK: admin /health.git_sha=abc1234" in result.stdout

    def test_b3_json_with_mismatched_sha_fails_loud(self, tmp_path):
        """Negative — git_sha mismatch is loud (rc=7), not silent.
        Same exit path as before the fix; the degraded mode is
        ONLY for non-JSON, not for "JSON but wrong sha".
        """
        servers = _start_health_servers(
            body_chat=b'{"git_sha":"wrong000"}',
            body_admin=b'{"git_sha":"wrong000"}',
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            _write_docker_shim(shim_dir, health_sequence=["healthy"])
            result = _run_verify(
                "abc1234",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "10"},
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 7, (
            f"sha mismatch must exit 7 (loud), got rc="
            f"{result.returncode}\nstderr:\n{result.stderr}"
        )
        assert "does not match expected SHA" in result.stderr

    def test_poll_budget_exhausted_fails_loud(self, tmp_path):
        """Bug 1 corollary: if the container never flips to
        healthy within budget, verify fails loud (rc=6) rather
        than blocking deploy forever.
        """
        servers = _start_health_servers(
            body_chat=b'{"git_sha":"5044173"}',
            body_admin=b'{"git_sha":"5044173"}',
        )
        try:
            shim_dir = tmp_path / "shim"
            shim_dir.mkdir()
            # Always "starting" — never healthy.
            _write_docker_shim(shim_dir, health_sequence=["starting"])
            result = _run_verify(
                "5044173",
                docker_shim_dir=shim_dir,
                env_extra={"VERIFY_HEALTHCHECK_BUDGET_S": "10"},
                timeout_s=30,
            )
        finally:
            _stop_servers(servers)

        assert result.returncode == 6, (
            f"poll budget exhaustion must exit 6 (loud), got rc="
            f"{result.returncode}\nstderr:\n{result.stderr}"
        )
        assert "did not become healthy" in result.stderr
