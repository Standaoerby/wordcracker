"""S-B10 acceptance: deploy hygiene — "the perfect deploy".

R2 negative-test gate for the 2026-06-04 S-B10 block. Three deploy/CI
splinters, each caught live in R-25:

  1. **re-record bounded + process-tree kill** — the deploy-time
     fixture re-record hung after a successful DONE: ``docker compose
     run --rm -T`` went ``<defunct>`` and the bare ``timeout 900`` only
     SIGTERM'd the compose CLIENT, never the daemon-side container, so
     ``deploy.sh`` waited forever. Fix: ``timeout --kill-after`` (TERM →
     KILL escalation) + an explicit ``--name``d container that is
     ``docker rm -f``'d after the run (the real process-tree kill) and
     by the shared cleanup trap.

  2. **self-cleaning working tree** — the ``--skip-heavy`` run leaves a
     degraded ``_manifest.json`` + rewritten fixtures in the tree. The
     branch ``git checkout --`` restores tracked files; a final
     ``git status --porcelain`` guard + ``git clean`` removes any
     untracked leftover so the next deploy's dirty-check sees a pristine
     tree. (Negative: ``checkout --`` alone leaves an untracked file.)

  3. **chat-log capture** — ``deploy.sh`` streams the new chat
     container's logs to ``/tmp/deploy_<sha>_chat.log`` over the
     verify+probe window so warmup / ``[llm]`` diagnostics survive a
     rollback ``--force-recreate``.

Plus the version-bump CI ref fix (``check_version_bump.py`` merge-base
mode + ``predeploy.yml`` PR-head checkout) — covered in
``test_w18_check_version_bump.py``; here we pin the workflow shape.

Like the other deploy tests these parse the script / workflow text (no
docker / curl) so they run on dev boxes and CI without a daemon. The one
behavioural test uses a throwaway git repo to pin the restore semantics.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "scripts" / "deploy.sh"
PREDEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "predeploy.yml"

FIXTURES_REL = "scripts/v2/contracts/fixtures"


def _strip_bash_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _have_git() -> bool:
    try:
        return subprocess.run(
            ["git", "--version"], capture_output=True, timeout=5
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# #1 — bounded re-record + process-tree kill
# ---------------------------------------------------------------------------


def test_rerecord_timeout_escalates_with_kill_after():
    """``timeout`` must carry ``--kill-after`` so a SIGTERM-swallowing
    client is force-SIGKILL'd inside the budget.

    R2 negative: the pre-S-B10 gate used a bare ``timeout 900`` — if the
    target ignores/slow-handles SIGTERM, ``timeout`` never escalates and
    the deploy hangs past the budget (the R-25 symptom).

    R-28 #1: the recorder now runs DETACHED and the budget-bounded client
    is ``docker wait`` (the attached compose client was itself a hang
    class — it wedged in attach teardown AFTER the container exited, so
    the gate sat out the full budget on every deploy). The pin therefore
    targets the ``docker wait`` line, which must still be wrapped in
    ``timeout --kill-after``.
    """
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    rec = code.find("scripts.v2.contracts.record_fixtures")
    assert rec != -1, "deploy.sh must invoke the recorder"
    wait_idx = code.find("docker wait", rec)
    assert wait_idx != -1, (
        "the re-record gate must run the recorder detached and `docker wait` "
        "for it — the attached compose client is a known hang class (R-28 #1)"
    )
    block = code[max(0, wait_idx - 300): wait_idx + 200]
    assert "timeout --kill-after" in block, (
        "the re-record `docker wait` must be wrapped in `timeout --kill-after` "
        "to escalate SIGTERM→SIGKILL so a wedged client cannot hang the deploy "
        "past the budget (S-B10 #1 / R-28 #1)"
    )


def test_rerecord_names_container_and_reaps_it():
    """The recorder container is ``--name``d and ``docker rm -f``'d after
    the run — the actual process-tree kill (``timeout`` only signals the
    compose client; the daemon-side container survives otherwise)."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    block = code[max(0, idx - 500): idx + 700]
    assert "--name" in block and "RR_CONTAINER" in block, (
        "re-record `run` must pin an explicit container --name so it can be "
        "reaped (S-B10 #1)"
    )
    # An explicit reap of that container must follow the run.
    after = code[idx: idx + 700]
    assert "docker rm -f" in after, (
        "re-record gate must `docker rm -f` the named container after the run "
        "to reap an orphaned/defunct daemon container (S-B10 #1)"
    )


