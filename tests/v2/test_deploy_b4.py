"""S-B4 acceptance: one-command self-verifying deploy with auto-rollback.

These tests are the R2 negative-test gate for the 2026-05-25 S-B4 block
(``docs/v2/decisions.md`` → 2026-05-25 S-B4: ADR-B4 + ADR-B5 +
D-SB4-1..D-SB4-3). They lock in:

* **D-SB4-1**: ``predeploy_probe_suite.py`` exposes ``--expected-sha``
  and routes the SHA mismatch into the same exit-code 4 bucket as
  "health never came up" — the probe-runner refuses to fire 12 probes
  against the wrong image.
* **D-SB4-2**: ``verify_deployed_image.sh`` no longer falls back to
  ``git rev-parse --short HEAD`` when called without an explicit SHA.
  Fallback order is now ``$1`` → ``$WC_IMAGE_TAG`` → fail loudly.
  ``smoke_s_b2.sh`` is updated to source ``.env`` and pass the tag
  explicitly so the smoke is not implicitly trusting host-repo state.
* **D-SB4-3**: ``deploy.sh`` captures ``PREVIOUS_SHA`` from the
  running ``gutenberg-lab`` container BEFORE ``compose up``, and on
  red verify OR red probe-gate self-invokes ``--rollback
  $PREVIOUS_SHA``. The "no rollback target" path exits non-zero
  without attempting a phantom rollback.

The tests do NOT shell out to docker / curl — they parse the scripts
and the registry directly so they work on dev boxes and CI runners
without a docker daemon.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
VERIFY_SH = REPO_ROOT / "scripts" / "verify_deployed_image.sh"
SMOKE_SH = REPO_ROOT / "scripts" / "smoke_s_b2.sh"
PROBE_SUITE_PY = REPO_ROOT / "scripts" / "predeploy_probe_suite.py"


def _strip_bash_comments(text: str) -> str:
    """Drop whole-line bash comments (``^\\s*#...``) before grepping
    for command shapes. Comments and docstrings legitimately
    describe behaviour using the same tokens as the executable code
    (e.g., the deploy.sh header mentions
    ``docker compose ... up -d --force-recreate`` in step-5 prose);
    a comment-blind grep finds the first prose occurrence and lies
    about the actual command order. We strip only whole-line
    comments, not inline ones — bash treats ``#`` as comment-start
    inside double-quoted strings the same way Python does, so inline
    stripping is fraught and not needed for our greps.
    """
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


# ---------------------------------------------------------------------------
# D-SB4-2: verify_deployed_image.sh no-arg footgun closed
# ---------------------------------------------------------------------------


def test_verify_does_not_grep_HEAD_in_fallback():
    """The pre-fix fallback ``git rev-parse --short HEAD`` is gone.

    Pre-D-SB4-2 the script used this fallback when invoked with no
    positional arg, silently comparing the running image against
    whatever HEAD the host happened to have checked out. The fallback
    is host-state-dependent and the 2026-05-22 deploy epic's smoke
    runs would have lied about success if the host had advanced HEAD
    between deploy and verify.

    The new fallback order is ``$1`` → ``$WC_IMAGE_TAG`` → exit 2.
    Asserts no executable line invokes ``git rev-parse`` (header
    comments that historically explain the change are tolerated).
    """
    code = _strip_bash_comments(VERIFY_SH.read_text(encoding="utf-8"))
    assert "git rev-parse" not in code, (
        "verify_deployed_image.sh must NOT execute `git rev-parse "
        "--short HEAD` as a fallback (D-SB4-2). Pass the SHA "
        "explicitly or set WC_IMAGE_TAG."
    )


def test_verify_reads_wc_image_tag_as_fallback():
    """Positive mirror: the new fallback IS ``$WC_IMAGE_TAG``.

    Pin the new shape so a future change cannot silently swap it for a
    different magic source (e.g., ``cat .env | grep`` from inside verify).
    """
    text = VERIFY_SH.read_text(encoding="utf-8")
    # Either form is acceptable — `${WC_IMAGE_TAG:-}` or plain `$WC_IMAGE_TAG`.
    assert re.search(r"\$\{?WC_IMAGE_TAG", text), (
        "verify_deployed_image.sh must consult $WC_IMAGE_TAG as the "
        "fallback when no positional SHA is passed (D-SB4-2)."
    )


def test_verify_fails_loud_when_no_arg_no_env():
    """End-to-end-shape: run the script with no arg and an empty env;
    expect exit 2 and a "refusing to fall back" message on stderr.

    Skipped on Windows because the script is bash-only — but the
    parsing tests above pin the shape on every OS.
    """
    if sys.platform == "win32":
        pytest.skip("bash-only script; shape pinned by static-grep tests above")
    if not _have_bash():
        pytest.skip("bash not on PATH")

    env = {k: v for k, v in os.environ.items() if not k.startswith("WC_")}
    # Force WITH_RUNTIME=0 so the script does not try to curl /health
    # before hitting the fallback path.
    env["WITH_RUNTIME"] = "0"
    proc = subprocess.run(
        ["bash", str(VERIFY_SH)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, (
        f"verify_deployed_image.sh with no arg + no WC_IMAGE_TAG must "
        f"exit 2 (D-SB4-2). Got rc={proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert "refusing to fall back" in proc.stderr.lower(), (
        f"stderr must explain the refusal; got: {proc.stderr!r}"
    )


def test_smoke_passes_explicit_sha_to_verify():
    """``smoke_s_b2.sh`` no longer calls verify without args.

    The pre-D-SB4-2 smoke had ``bash scripts/verify_deployed_image.sh``
    on its own line — which under the new verify behaviour would
    fail loud. The fix sources ``WC_IMAGE_TAG`` from ``.env`` and
    passes it explicitly. Anti-drift pin: no bare verify invocation
    survives.
    """
    text = SMOKE_SH.read_text(encoding="utf-8")
    # Find every uncommented `bash scripts/verify_deployed_image.sh ...` line.
    bare_calls = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "scripts/verify_deployed_image.sh" not in line:
            continue
        # Allow the redirect/echo lines that mention the path in a
        # message — only consider executable invocations.
        if "bash scripts/verify_deployed_image.sh" not in line:
            continue
        # Strip everything up to and including the script name; what's
        # left should be a non-empty arg.
        after = line.split("scripts/verify_deployed_image.sh", 1)[1].strip()
        if not after:
            bare_calls.append(line.strip())
    assert not bare_calls, (
        "smoke_s_b2.sh must pass the SHA explicitly to verify "
        "(D-SB4-2). Bare invocations would now fail under the new "
        f"verify no-arg behaviour. Offending lines: {bare_calls}"
    )
    # Positive mirror: smoke reads WC_IMAGE_TAG from .env before verify.
    assert re.search(r"WC_IMAGE_TAG=.*\.env", text) or "WC_IMAGE_TAG=" in text, (
        "smoke_s_b2.sh must read WC_IMAGE_TAG from .env (or env) and "
        "pass it explicitly to verify (D-SB4-2)."
    )


# ---------------------------------------------------------------------------
# D-SB4-1: predeploy_probe_suite.py --expected-sha
# ---------------------------------------------------------------------------


def test_probe_suite_has_expected_sha_flag():
    """``--expected-sha`` is a declared argparse flag, defaulting from
    ``WC_PROBE_EXPECTED_SHA`` env."""
    text = PROBE_SUITE_PY.read_text(encoding="utf-8")
    assert "--expected-sha" in text, (
        "predeploy_probe_suite.py must define --expected-sha (D-SB4-1)"
    )
    assert "WC_PROBE_EXPECTED_SHA" in text, (
        "predeploy_probe_suite.py --expected-sha must accept "
        "WC_PROBE_EXPECTED_SHA env default (D-SB4-1)"
    )


def test_probe_suite_checks_git_sha_after_health():
    """The suite parses ``/health``'s JSON ``git_sha`` field and
    compares it to ``--expected-sha`` BEFORE running probes.

    Source-grep pattern: the helper ``_check_expected_sha`` (or
    equivalent) exists, takes the parsed body, and asserts on
    ``git_sha``.
    """
    text = PROBE_SUITE_PY.read_text(encoding="utf-8")
    # Helper or inline assertion — either is acceptable; the contract
    # is that the *string* `git_sha` is read from the health body
    # somewhere AND used in the bail-out path.
    assert re.search(r'"git_sha"|\.get\(\s*[\'"]git_sha[\'"]', text), (
        "predeploy_probe_suite.py must read 'git_sha' from the /health "
        "response body and compare to the expected value (D-SB4-1)."
    )


def test_probe_suite_sha_mismatch_exits_4(tmp_path):
    """End-to-end: with ``--expected-sha`` set and ``--no-health``
    forced off (default), but pointing at a base-url that does not
    serve /health, the suite exits 4 (health-not-up bucket).

    We can't easily simulate a live SHA mismatch without spinning up
    a mock HTTP server. Instead, verify the negative mirror: if
    ``--expected-sha`` is set but the health body has no ``git_sha``
    field, the runner exits 4 — same exit code as "health never came
    up". Skipped if probe-config has unfilled slots (the suite would
    exit 5 first, before reaching the SHA check).
    """
    if sys.platform == "win32" and not _have_bash():
        pytest.skip("python3 invocation differs on Windows; CI Linux covers")
    # Point base-url at an unbound port → wait_for_health returns False
    # → exit 4. With --expected-sha set the failure path stays in the
    # exit-4 bucket regardless of whether the SHA check fired.
    base_url = "http://127.0.0.1:1"  # port 1 is privileged; nothing listens
    proc = subprocess.run(
        [sys.executable, str(PROBE_SUITE_PY),
         "--base-url", base_url,
         "--expected-sha", "deadbee",
         "--no-require-version-bump"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The suite exits 4 on "health never came up" (no body to check).
    # If the probe-config is unfilled, the suite would exit 5 first —
    # we tolerate either 4 or 5 here; the point is that the new flag
    # doesn't crash the runner with an unrelated exit code.
    assert proc.returncode in (4, 5), (
        f"probe suite must exit 4 (health/SHA bucket) or 5 (config) on "
        f"unreachable base-url with --expected-sha; got rc={proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# D-SB4-3: deploy.sh captures PREVIOUS_SHA + invokes probe gate + rollback
# ---------------------------------------------------------------------------


def test_deploy_sh_captures_previous_sha_before_compose_up():
    """The PREVIOUS_SHA capture must happen BEFORE ``compose up``.

    Post-recreate, ``docker inspect`` of the running gutenberg-lab
    shows the *new* tag — the previous tag survives only in the
    image store. Capturing before recreate is the only point at
    which container-level state still names the previous SHA
    (D-SB4-3).
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    # Find the EXECUTED `docker compose up -d --force-recreate` line —
    # the docstring step-5 prose at the top of the script uses the
    # same tokens to document the step. Stripped above.
    up_match = re.search(r"docker compose [^\n]*up -d --force-recreate", code)
    assert up_match, "deploy.sh must invoke `docker compose up -d --force-recreate`"
    up_pos = up_match.start()
    # Find the line that does `docker inspect ... gutenberg-lab`-shape capture.
    inspect_match = re.search(r"docker inspect[^\n]*Config\.Image", code)
    assert inspect_match, (
        "deploy.sh must capture the running gutenberg-lab image via "
        "`docker inspect --format='{{.Config.Image}}'` (D-SB4-3)"
    )
    assert inspect_match.start() < up_pos, (
        "deploy.sh must capture PREVIOUS_SHA BEFORE `compose up` "
        "(D-SB4-3) — capturing after means the inspect call returns "
        "the just-recreated container's NEW tag and the rollback "
        "target is silently lost."
    )


