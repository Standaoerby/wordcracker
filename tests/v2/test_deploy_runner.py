"""Characterization tests for the deploy runner (AUTONOMY_RUNBOOK_R-30 §4).

WP-0 #2. Pins the runner contract:

  - happy path: every gate green → deploy lives, one DEPLOY_OK audit line, no
    rollback;
  - the rollback TARGET is captured BEFORE deploy (pre-deploy image);
  - ANY runner-owned gate (verify / smoke / eval-tripwire) failure → roll back
    to the previous SHA, ONE audit line, STOP — later gates do not run;
  - a deploy.sh failure is NOT re-rolled-back by the runner (deploy.sh owns its
    own internal rollback) — record + stop;
  - no rollback target (cold start) and a failed rollback are distinct, loud
    terminal states, never a silent success or a blind retry;
  - the kill-switch refuses before anything is touched;
  - the exact subprocess argv each external step issues — the deploy-saga
    lesson: the path is exactly `scripts/deploy.sh`, checkout main + ff-only
    pull, verify with the SHA, rollback via `--rollback`.

Every external effect is overridden with a pure fake, so the suite needs no
docker / git / live endpoint and runs on bare ubuntu-latest in `predeploy`.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy.deploy_runner import (  # noqa: E402
    DeployRunner,
    StepResult,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_KILLSWITCH,
    EXIT_DEPLOY_FAILED,
    EXIT_ROLLED_BACK,
    EXIT_NO_ROLLBACK_TARGET,
    EXIT_ROLLBACK_FAILED,
)
import scripts.autonomy.deploy_runner as runner_mod  # noqa: E402


def _ok(name, **kw):
    return StepResult(name, ok=True, **kw)


def _fail(name, **kw):
    return StepResult(name, ok=False, **kw)


def _kinds(calls):
    """Reduce the recorder's mixed call list to bare step kinds."""
    return [c[0] if isinstance(c, tuple) else c for c in calls]


def _events(lines):
    """The EVENT token (2nd field) of each audit line."""
    return [ln.split("  ")[1] for ln in lines]


class _Recorder(DeployRunner):
    """A runner whose every effect method is scripted from `plan` and which
    records the ordered call list + the audit lines. Defaults make the happy
    path green; a test overrides only the steps it cares about."""

    def __init__(self, plan=None):
        super().__init__(repo_root=Path("."), runner_id="test",
                         clock=lambda: "2026-06-15T00:00:00Z",
                         log=lambda *a, **k: None)
        self.plan = plan or {}
        self.calls = []
        self.audit_lines = []

    def write_audit(self, line):          # capture instead of writing a file
        self.audit_lines.append(line)

    def check_killswitch(self):
        self.calls.append("killswitch")
        return self.plan.get("killswitch", _ok("killswitch", detail="clear"))

    def sync_main(self):
        self.calls.append("sync")
        return self.plan.get("sync", _ok("sync"))

    def current_head_sha(self):
        self.calls.append("head")
        return self.plan.get("head", _ok("head_sha", detail="newsha"))

    def capture_previous_sha(self):
        self.calls.append("capture")
        return self.plan.get("capture", _ok("previous_sha", detail="prevsha"))

    def run_deploy(self):
        self.calls.append("deploy")
        return self.plan.get("deploy", _ok("deploy", exit_code=0))

    def run_verify(self, sha):
        self.calls.append(("verify", sha))
        return self.plan.get("verify", _ok("verify"))

    def run_smoke(self, sha):
        self.calls.append(("smoke", sha))
        return self.plan.get("smoke", _ok("smoke"))

    def run_eval_tripwire(self, sha):
        self.calls.append(("eval", sha))
        return self.plan.get("eval", _ok("eval_tripwire"))

    def run_rollback(self, prev):
        self.calls.append(("rollback", prev))
        return self.plan.get("rollback", _ok("rollback", exit_code=0))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class HappyPath(unittest.TestCase):
    def test_all_gates_green_deploys_and_audits_ok(self):
        r = _Recorder()
        self.assertEqual(r.run(), EXIT_OK)
        self.assertNotIn("rollback", _kinds(r.calls))
        self.assertEqual(_events(r.audit_lines), ["DEPLOY_OK"])
        self.assertEqual(len(r.audit_lines), 1)

    def test_capture_previous_runs_before_deploy(self):
        # The rollback target must be the image live BEFORE deploy swaps it.
        r = _Recorder()
        r.run()
        self.assertLess(r.calls.index("capture"), r.calls.index("deploy"))

    def test_target_sha_threads_to_every_gate(self):
        r = _Recorder({"head": _ok("head_sha", detail="abc123")})
        r.run()
        self.assertIn(("verify", "abc123"), r.calls)
        self.assertIn(("smoke", "abc123"), r.calls)
        self.assertIn(("eval", "abc123"), r.calls)

    def test_default_eval_tripwire_stub_passes(self):
        # #4 not yet wired: run_eval_tripwire is still a pass-through stub so the
        # happy path stays green until that PR lands. (run_smoke is real as of
        # #3 — its wiring + fail-closed behaviour are pinned in
        # tests/v2/test_smoke_battery.py.)
        base = DeployRunner(repo_root=Path("."), log=lambda *a, **k: None)
        self.assertTrue(base.run_eval_tripwire("x").ok)


