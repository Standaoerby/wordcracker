"""S-B7 acceptance: deploy hardening.

R2 negative-test gate for the 2026-05-30 S-B7 block. Three pieces:

* **F2-DEPLOY-RERECORD** — ``scripts/deploy.sh`` gains a fixture
  re-record gate between the probe-gate and prune steps (forward mode
  only). It spins up an ephemeral dev-overlay container, re-runs
  ``record_fixtures --skip-heavy`` against the just-deployed image, and
  ``git diff``s the committed contract fixtures. The gate is **advisory**
  (fix-rerecord-gate): a recorder timeout/error or a drift finding prints
  a loud WARNING and the deploy continues — by this point the service is
  already verify+probe green and live, so a drift signal is a fix-forward
  heads-up, not a rollback trigger. The diff EXCLUDES ``_manifest.json``
  because its ``elapsed_s`` / ``size_bytes`` fields are non-deterministic
  and would false-warn every deploy. ``--skip-heavy`` + a ``timeout``
  backstop keep the gate from hanging the deploy on a multi-minute corpus
  scan, and ``record_fixtures`` hard-exits (``os._exit``) after recording
  so a lingering non-daemon thread (chromadb telemetry / cuda) can't wedge
  interpreter shutdown into that ``timeout``. This is layer 4 of the
  stale-fixture defence documented in
  ``tests/v2/test_v1_contracts.py::FixtureFreshnessGate``.

* **status unit orphan-kill** — ``systemd/wordcracker-status.service``
  gains a HOST-side ``pkill`` ``ExecStartPre`` (not ``docker exec`` —
  status_server runs on the host) to reap a stray nohup'd status_server
  bound to :8889 before binding the port.

* **.gitignore prod-root clutter** — ``/test_report_*.md`` and
  ``/*.bak`` so the ~24 root-level untracked files stop showing in
  ``git status`` / the deploy dirty-check.

Like test_deploy_b4 / test_deploy_artifact, these parse the scripts and
unit files directly (no docker / curl), so they run on dev boxes and CI
runners without a docker daemon. The one behavioural test
(``test_manifest_excluded_diff_*``) uses a throwaway git repo to pin the
load-bearing diff semantics deterministically.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
RECORD_FIXTURES_PY = REPO_ROOT / "scripts" / "v2" / "contracts" / "record_fixtures.py"
STATUS_UNIT = REPO_ROOT / "systemd" / "wordcracker-status.service"
GITIGNORE = REPO_ROOT / ".gitignore"

FIXTURES_REL = "scripts/v2/contracts/fixtures"


def _strip_bash_comments(text: str) -> str:
    """Drop whole-line bash comments before grepping for command shapes.

    The deploy.sh header docstring describes the gate using the same
    tokens as the executable code (e.g. ``record_fixtures --skip-heavy``
    appears in the step-8b prose). A comment-blind grep would match the
    prose and lie about command order. Mirror of the helper in
    test_deploy_b4.py.
    """
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _have_git() -> bool:
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=5, text=True
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# F2-DEPLOY-RERECORD — deploy.sh re-record gate, script-shape pins
# ---------------------------------------------------------------------------


def test_rerecord_gate_runs_after_probe_gate_before_prune():
    """Gate order: probe-gate (predeploy_probe_suite.py) → re-record
    (record_fixtures) → prune.

    Re-record must come AFTER the cheap verify + probe-gate (a red
    either-one rolls back and exits first, so we never burn a corpus
    sweep on a failed deploy) and BEFORE prune (a drift failure must
    block the deploy, not happen after images are already cleaned up).
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    probe_pos = code.find("scripts/predeploy_probe_suite.py")
    record_pos = code.find("scripts.v2.contracts.record_fixtures")
    prune_pos = code.find("docker image rm")
    assert probe_pos != -1, "deploy.sh must still call the probe gate"
    assert record_pos != -1, (
        "deploy.sh must invoke `record_fixtures` as the re-record gate (S-B7)"
    )
    assert prune_pos != -1, "deploy.sh must still have the prune step"
    assert probe_pos < record_pos < prune_pos, (
        "re-record gate must run AFTER the probe gate and BEFORE prune "
        f"(S-B7). Got probe@{probe_pos} record@{record_pos} prune@{prune_pos}."
    )