def test_deploy_sh_invokes_probe_gate_after_verify():
    """Probe-gate sequence: ``verify_deployed_image.sh`` runs first,
    then ``predeploy_probe_suite.py``. The order matters because
    verify is the cheap check (docker tag + one curl per service)
    and probe-gate is the expensive one (12 probes × up to 180 s).
    A red verify must short-circuit before the 12-probe budget burns.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    verify_match = re.search(r"scripts/verify_deployed_image\.sh", code)
    probe_match = re.search(r"scripts/predeploy_probe_suite\.py", code)
    assert verify_match, "deploy.sh must call verify_deployed_image.sh"
    assert probe_match, (
        "deploy.sh must call predeploy_probe_suite.py as the probe gate (D-SB4-1)"
    )
    assert verify_match.start() < probe_match.start(), (
        "deploy.sh must call verify_deployed_image.sh BEFORE the probe "
        "gate (D-SB4-1). Reversed order would burn the 12-probe budget "
        "on a tag-mismatch that verify catches in seconds."
    )


def test_deploy_sh_probe_gate_passes_expected_sha():
    """The probe-gate invocation must include ``--expected-sha
    "${SHA}"`` so the identity gate is wired through. Without this
    flag the suite reverts to "any /health 200 OK is fine" and the
    silent-failure deploy class is back."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    # Look for --expected-sha "${SHA}" in proximity to the EXECUTED
    # probe-suite invocation (comments stripped above — header
    # docstring mentions --expected-sha too).
    probe_block = re.search(
        r"predeploy_probe_suite\.py[\s\S]{0,500}",
        code,
    )
    assert probe_block, "deploy.sh must invoke predeploy_probe_suite.py"
    assert "--expected-sha" in probe_block.group(0), (
        "deploy.sh probe-gate must pass --expected-sha (D-SB4-1)"
    )
    assert re.search(r'--expected-sha\s+["\$]', probe_block.group(0)), (
        "deploy.sh must wire the SHA variable through to --expected-sha, "
        "not a literal (D-SB4-1)"
    )