# ---------------------------------------------------------------------------
# Kill-switch + preconditions
# ---------------------------------------------------------------------------

class KillSwitch(unittest.TestCase):
    def test_engaged_refuses_before_anything(self):
        r = _Recorder({"killswitch": _fail("killswitch", detail="frozen")})
        self.assertEqual(r.run(), EXIT_KILLSWITCH)
        self.assertNotIn("sync", r.calls)
        self.assertNotIn("deploy", r.calls)
        self.assertEqual(_events(r.audit_lines), ["KILLSWITCH_REFUSE"])


class Precondition(unittest.TestCase):
    def test_sync_failure_stops_before_deploy(self):
        r = _Recorder({"sync": _fail("sync", detail="detached HEAD")})
        self.assertEqual(r.run(), EXIT_PRECONDITION)
        self.assertNotIn("deploy", r.calls)
        self.assertEqual(_events(r.audit_lines), ["PRECONDITION_FAIL"])

    def test_empty_head_sha_stops_before_deploy(self):
        r = _Recorder({"head": _ok("head_sha", detail="")})
        self.assertEqual(r.run(), EXIT_PRECONDITION)
        self.assertNotIn("deploy", r.calls)
        self.assertEqual(_events(r.audit_lines), ["PRECONDITION_FAIL"])


# ---------------------------------------------------------------------------
# deploy.sh failure — runner must NOT re-roll-back (deploy.sh owns that)
# ---------------------------------------------------------------------------

class DeployScriptFailure(unittest.TestCase):
    def test_runner_does_not_reroll_back(self):
        r = _Recorder({"deploy": _fail("deploy", exit_code=10, detail="deploy.sh exit 10")})
        self.assertEqual(r.run(), EXIT_DEPLOY_FAILED)
        self.assertNotIn("rollback", _kinds(r.calls))
        self.assertNotIn("verify", _kinds(r.calls))  # post-deploy gates never run
        self.assertEqual(_events(r.audit_lines), ["DEPLOY_FAILED"])
        self.assertIn("deploy_rc=10", r.audit_lines[0])


# ---------------------------------------------------------------------------
# Runner-owned gate failures → rollback to the previous SHA
# ---------------------------------------------------------------------------

class RunnerGateRollback(unittest.TestCase):
    def test_verify_failure_rolls_back(self):
        r = _Recorder({"verify": _fail("verify", exit_code=5, detail="sha mismatch")})
        self.assertEqual(r.run(), EXIT_ROLLED_BACK)
        self.assertIn(("rollback", "prevsha"), r.calls)
        self.assertNotIn("smoke", _kinds(r.calls))  # later gates skipped
        self.assertNotIn("eval", _kinds(r.calls))
        self.assertEqual(_events(r.audit_lines), ["ROLLBACK_OK"])

    def test_smoke_failure_rolls_back(self):
        r = _Recorder({"smoke": _fail("smoke", detail="probe red")})
        self.assertEqual(r.run(), EXIT_ROLLED_BACK)
        self.assertIn(("rollback", "prevsha"), r.calls)
        self.assertNotIn("eval", _kinds(r.calls))
        self.assertEqual(_events(r.audit_lines), ["ROLLBACK_OK"])

    def test_eval_tripwire_failure_rolls_back(self):
        r = _Recorder({"eval": _fail("eval_tripwire", detail="pass-rate below baseline")})
        self.assertEqual(r.run(), EXIT_ROLLED_BACK)
        self.assertIn(("rollback", "prevsha"), r.calls)
        self.assertEqual(_events(r.audit_lines), ["ROLLBACK_OK"])

    def test_rollback_line_names_the_failing_gate(self):
        r = _Recorder({"smoke": _fail("smoke", detail="probe red")})
        r.run()
        self.assertIn("gate=smoke", r.audit_lines[0])
        self.assertIn("previous=prevsha", r.audit_lines[0])