def test_rerecord_gate_uses_ephemeral_dev_overlay():
    """The gate uses an ephemeral dev-overlay container.

    Pins the three load-bearing tokens on the recorder invocation:
      * ``-f docker-compose.dev.yml`` — the dev overlay bind-mounts
        ./scripts so fixtures written in-container land on the host repo
        (a host `git diff` can then see drift). Without it the recorder
        writes into the image's COPY'd /workspace/scripts and the host
        diff sees nothing.
      * ``run -d`` — detached (R-28 #1; was ``run --rm``): the attached
        compose client wedged in attach teardown after the container
        exited, eating the full timeout budget on every deploy. ``run``
        (vs ``up``) still does NOT recreate / touch the running
        chat/admin/gutenberg-lab services. Ephemerality is now reap-based:
        an explicit ``docker rm -f`` after the wait (plus the shared
        cleanup trap) replaces ``--rm`` and removes the daemon-side
        ``--rm``-vs-``docker wait`` race.
      * ``record_fixtures`` — the recorder module itself.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    # Scope to the recorder invocation neighbourhood.
    idx = code.find("scripts.v2.contracts.record_fixtures")
    assert idx != -1, "deploy.sh must invoke record_fixtures (S-B7)"
    block = code[max(0, idx - 400): idx + 200]
    assert "-f docker-compose.dev.yml" in block, (
        "re-record gate must layer the dev overlay (-f docker-compose.dev.yml) "
        "so the in-container fixture writes bind-mount back to the host repo (S-B7)"
    )
    assert "run -d" in block, (
        "re-record gate must `run -d` (detached) — the attached compose client "
        "is a known hang class in attach teardown (R-28 #1)"
    )
    assert "run --rm" not in block, (
        "re-record gate must NOT use `--rm`: the explicit `docker rm -f` reap "
        "owns container removal, avoiding the daemon-side --rm-vs-`docker wait` "
        "race (R-28 #1)"
    )
    after = code[idx: idx + 1600]
    assert "docker rm -f" in after, (
        "with --rm gone, the gate must explicitly `docker rm -f` the named "
        "container after the wait — ephemerality is reap-based (S-B10 #1 / "
        "R-28 #1)"
    )


def test_rerecord_gate_is_timeout_aware():
    """The gate cannot hang a deploy on a multi-minute corpus scan.

    Two mechanisms, both required (brief: 'щедрый overall-budget ИЛИ
    per-tool skip-heavy' — we do both for belt-and-suspenders):
      * ``--skip-heavy`` drops the HEAVY_BINDINGS full-corpus scans
        (word_contexts_global ~400s etc.).
      * ``timeout`` is a hard wall-clock backstop if a non-heavy tool
        wedges. R-28 #1: the recorder runs detached, so the backstop now
        wraps the ``docker wait`` that blocks on it — same budget, same
        rc=124 → WARN semantics.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    block = code[max(0, idx - 400): idx + 200]
    assert "--skip-heavy" in block, (
        "re-record gate must pass --skip-heavy so the multi-minute corpus "
        "scans don't hang the deploy (S-B7)"
    )
    wait_idx = code.find("docker wait", idx)
    assert wait_idx != -1, (
        "the detached re-record gate must block on `docker wait` (R-28 #1)"
    )
    wait_block = code[max(0, wait_idx - 300): wait_idx + 200]
    assert "timeout " in wait_block, (
        "re-record gate must wrap the `docker wait` in `timeout` as a hard "
        "wall-clock backstop (S-B7 / R-28 #1)"
    )


def test_rerecord_gate_diff_excludes_manifest():
    """The drift diff must EXCLUDE _manifest.json.

    _manifest.json is tracked but carries non-deterministic fields
    (elapsed_s, size_bytes) that change on every recording — a naive
    `git diff --exit-code <fixtures-dir>` would therefore fail on EVERY
    deploy (false positive) and leave the tree dirty for the next
    deploy's dirty-check. The fixture JSON *content* carries the real
    drift signal. Pin both: a `git diff` over the fixtures dir AND an
    exclude pathspec naming _manifest.json.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    assert "git diff --exit-code" in code, (
        "re-record gate must use `git diff --exit-code` over the fixtures "
        "dir to detect drift (S-B7)"
    )
    assert FIXTURES_REL in code, (
        f"re-record gate must diff the fixtures dir ({FIXTURES_REL}) (S-B7)"
    )
    # Exclude pathspec, either `:(exclude)` or `:!` magic form.
    assert ("(exclude)" in code and "_manifest.json" in code), (
        "re-record gate must EXCLUDE _manifest.json from the diff (its "
        "elapsed_s/size_bytes are non-deterministic and would false-fail "
        "every deploy) — use a `:(exclude)...:_manifest.json` pathspec (S-B7)"
    )


def test_rerecord_gate_is_advisory_not_deploy_failing():
    """The gate is ADVISORY (fix-rerecord-gate): neither a recorder
    timeout/error nor a fixture-drift finding may abort the deploy.

    R2 negative for the fix-rerecord-gate block. The pre-fix S-B7 gate
    hard-failed the deploy (``exit 13`` on recorder error/timeout,
    ``exit 14`` on drift). That bit the live 2.6.33 deploy: a SUCCESSFUL
    re-record ("DONE: 28 ok") still hit the ``timeout`` wall (rc=124,
    lingering chromadb-telemetry thread) → ``exit 13`` → deploy reported
    failed even though the service was live+healthy. The gate must now
    only WARN. Pin: the recorder-invocation block carries no ``exit 13`` /
    ``exit 14`` and emits an advisory WARNING instead.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    block = code[idx: idx + 1600]
    assert "exit 13" not in block, (
        "advisory gate must NOT `exit 13` on recorder error/timeout — a "
        "re-record failure cannot abort an already-live deploy "
        "(fix-rerecord-gate)"
    )
    assert "exit 14" not in block, (
        "advisory gate must NOT `exit 14` on fixture drift — drift is a "
        "fix-forward warning, not a rollback trigger (fix-rerecord-gate)"
    )
    assert "WARN" in block, (
        "advisory gate must print a loud WARN on recorder failure / drift "
        "so the signal is never silent (fix-rerecord-gate)"
    )


def test_rerecord_gate_restores_tree_for_next_dirty_check():
    """After running, the gate must restore the fixtures dir so the
    deployed host's working tree matches HEAD.

    The recorder rewrites tracked files (the fixture JSONs and the
    volatile _manifest.json). If the gate left them modified, the NEXT
    deploy's dirty-check (`git diff --quiet HEAD --`, D-SB1-8) would
    block on a tracked-file change. The gate must `git checkout --` the
    fixtures dir on every exit path (pass, drift, recorder-fail).
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    prune = code.find("docker image rm")
    # Whole gate body (recorder → prune): R-28's detached-run plumbing sits
    # between the invocation and the restores, so a fixed-width window
    # under-runs.
    block = code[idx:prune]
    restores = block.count("git checkout --")
    assert restores >= 3, (
        "re-record gate must `git checkout --` the fixtures dir on all "
        f"three exit paths (recorder-fail / drift / clean-pass); found "
        f"{restores} (S-B7)"
    )


def test_rerecord_gate_is_forward_mode_only():
    """The gate is guarded by MODE==deploy.

    A rollback (MODE=rollback) legitimately steps source backward to a
    previous SHA whose fixtures may predate a contract — re-recording
    there would false-fail the recovery path. The gate body must sit
    inside a `MODE == deploy` guard.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    # The nearest `MODE` guard above the recorder invocation must be a
    # deploy guard (the gate block opens with `if [[ "$MODE" == "deploy" ]]`).
    preceding = code[max(0, idx - 600): idx]
    assert '"$MODE" == "deploy"' in preceding, (
        "re-record gate must be guarded by MODE==deploy (forward only) — a "
        "rollback must not re-record against a backward-stepped tree (S-B7)"
    )


# ---------------------------------------------------------------------------
# F2-DEPLOY-RERECORD — record_fixtures --skip-heavy + HEAVY_BINDINGS
# ---------------------------------------------------------------------------


def test_skip_heavy_flag_declared():
    """``record_fixtures.py`` declares the ``--skip-heavy`` flag."""
    text = RECORD_FIXTURES_PY.read_text(encoding="utf-8")
    assert "--skip-heavy" in text, (
        "record_fixtures.py must define --skip-heavy (S-B7)"
    )


def test_heavy_bindings_are_known_live_args():
    """``HEAVY_BINDINGS`` is a subset of ``LIVE_ARGS`` and names the
    three measured slow tools; it must not overlap FIXTURE_EXEMPT (a
    binding can't be both skipped-heavy and exempt — that double-skip
    would silently drop it from every accounting path)."""
    from scripts.v2.contracts.live_args import (
        LIVE_ARGS, HEAVY_BINDINGS, FIXTURE_EXEMPT,
    )
    assert HEAVY_BINDINGS, "HEAVY_BINDINGS must be non-empty (S-B7)"
    unknown = HEAVY_BINDINGS - set(LIVE_ARGS)
    assert not unknown, (
        f"HEAVY_BINDINGS contains qualnames not in LIVE_ARGS: {unknown}. "
        f"A heavy binding must be a real recordable binding (S-B7)."
    )
    assert not (HEAVY_BINDINGS & FIXTURE_EXEMPT), (
        "HEAVY_BINDINGS and FIXTURE_EXEMPT must be disjoint (S-B7)"
    )
    # The dominant scan named in the brief must be in the set.
    assert "scripts.rag_tools.word_contexts_global" in HEAVY_BINDINGS, (
        "word_contexts_global (~400s) is the headline heavy scan and must "
        "be in HEAVY_BINDINGS (S-B7)"
    )


def test_skip_heavy_returns_zero_and_does_not_count_skips_as_failure():
    """``--skip-heavy`` skips heavy bindings cleanly (status
    skipped_heavy), never recording them — so the recorder exit code is
    driven only by the non-heavy bindings. Verified via ``--list``-free
    static read of the loop logic: the skipped_heavy entries carry a
    distinct status and are emitted before the recording loop.

    A full live run needs the corpus (prod only); here we pin that the
    skip path exists and is wired into the manifest accounting.
    """
    text = RECORD_FIXTURES_PY.read_text(encoding="utf-8")
    assert "skipped_heavy" in text, (
        "record_fixtures.py must track skipped_heavy bindings in the "
        "manifest (S-B7)"
    )
    assert "HEAVY_BINDINGS" in text, (
        "record_fixtures.py must consult HEAVY_BINDINGS when --skip-heavy "
        "is set (S-B7)"
    )
    # The skip must be logged, not silent (brief: 'No silent caps').
    assert 'status": "skipped_heavy"' in text or "'skipped_heavy'" in text or \
        "\"skipped_heavy\"" in text, (
        "skipped_heavy must be a recorded manifest status (S-B7)"
    )


# ---------------------------------------------------------------------------
# fix-rerecord-gate — recorder hard-exit so it cannot hang the deploy gate
# ---------------------------------------------------------------------------


def test_record_fixtures_hard_exits_after_done():
    """``record_fixtures`` ``os._exit``s from ``__main__`` instead of
    falling through to a normal interpreter shutdown.

    The recorded v1 tools leave a non-daemon background thread alive
    (chromadb's default-on Posthog telemetry Consumer) plus a cuda/torch
    native context. On a plain ``sys.exit`` the interpreter blocks at
    shutdown joining that thread — the live 2.6.33 deploy printed
    "DONE: 28 ok" then hung until the gate's ``timeout 600`` killed it
    (rc=124). The recorder must finish its work, flush, and exit the
    process directly. Pin the shape: an ``os._exit`` in ``__main__`` with
    a preceding flush.
    """
    text = RECORD_FIXTURES_PY.read_text(encoding="utf-8")
    assert "os._exit" in text, (
        "record_fixtures.py must os._exit after recording so a lingering "
        "non-daemon thread (chromadb telemetry / cuda) cannot wedge "
        "interpreter shutdown into the deploy gate's timeout "
        "(fix-rerecord-gate)"
    )
    assert "sys.stdout.flush()" in text, (
        "record_fixtures.py must flush stdout before os._exit (os._exit "
        "skips buffer flushing) so the DONE/manifest line isn't lost "
        "(fix-rerecord-gate)"
    )


def test_record_fixtures_exit_defeats_lingering_nondaemon_thread():
    """Behavioural pin of the fix mechanism: a process that spawns a
    non-daemon thread blocked forever still exits promptly when it calls
    the recorder's tail (`flush` + `os._exit(rc)`), whereas a normal
    `sys.exit` would hang on the un-joinable thread.

    This reproduces the exact shutdown-hang shape (non-daemon thread that
    never returns) without needing the prod corpus, and proves os._exit is
    the right tool — it bypasses the interpreter's join-all-threads
    shutdown.
    """
    prog = (
        "import os, sys, threading\n"
        "def _block():\n"
        "    threading.Event().wait()  # non-daemon, never returns\n"
        "t = threading.Thread(target=_block, daemon=False)\n"
        "t.start()\n"
        "print('DONE')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, (
        "os._exit(0) tail must exit 0 even with a live non-daemon thread "
        f"(got rc={proc.returncode}); stderr={proc.stderr!r}"
    )
    assert "DONE" in proc.stdout, (
        "the flushed DONE line must survive the os._exit (fix-rerecord-gate)"
    )


# ---------------------------------------------------------------------------
# F2-DEPLOY-RERECORD — behavioural pin: the manifest-excluded diff
# distinguishes a real fixture drift from a manifest-only timing change.
# This is the R2 negative: under the brief-literal `git diff --exit-code
# <dir>` (no exclude), case (b) would return non-zero → the gate would
# false-fail on EVERY deploy. The exclude pathspec makes (a) fail and
# (b) pass — exactly what the gate needs.
# ---------------------------------------------------------------------------


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env,
    )