def test_cleanup_trap_reaps_container_and_logger():
    """A single ``trap cleanup EXIT`` drives teardown of the re-record
    container AND the chat-log writer on every exit path."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in text, (
        "deploy.sh must register one `cleanup` EXIT trap (S-B10)"
    )
    # cleanup body must kill the logger PID and rm -f the container.
    start = text.find("cleanup() {")
    end = text.find("}", start)
    assert start != -1 and end != -1, "deploy.sh must define a cleanup() function"
    body = text[start:end]
    assert "kill" in body and "CHAT_LOG_PID" in body, (
        "cleanup() must kill the background chat-log writer (S-B10 #3a)"
    )
    assert "docker rm -f" in body and "RR_CONTAINER" in body, (
        "cleanup() must reap the re-record container on any exit (S-B10 #1)"
    )


def test_rerecord_gate_still_advisory_after_hardening():
    """The hardening must not turn the advisory gate into a deploy-failer:
    no ``exit`` between the recorder invocation and prune (S-B10 keeps the
    fix-rerecord-gate advisory semantics intact)."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    prune = code.find("docker image rm")
    block = code[idx:prune]
    assert "exit 13" not in block and "exit 14" not in block, (
        "re-record gate must stay advisory — no exit-on-drift/error (S-B10)"
    )
    assert "WARN" in block, "advisory gate must WARN loudly (S-B10)"


# ---------------------------------------------------------------------------
# #2 — self-cleaning working tree
# ---------------------------------------------------------------------------