class RollbackEdgeCases(unittest.TestCase):
    def test_no_previous_sha_means_no_rollback_call(self):
        # Cold start: capture returns empty. A post-deploy gate failure must
        # NOT invoke run_rollback with an empty SHA — it's a manual-recovery
        # dead-end, surfaced loudly.
        r = _Recorder({"capture": _ok("previous_sha", detail=""),
                       "verify": _fail("verify", detail="mismatch")})
        self.assertEqual(r.run(), EXIT_NO_ROLLBACK_TARGET)
        self.assertNotIn("rollback", _kinds(r.calls))
        self.assertEqual(_events(r.audit_lines), ["ROLLBACK_NO_TARGET"])

    def test_rollback_failure_is_a_bad_state(self):
        r = _Recorder({"verify": _fail("verify", detail="mismatch"),
                       "rollback": _fail("rollback", exit_code=11, detail="rollback verify red")})
        self.assertEqual(r.run(), EXIT_ROLLBACK_FAILED)
        self.assertEqual(_events(r.audit_lines), ["ROLLBACK_FAILED"])
        self.assertIn("rollback_rc=11", r.audit_lines[0])

    def test_single_pass_no_blind_retry(self):
        # No effect runs twice: one deploy, one verify, one rollback, then STOP.
        r = _Recorder({"verify": _fail("verify", detail="mismatch")})
        r.run()
        self.assertEqual(r.calls.count("deploy"), 1)
        self.assertEqual(sum(1 for c in r.calls if isinstance(c, tuple) and c[0] == "verify"), 1)
        self.assertEqual(sum(1 for c in r.calls if isinstance(c, tuple) and c[0] == "rollback"), 1)
        self.assertEqual(len(r.audit_lines), 1)  # exactly one terminal outcome


# ---------------------------------------------------------------------------
# Subprocess contracts — the exact argv each external step issues
# ---------------------------------------------------------------------------

_FakeProc = namedtuple("_FakeProc", "returncode stdout stderr")


class _ArgvRunner(DeployRunner):
    """Captures every `_run` argv and returns canned results via `proc_fn`."""

    def __init__(self, proc_fn=None):
        super().__init__(repo_root=Path("."), runner_id="test",
                         clock=lambda: "T", log=lambda *a, **k: None)
        self.argv = []
        self._proc_fn = proc_fn or (lambda argv: _FakeProc(0, "", ""))

    def _run(self, argv):
        self.argv.append(list(argv))
        return self._proc_fn(list(argv))


class SubprocessContracts(unittest.TestCase):
    def test_deploy_invokes_bash_scripts_deploy_sh(self):
        r = _ArgvRunner()
        res = r.run_deploy()
        self.assertEqual(r.argv[-1], ["bash", "scripts/deploy.sh"])
        self.assertTrue(res.ok)

    def test_verify_invokes_verify_script_with_sha(self):
        r = _ArgvRunner()
        r.run_verify("abc123")
        self.assertEqual(r.argv[-1], ["bash", "scripts/verify_deployed_image.sh", "abc123"])

    def test_rollback_invokes_deploy_sh_rollback(self):
        r = _ArgvRunner()
        r.run_rollback("def456")
        self.assertEqual(r.argv[-1], ["bash", "scripts/deploy.sh", "--rollback", "def456"])

    def test_deploy_nonzero_exit_is_not_ok(self):
        r = _ArgvRunner(lambda argv: _FakeProc(10, "", "boom"))
        res = r.run_deploy()
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 10)

    def test_sync_is_checkout_main_then_ff_only_pull(self):
        r = _ArgvRunner()
        res = r.sync_main()
        self.assertEqual(r.argv[0], ["git", "checkout", "main"])
        self.assertEqual(r.argv[1], ["git", "pull", "--ff-only"])
        self.assertTrue(res.ok)

    def test_sync_checkout_failure_short_circuits_pull(self):
        def fn(argv):
            return _FakeProc(1, "", "no such branch") if argv[:2] == ["git", "checkout"] else _FakeProc(0, "", "")
        r = _ArgvRunner(fn)
        res = r.sync_main()
        self.assertFalse(res.ok)
        self.assertEqual(r.argv, [["git", "checkout", "main"]])  # pull never attempted

    def test_head_sha_uses_rev_parse_short(self):
        r = _ArgvRunner(lambda argv: _FakeProc(0, "deadbee\n", ""))
        res = r.current_head_sha()
        self.assertEqual(r.argv[-1], ["git", "rev-parse", "--short", "HEAD"])
        self.assertEqual(res.detail, "deadbee")