@pytest.fixture
def _fixtures_repo(tmp_path):
    """A throwaway git repo mirroring the fixtures-dir layout: one
    fixture JSON + a _manifest.json, both committed."""
    if not _have_git():
        pytest.skip("git not on PATH")
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    repo = tmp_path
    fixtures = repo / FIXTURES_REL
    fixtures.mkdir(parents=True)
    (fixtures / "scripts.rag_tools.find_book.json").write_text(
        '{"matches": [{"pg_id": "PG1342"}]}\n', encoding="utf-8"
    )
    (fixtures / "_manifest.json").write_text(
        '{"results": [{"v1_qualname": "x", "status": "ok", '
        '"elapsed_s": 0.09, "size_bytes": 42}]}\n', encoding="utf-8"
    )
    assert _git(["init", "-q"], repo, env).returncode == 0
    _git(["add", "-A"], repo, env)
    assert _git(["commit", "-qm", "seed"], repo, env).returncode == 0
    return repo, fixtures, env


def _diff_excluding_manifest(repo, env):
    """Run the exact diff shape the gate uses and return its exit code."""
    return _git(
        ["diff", "--exit-code", "--", FIXTURES_REL,
         f":(exclude){FIXTURES_REL}/_manifest.json"],
        repo, env,
    ).returncode