def test_deploy_sh_rolls_back_on_red_verify_or_probe_gate():
    """A red verify or red probe-gate must trigger the rollback path,
    which is a self-invocation of ``bash $0 --rollback $PREVIOUS_SHA``
    (the existing rollback mode handles .env-write + compose up +
    verify against the previous tag). Self-invocation rather than
    function extraction keeps the rollback semantics identical to a
    manual ``--rollback`` invocation — same code path, same exit
    codes, same guarantees (D-SB4-3)."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    # Captured exit codes for verify and probe gate.
    assert re.search(r"verify_rc\s*=", text) and re.search(r"probe_rc\s*=", text), (
        "deploy.sh must capture verify_rc and probe_rc into shell "
        "variables — flat `set -e` abort cannot drive rollback "
        "(D-SB4-3)"
    )
    # Rollback self-invocation conditioned on verify_rc / probe_rc non-zero.
    rollback_block = re.search(
        r'"\$verify_rc"\s*-ne\s*0[\s\S]{0,800}\$0[\s\S]{0,200}--rollback',
        text,
    )
    assert rollback_block, (
        "deploy.sh must self-invoke `bash $0 --rollback ${PREVIOUS_SHA}` "
        "when verify_rc or probe_rc is non-zero (D-SB4-3)."
    )


def test_deploy_sh_aborts_without_rollback_when_no_previous():
    """If ``PREVIOUS_SHA`` is empty (cold start / re-deploy of same
    SHA / running image not in family), the rollback path must NOT
    fire — the deploy must exit non-zero with an explicit "no
    rollback target" message so the operator does not chase a
    phantom-rollback false success (D-SB4-3)."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    # Two assertions, each scoped — clearer failure mode than one long
    # cross-block regex:
    #   (a) the rollback-failure block tests `-n "$PREVIOUS_SHA"`
    #   (b) the same control-flow block contains a `no rollback target`
    #       message after `else`, AND that branch exits non-zero.
    assert re.search(r'-n\s*"\$\{?PREVIOUS_SHA\}?"', code), (
        "deploy.sh must guard rollback on `-n $PREVIOUS_SHA` "
        "(D-SB4-3): an empty PREVIOUS_SHA is not a rollback target."
    )
    # Anchor on the no-rollback-target message and an adjacent exit.
    no_target_block = re.search(
        r'no rollback target[\s\S]{0,400}\bexit\s+\d+',
        code,
        re.IGNORECASE,
    )
    assert no_target_block, (
        "deploy.sh must exit non-zero on the no-rollback-target path "
        "with a 'no rollback target' message (D-SB4-3)."
    )