class CapturePreviousSha(unittest.TestCase):
    @staticmethod
    def _proc_fn(ps_id, image):
        def fn(argv):
            if argv[:2] == ["docker", "ps"]:
                return _FakeProc(0, (ps_id + "\n") if ps_id else "", "")
            if argv[:2] == ["docker", "inspect"]:
                return _FakeProc(0, (image + "\n") if image else "", "")
            return _FakeProc(0, "", "")
        return fn

    def test_running_family_image_yields_its_tag(self):
        r = _ArgvRunner(self._proc_fn("cid123", "wordcracker-textlab:abc123"))
        self.assertEqual(r.capture_previous_sha().detail, "abc123")

    def test_no_container_is_cold_start_empty(self):
        r = _ArgvRunner(self._proc_fn("", ""))
        res = r.capture_previous_sha()
        self.assertTrue(res.ok)  # cold start is a clean state, not an error
        self.assertEqual(res.detail, "")

    def test_non_family_image_yields_empty(self):
        r = _ArgvRunner(self._proc_fn("cid123", "some/other-image:latest"))
        self.assertEqual(r.capture_previous_sha().detail, "")


# ---------------------------------------------------------------------------
# Audit-log sink (#5 seam) + line format
# ---------------------------------------------------------------------------

class AuditLog(unittest.TestCase):
    def test_line_has_event_target_previous_runner_detail(self):
        r = _Recorder()
        line = r._audit("DEPLOY_OK", target="abc", previous="def", deploy_rc=0, detail="ok")
        for token in ("DEPLOY_OK", "target=abc", "previous=def", "deploy_rc=0",
                      "runner=test", ":: ok"):
            self.assertIn(token, line)

    def test_empty_previous_renders_dash(self):
        r = _Recorder()
        line = r._audit("ROLLBACK_NO_TARGET", target="abc", previous="", gate="verify")
        self.assertIn("previous=-", line)

    def test_write_audit_appends_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "sub" / "AUTONOMY_LOG.md"   # parent created on demand
            runner = DeployRunner(repo_root=Path(td), audit_path=log,
                                  clock=lambda: "T", log=lambda *a, **k: None)
            runner.write_audit("line one")
            runner.write_audit("line two")
            self.assertEqual(log.read_text(encoding="utf-8"), "line one\nline two\n")


# ---------------------------------------------------------------------------
# Real kill-switch impl (#5 seam, base behaviour) + CLI wiring
# ---------------------------------------------------------------------------

class KillSwitchRealImpl(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("WC_AUTONOMY_KILLSWITCH")
        os.environ.pop("WC_AUTONOMY_KILLSWITCH", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("WC_AUTONOMY_KILLSWITCH", None)
        else:
            os.environ["WC_AUTONOMY_KILLSWITCH"] = self._saved

    def test_env_set_engages(self):
        os.environ["WC_AUTONOMY_KILLSWITCH"] = "1"
        r = DeployRunner(repo_root=Path("."), log=lambda *a, **k: None)
        self.assertFalse(r.check_killswitch().ok)

    def test_clear_when_unset_and_no_flag_file(self):
        with tempfile.TemporaryDirectory() as td:
            r = DeployRunner(repo_root=Path(td), log=lambda *a, **k: None)
            self.assertTrue(r.check_killswitch().ok)

    def test_flag_file_engages(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "AUTONOMY_KILLSWITCH").write_text("freeze", encoding="utf-8")
            r = DeployRunner(repo_root=Path(td), log=lambda *a, **k: None)
            self.assertFalse(r.check_killswitch().ok)


class CliMain(unittest.TestCase):
    def test_main_wires_repo_and_runner_id(self):
        captured = {}

        class _Stub(runner_mod.DeployRunner):
            def __init__(self, **kw):
                captured.update(kw)

            def run(self):
                return 0

        orig = runner_mod.DeployRunner
        runner_mod.DeployRunner = _Stub
        try:
            rc = runner_mod.main(["--repo", "/tmp/x", "--runner-id", "nightly"])
        finally:
            runner_mod.DeployRunner = orig
        self.assertEqual(rc, 0)
        self.assertEqual(captured["repo_root"], Path("/tmp/x"))
        self.assertEqual(captured["runner_id"], "nightly")


if __name__ == "__main__":
    unittest.main()