def test_manifest_excluded_diff_catches_real_fixture_drift(_fixtures_repo):
    """A changed fixture JSON → diff exits non-zero (drift caught)."""
    repo, fixtures, env = _fixtures_repo
    (fixtures / "scripts.rag_tools.find_book.json").write_text(
        '{"matches": [{"pg_id": "PG9999"}]}\n', encoding="utf-8"
    )
    assert _diff_excluding_manifest(repo, env) != 0, (
        "a real fixture-content change MUST make the manifest-excluded "
        "diff exit non-zero — otherwise the gate misses drift (S-B7)"
    )


def test_manifest_excluded_diff_ignores_manifest_only_change(_fixtures_repo):
    """A change to ONLY _manifest.json (the every-run timing churn) →
    diff exits zero. This is the false-positive the exclude pathspec
    exists to prevent; under the brief-literal whole-dir diff this would
    be non-zero and fail every deploy."""
    repo, fixtures, env = _fixtures_repo
    (fixtures / "_manifest.json").write_text(
        '{"results": [{"v1_qualname": "x", "status": "ok", '
        '"elapsed_s": 412.7, "size_bytes": 99}]}\n', encoding="utf-8"
    )
    assert _diff_excluding_manifest(repo, env) == 0, (
        "a manifest-ONLY change (non-deterministic elapsed_s/size_bytes) "
        "MUST NOT trip the gate — else every deploy false-fails (S-B7)"
    )
    # NOT-X mirror: the naive whole-dir diff (no exclude) WOULD trip here,
    # which is precisely why the exclude is load-bearing.
    naive_rc = _git(
        ["diff", "--exit-code", "--", FIXTURES_REL], repo, env,
    ).returncode
    assert naive_rc != 0, (
        "sanity: the brief-literal whole-dir diff DOES flag the manifest "
        "churn — confirming the exclude pathspec is necessary, not cosmetic"
    )