def test_rerecord_gate_has_status_clean_guard():
    """A final ``git status --porcelain`` guard + ``git clean`` ensures the
    fixtures dir is pristine after the gate (acceptance: git status clean
    on prod after deploy)."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    idx = code.find("scripts.v2.contracts.record_fixtures")
    prune = code.find("docker image rm")
    # Whole gate body (recorder → prune): R-28's detached-run plumbing sits
    # between the invocation and the guard, so a fixed-width window under-runs.
    block = code[idx:prune]
    assert "git status --porcelain" in block, (
        "re-record gate must assert a clean tree via `git status --porcelain` "
        "after restore (S-B10 #2)"
    )
    assert "git clean" in block, (
        "re-record gate must `git clean` untracked leftovers so the next "
        "deploy's dirty-check sees a pristine tree (S-B10 #2)"
    )


@pytest.fixture
def _fixtures_repo(tmp_path):
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
        '{"matches": []}\n', encoding="utf-8"
    )
    (fixtures / "_manifest.json").write_text('{"results": []}\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, env=env, check=True)
    return repo, fixtures, env


def _porcelain(repo, env) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain", "--", FIXTURES_REL],
        cwd=repo, env=env, capture_output=True, text=True,
    ).stdout.strip()


def test_restore_sequence_leaves_tree_clean(_fixtures_repo):
    """Behavioural pin of the #2 fix: after the recorder mutates a tracked
    fixture AND drops a NEW untracked one, the gate's restore sequence
    (`git checkout --` + `git clean -fdq`) returns the tree to pristine.

    NOT-X mirror: `git checkout --` ALONE (the pre-S-B10 restore) leaves
    the untracked file behind — a dirty mine for the next deploy.
    """
    repo, fixtures, env = _fixtures_repo
    # Recorder churn: modify a tracked fixture + add an untracked one.
    (fixtures / "scripts.rag_tools.find_book.json").write_text(
        '{"matches": [{"pg_id": "PG1"}]}\n', encoding="utf-8"
    )
    (fixtures / "scripts.rag_tools.brand_new.json").write_text(
        '{"x": 1}\n', encoding="utf-8"
    )
    # Pre-S-B10 restore: checkout only.
    subprocess.run(["git", "checkout", "--", FIXTURES_REL], cwd=repo, env=env)
    assert _porcelain(repo, env) != "", (
        "sanity: `git checkout --` alone must leave the untracked file dirty "
        "— this is exactly the dirty mine S-B10 #2 closes"
    )
    # S-B10 restore: checkout + clean.
    subprocess.run(["git", "clean", "-fdq", "--", FIXTURES_REL], cwd=repo, env=env)
    assert _porcelain(repo, env) == "", (
        "S-B10 restore (checkout + clean) must leave the fixtures dir "
        "pristine (git status clean)"
    )


# ---------------------------------------------------------------------------
# #3a — chat-log capture
# ---------------------------------------------------------------------------


def test_chat_logs_captured_to_tmp_after_compose_up():
    """``deploy.sh`` streams chat logs to ``/tmp/deploy_<sha>_chat.log``,
    started AFTER the compose up (so the new container exists) and BEFORE
    verify (so the warmup/[llm] window is captured before any rollback
    recreate discards it)."""
    code = _strip_bash_comments(DEPLOY_SH.read_text(encoding="utf-8"))
    assert "/tmp/deploy_${SHA}_chat.log" in code, (
        "deploy.sh must capture chat logs to /tmp/deploy_<sha>_chat.log (S-B10 #3a)"
    )
    up_pos = code.find("up -d --force-recreate")
    log_pos = code.find("/tmp/deploy_${SHA}_chat.log")
    verify_pos = code.find("verify_deployed_image.sh")
    assert up_pos != -1 and log_pos != -1 and verify_pos != -1
    assert up_pos < log_pos < verify_pos, (
        "chat-log capture must start after `compose up` and before verify "
        f"(got up@{up_pos} log@{log_pos} verify@{verify_pos}) (S-B10 #3a)"
    )
    # It must be a backgrounded `docker compose logs` of the chat service.
    block = code[log_pos - 300: log_pos + 100]
    assert "docker compose" in block and "logs" in block and "chat" in block, (
        "chat-log capture must background `docker compose logs ... chat` (S-B10 #3a)"
    )


# ---------------------------------------------------------------------------
# #3b — dirty-guard staged-case remediation hint
# ---------------------------------------------------------------------------


def test_dirty_guard_prints_checkout_head_hint():
    """The dirty-blocker error must spell out the staged-file remediation
    (`git checkout HEAD -- <file>`)."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert "git checkout HEAD --" in text, (
        "dirty-check error must hint `git checkout HEAD -- <file>` for the "
        "stale staged/modified-tracked case (S-B10 #3b)"
    )


# ---------------------------------------------------------------------------
# version-bump CI ref fix — workflow shape (logic in test_w18_check_version_bump)
# ---------------------------------------------------------------------------


def test_predeploy_workflow_uses_mergebase_on_pr_and_head_checkout():
    """``predeploy.yml`` must, on pull_request: check out the PR HEAD
    (not the merge commit) and compare with ``merge-base`` against
    ``origin/<base>``. push keeps ``HEAD~1``.

    R2 negative for the R-25 false-reds: comparing against the moving
    ``origin/main`` tip reds a merged/bumped branch; a merge-commit
    checkout would also collapse the merge-base back to the tip.
    """
    text = PREDEPLOY_YML.read_text(encoding="utf-8")
    assert "pull_request.head.sha" in text, (
        "predeploy.yml must check out the PR head sha (not the merge commit) "
        "so merge-base resolves to the fork point, not origin/main tip (S-B10)"
    )
    assert "merge-base" in text, (
        "predeploy.yml PR path must compare with --against merge-base (S-B10)"
    )
    assert "HEAD~1" in text, (
        "predeploy.yml push path must keep the HEAD~1 compare (S-B10)"
    )