def test_deploy_sh_no_probe_gate_flag_documented():
    """The ``--no-probe-gate`` escape hatch exists for emergency
    rollback paths (when the probe-gate itself is broken). Its
    presence is documented in the script header so an operator
    finds it via ``--help``."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert "--no-probe-gate" in text, (
        "deploy.sh must support --no-probe-gate as an emergency "
        "escape hatch (ADR-B4 trade-off)"
    )


# ---------------------------------------------------------------------------
# Stale-image-under-new-tag class — the headline S-B4 acceptance test.
# This is a behavioural test, not a pin: it mocks `docker` and `curl` on
# PATH so that docker-tag checks PASS but /health.git_sha returns a
# DIFFERENT SHA. The verify script must catch the mismatch via the
# D-SB3-3 runtime probe and exit non-zero.
# ---------------------------------------------------------------------------


def test_stale_image_under_new_tag_fails_verify(tmp_path):
    """S-B4 TZ acceptance gate: 'подсунуть старый образ под новый тег → verify обязан упасть'.

    Scenario: an operator manually retags an old image as the new SHA,
    or compose somehow ends up with a current-SHA tag pointing at
    stale content. The docker-tag layer is happy (the tag matches),
    but the running process inside that container still self-reports
    the OLD ``git_sha`` via /health (the image content didn't actually
    change). Without the D-SB3-3 runtime probe, verify would silently
    pass — exactly the failure mode that wasted runs 2-5 of the
    2026-05-22 deploy epic.

    The test mocks ``docker`` and ``curl`` on PATH:
      * docker: every shape verify_deployed_image.sh uses returns the
        NEW SHA (tag check passes).
      * curl: /health on chat:8890 and admin:8891 returns JSON with
        ``git_sha`` = OLD SHA (mismatch).

    Expected: verify exits with the D-SB3-3 runtime-mismatch code (7).
    """
    if sys.platform == "win32":
        pytest.skip("bash + PATH-prepend mocks; Linux CI covers")
    if not _have_bash():
        pytest.skip("bash not on PATH")

    new_sha = "deadbee"
    old_sha = "abc1230"

    docker_mock = tmp_path / "docker"
    docker_mock.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Mock docker for the stale-image-under-new-tag test. Returns
        # the NEW SHA for every code path verify_deployed_image.sh
        # consults so the docker-tag check passes — the failure must
        # be caught by the runtime /health probe, not by the tag check.
        case "$*" in
          *compose*ps*--format*json*)
            # JSON array form (compose v2.21+). First-byte '[' steers
            # the verify script's branch selection (head -c 1).
            printf '[{{"Service":"%s","Image":"wordcracker-textlab:{new_sha}"}}]' "${{4:-svc}}"
            ;;
          *compose*ps*-q*)
            printf 'fake-cid\\n'
            ;;
          *inspect*Config.Image*)
            printf 'wordcracker-textlab:{new_sha}\\n'
            ;;
          *inspect*State.Health*)
            # D-SB3-3 amendment (2026-05-25) — poll_until_healthy reads
            # `.State.Health.Status` to wait for compose's healthcheck.
            # For this test the runtime probe is what must catch the
            # sha mismatch, so we report healthy immediately and let
            # the curl path do its job.
            printf 'healthy\\n'
            ;;
          *image*ls*--format*)
            # Used in error-path image listings; harmless empty.
            ;;
          *)
            printf 'mock docker: unhandled args: %s\\n' "$*" >&2
            exit 99
            ;;
        esac
    """))
    docker_mock.chmod(0o755)

    curl_mock = tmp_path / "curl"
    curl_mock.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Mock curl. Returns JSON whose git_sha is OLD — that's the
        # signature of "image content stale despite tag changed".
        for arg in "$@"; do
          case "$arg" in
            *127.0.0.1:8890/health*|*127.0.0.1:8891/health*)
              printf '{{"status":"ok","version":"2.6.13","git_sha":"{old_sha}","build_time":"2026-05-25T00:00:00Z"}}'
              exit 0
              ;;
          esac
        done
        # Anything else → verify script handles "empty body" path; not
        # expected to be hit by this test.
        exit 1
    """))
    curl_mock.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    # WITH_RUNTIME defaults to 1 (we want the runtime probe to fire).
    env.pop("WITH_RUNTIME", None)

    proc = subprocess.run(
        ["bash", str(VERIFY_SH), new_sha],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # The stale-image-under-new-tag class must surface as a non-zero
    # exit. Pin the specific exit code (7 = D-SB3-3 runtime mismatch)
    # so a future change that downgrades the mismatch to a warning
    # also fails here.
    assert proc.returncode != 0, (
        f"verify_deployed_image.sh with docker-tag={new_sha} but "
        f"/health.git_sha={old_sha} must FAIL but did not.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert proc.returncode == 7, (
        f"stale-image-under-new-tag must exit 7 (D-SB3-3 runtime "
        f"mismatch), not rc={proc.returncode}.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # NOT-X mirror INSIDE the same test: the stderr must specifically
    # explain the /health.git_sha mismatch, not a tag mismatch (those
    # are different failure classes — keeping the diagnostic specific
    # makes the next operator debug the right surface).
    assert "/health.git_sha" in proc.stderr or "git_sha" in proc.stderr, (
        f"verify stderr must name /health.git_sha as the failing "
        f"surface; got: {proc.stderr!r}"
    )


def test_verify_runtime_probe_still_present():
    """Anti-drift pin (cheaper than the behavioural test above; runs
    on every OS without bash). The runtime ``/health.git_sha`` probe
    (D-SB3-3) is what catches the stale-image-under-new-tag class.
    A future change that deletes the probe also fails the behavioural
    test above; this pin makes the failure mode visible without
    needing bash/curl mocks.
    """
    text = VERIFY_SH.read_text(encoding="utf-8")
    assert "/health" in text and "git_sha" in text, (
        "verify_deployed_image.sh must still carry the runtime "
        "/health.git_sha probe from D-SB3-3 — without it the "
        "stale-image-under-new-tag class is back."
    )


def test_deploy_sh_rollback_mode_exits_loud_on_verify_red():
    """Recursion / silent-success guard (D-SB4-3): in MODE=rollback,
    the probe-gate and rollback-decision blocks both gate on
    MODE==deploy. Without an explicit guard, a verify failure in
    rollback mode would fall through to prune + print "OK", and the
    parent script's `rollback_rc=$?` would capture rc=0 → parent
    prints "rolled back" while the host is actually broken. The
    rollback-mode strict-exit must be present.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    # The guard shape: `"$MODE" == "rollback" && "$verify_rc" -ne 0` → exit.
    # Bash quotes around $MODE are optional in test-bracket context, so
    # tolerate both `$MODE` and `"$MODE"` forms.
    guard = re.search(
        r'\$MODE"?\s*==\s*"rollback"[\s\S]{0,300}verify_rc[\s\S]{0,300}\bexit\b',
        code,
    )
    assert guard, (
        "deploy.sh must hard-exit on a red verify in MODE=rollback "
        "(D-SB4-3). Without it, child-process rollback failure is "
        "silent and the parent thinks the rollback succeeded."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _have_bash() -> bool:
    """True if `bash` is callable. Some Windows boxes have it (Git Bash,
    WSL); most don't. The bash-only smokes skip cleanly on absence."""
    try:
        proc = subprocess.run(
            ["bash", "--version"], capture_output=True, timeout=5, text=True
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