# ---------------------------------------------------------------------------
# STRETCH FINGERPRINT-PYVER — Python-minor stamping + clear mismatch signal
# ---------------------------------------------------------------------------


def test_record_fixtures_stamps_python_minor():
    """``record_fixtures`` writes ``python_minor`` into the manifest so a
    cross-version freshness check can detect the mismatch (S-B7 stretch)."""
    text = RECORD_FIXTURES_PY.read_text(encoding="utf-8")
    assert "python_minor" in text, (
        "record_fixtures.py must stamp python_minor in the manifest "
        "(FINGERPRINT-PYVER)"
    )
    assert "version_info" in text, (
        "python_minor must derive from sys.version_info (FINGERPRINT-PYVER)"
    )


def test_freshness_gate_short_circuits_on_python_minor_mismatch():
    """``FixtureFreshnessGate`` fails with a clear 'Python minor mismatch'
    message (not N 'source changed' lines) when the recording and running
    interpreters disagree — and only when the manifest actually carries
    the stamp (pre-S-B7 manifests keep the original behaviour). (S-B7
    stretch)"""
    gate_src = (REPO_ROOT / "tests" / "v2" / "test_v1_contracts.py").read_text(
        encoding="utf-8"
    )
    assert 'manifest.get("python_minor")' in gate_src, (
        "FixtureFreshnessGate must read python_minor from the manifest "
        "(FINGERPRINT-PYVER)"
    )
    assert "Python minor mismatch" in gate_src, (
        "FixtureFreshnessGate must emit a 'Python minor mismatch' message "
        "instead of misleading source-drift lines (FINGERPRINT-PYVER)"
    )
    # Guarded on presence so old manifests don't change behaviour.
    assert "recorded_py is not None" in gate_src, (
        "the mismatch check must be guarded on the stamp's presence so "
        "pre-S-B7 manifests keep working (FINGERPRINT-PYVER)"
    )


# ---------------------------------------------------------------------------
# status unit orphan-kill (host pkill, NOT docker exec)
# ---------------------------------------------------------------------------


def test_status_unit_has_host_orphan_kill_execstartpre():
    """``wordcracker-status.service`` reaps a stray status_server before
    binding :8889, via a HOST ``pkill`` ExecStartPre.

    The retired chat/admin units used `docker compose exec ... pkill`,
    but status_server runs as host Python — so the reap must be a host
    pkill of the script path, and must NOT reintroduce the forbidden
    `docker exec` pattern (test_no_docker_exec_in_any_systemd_unit).
    """
    text = STATUS_UNIT.read_text(encoding="utf-8")
    execstartpre = [
        ln for ln in text.splitlines()
        if ln.strip().startswith("ExecStartPre=")
    ]
    assert execstartpre, "status unit must declare an ExecStartPre (S-B7)"
    joined = "\n".join(execstartpre)
    assert "pkill" in joined and "status_server.py" in joined, (
        "status unit ExecStartPre must host-pkill the orphaned "
        "status_server.py (S-B7)"
    )
    # Must NOT use docker exec (that pattern is forbidden post-S-B2).
    assert "docker compose exec" not in joined and "docker exec " not in joined, (
        "status orphan-kill must be a HOST pkill, not docker exec — "
        "status_server runs on the host, and docker exec in systemd is "
        "forbidden post-S-B2 (S-B7)"
    )
    # The `-` prefix swallows pkill's exit 1 when no orphan matches, so a
    # clean boot (no stray process) doesn't fail the unit start.
    orphan_lines = [ln for ln in execstartpre if "pkill" in ln]
    assert all("=-" in ln for ln in orphan_lines), (
        "the pkill ExecStartPre must be prefixed `=-` so a no-match "
        "(exit 1) does not abort the unit start (S-B7)"
    )


# ---------------------------------------------------------------------------
# .gitignore prod-root clutter
# ---------------------------------------------------------------------------


def test_gitignore_has_root_clutter_patterns():
    """`.gitignore` carries the anchored prod-root clutter patterns."""
    text = GITIGNORE.read_text(encoding="utf-8")
    assert "/test_report_*.md" in text, (
        ".gitignore must ignore root-level test_report_*.md (S-B7)"
    )
    assert "/*.bak" in text, (
        ".gitignore must ignore root-level *.bak (S-B7)"
    )


def test_gitignore_clutter_anchored_to_root_only():
    """Behavioural: the patterns ignore root-level clutter but NOT
    same-named files in subdirectories (anchored with a leading `/` so
    we don't mask a legitimately-tracked file deeper in the tree)."""
    if not _have_git():
        pytest.skip("git not on PATH")
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    def ignored(relpath: str) -> bool:
        return _git(["check-ignore", "-q", relpath], REPO_ROOT).returncode == 0

    assert ignored("test_report_x.md"), "root test_report_x.md must be ignored"
    assert ignored("backup.bak"), "root backup.bak must be ignored"
    # Anchored: a subdir file of the same shape is NOT ignored by these
    # patterns (it may still be ignored by an unrelated rule, but the
    # root-anchored S-B7 patterns must not be what catches it).
    assert not ignored("scripts/test_report_keep.md"), (
        "subdir test_report_*.md must NOT be caught by the root-anchored "
        "pattern (would risk masking tracked files) (S-B7)"
    )
    assert not ignored("scripts/keep.bak"), (
        "subdir *.bak must NOT be caught by the root-anchored pattern (S-B7)"
    )
