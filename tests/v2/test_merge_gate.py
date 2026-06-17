"""Characterization tests for the auto-merge step
(scripts/autonomy/merge_gate.py — AUTONOMY_RUNBOOK_R-30 §3 + §7). WP-0 #5.

Pins the merge-side control surface:

  - kill-switch engaged → the step REFUSES before `do_merge` runs (do_merge is
    never called), writes ONE MERGE_KILLSWITCH_REFUSE line, returns EXIT_KILLSWITCH;
  - clear → merges via the seam → ONE MERGE_OK line; a failed merge → MERGE_FAILED,
    no retry;
  - the §7 MERGE audit line carries PR#, SHA, flipped pins, scope-fence, eval-delta,
    in a stable byte-exact order, `-` for empties;
  - the unwired `do_merge` seam raises (fail-loud) when actually reached — never a
    silent half-merge.

Every effect (kill-switch, merge call, audit write) is overridden with a fake, so
the suite needs no `gh` / network / files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy import control  # noqa: E402
from scripts.autonomy import deploy_runner  # noqa: E402  (exit-code bands)
from scripts.autonomy.merge_gate import (  # noqa: E402
    MergeStep,
    GhMergeStep,
    MergeContext,
    MergeResult,
    EXIT_OK,
    EXIT_KILLSWITCH,
    EXIT_MERGE_FAILED,
)
from scripts.pr_watch import (  # noqa: E402
    evaluate,
    decide_deploy,
    PrFacts,
    PrWatcher,
    REQUIRED_CHECKS,
    FENCE_JOB,
    SELF_REVIEW_LABEL,
    MERGE,
    WAIT,
    SKIP_FENCED,
    SKIP_ALREADY,
    DEPLOY,
    DEPLOY_SKIP_CURRENT,
    DEPLOY_SKIP_BACKOFF,
)


def _green_checks():
    """All required checks SUCCESS — the eligible baseline tests mutate."""
    return {c: "SUCCESS" for c in REQUIRED_CHECKS}


def _events(lines):
    return [ln.split("  ")[1] for ln in lines]


class _FakeMerge(MergeStep):
    """A merge-step with the kill-switch + merge scripted; records the ordered
    call list + the audit lines. Defaults: clear kill-switch, do_merge succeeds."""

    def __init__(self, *, engaged=False, merge_ok=True, merge_detail="merged @ sha"):
        super().__init__(repo_root=Path("."), runner_id="test",
                         clock=lambda: "2026-06-16T00:00:00Z", log=lambda *a, **k: None)
        self._engaged = engaged
        self._merge_ok = merge_ok
        self._merge_detail = merge_detail
        self.calls = []
        self.merge_calls = []
        self.audit_lines = []

    def check_killswitch(self):
        self.calls.append("killswitch")
        if self._engaged:
            return control.KillSwitch(True, "frozen by test", "file")
        return control.KillSwitch(False, "clear", "clear")

    def do_merge(self, ctx):
        self.calls.append("merge")
        self.merge_calls.append(ctx)
        return MergeResult(ok=self._merge_ok, detail=self._merge_detail)

    def write_audit(self, line):
        self.audit_lines.append(line)


# ---------------------------------------------------------------------------
# Kill-switch blocks the merge-step
# ---------------------------------------------------------------------------

class MergeKillSwitch(unittest.TestCase):
    def test_engaged_refuses_before_merge(self):
        m = _FakeMerge(engaged=True)
        rc = m.run(MergeContext(pr=58, sha="abc123"))
        self.assertEqual(rc, EXIT_KILLSWITCH)
        self.assertNotIn("merge", m.calls)        # do_merge NEVER reached
        self.assertEqual(m.merge_calls, [])
        self.assertEqual(_events(m.audit_lines), ["MERGE_KILLSWITCH_REFUSE"])

    def test_unwired_do_merge_raises_when_clear(self):
        # With a clear kill-switch and the real (unimplemented) do_merge, a live
        # step raises loudly — no silent no-op merge. The engaged path above
        # proves the seam is never reached under a freeze.
        m = MergeStep(repo_root=Path("."), clock=lambda: "T", log=lambda *a, **k: None)
        m.check_killswitch = lambda: control.KillSwitch(False, "clear", "clear")
        m.write_audit = lambda line: None
        with self.assertRaises(NotImplementedError):
            m.run(MergeContext(pr=1))


# ---------------------------------------------------------------------------
# Clear → merge; failure → recorded, no retry
# ---------------------------------------------------------------------------

class MergeHappyPath(unittest.TestCase):
    def test_clear_merges_and_audits_ok(self):
        m = _FakeMerge()
        rc = m.run(MergeContext(pr=58, sha="abc123"))
        self.assertEqual(rc, EXIT_OK)
        self.assertEqual(m.calls, ["killswitch", "merge"])
        self.assertEqual(_events(m.audit_lines), ["MERGE_OK"])
        self.assertEqual(len(m.audit_lines), 1)

    def test_merge_failure_records_and_stops(self):
        m = _FakeMerge(merge_ok=False, merge_detail="gh: 422 not mergeable")
        rc = m.run(MergeContext(pr=58, sha="abc123"))
        self.assertEqual(rc, EXIT_MERGE_FAILED)
        self.assertEqual(_events(m.audit_lines), ["MERGE_FAILED"])
        self.assertIn("gh: 422 not mergeable", m.audit_lines[0])
        self.assertEqual(len(m.audit_lines), 1)  # exactly one terminal outcome


# ---------------------------------------------------------------------------
# §7 MERGE audit fields + stable format
# ---------------------------------------------------------------------------

class MergeAuditFields(unittest.TestCase):
    def test_line_carries_all_section7_merge_fields(self):
        m = _FakeMerge()
        m.run(MergeContext(pr=58, sha="abc123",
                           pins_flipped=["test_merge_killswitch", "test_audit_fields"],
                           scope_fence="clean", eval_delta="+0%"))
        line = m.audit_lines[0]
        for token in ("MERGE_OK", "pr=58", "sha=abc123",
                      "pins=test_merge_killswitch,test_audit_fields",
                      "scope_fence=clean", "eval_delta=+0%", "runner=test"):
            self.assertIn(token, line)

    def test_empty_pins_and_sha_render_dash(self):
        m = _FakeMerge()
        m.run(MergeContext(pr=58))           # no sha, no pins
        line = m.audit_lines[0]
        self.assertIn("pins=-", line)
        self.assertIn("sha=-", line)

    def test_field_order_is_byte_stable(self):
        m = _FakeMerge()
        m.run(MergeContext(pr=7, sha="s", pins_flipped=["p"],
                           scope_fence="clean", eval_delta="+1%"))
        self.assertEqual(
            m.audit_lines[0],
            "2026-06-16T00:00:00Z  MERGE_OK  pr=7  sha=s  pins=p  "
            "scope_fence=clean  eval_delta=+1%  runner=test  :: merged @ sha")


# ===========================================================================
# Phase B B2 — Part 1: GhMergeStep.do_merge wires `gh pr merge --squash --auto`
# ===========================================================================

def _gh_step():
    return GhMergeStep(repo_root=Path("."), runner_id="test",
                       clock=lambda: "T", log=lambda *a, **k: None)


class GhDoMerge(unittest.TestCase):
    def test_returncode_zero_means_automerge_enabled(self):
        cp = subprocess.CompletedProcess(args=[], returncode=0,
                                         stdout="✓ auto-merge enabled", stderr="")
        with mock.patch("subprocess.run", return_value=cp) as run:
            res = _gh_step().do_merge(MergeContext(pr=58))
        self.assertTrue(res.ok)
        argv = run.call_args.args[0]                 # the exact gh invocation
        self.assertEqual(argv[:3], ["gh", "pr", "merge"])
        self.assertIn("58", argv)
        self.assertIn("--squash", argv)             # locked merge method
        self.assertIn("--auto", argv)               # server-side backstop

    def test_returncode_zero_detail_defaults_to_pr(self):
        cp = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=cp):
            res = _gh_step().do_merge(MergeContext(pr=58))
        self.assertTrue(res.ok)
        self.assertIn("#58", res.detail)            # falls back to a stable label

    def test_nonzero_returncode_fails_with_code_and_stderr(self):
        cp = subprocess.CompletedProcess(args=[], returncode=1, stdout="",
                                         stderr="not mergeable")
        with mock.patch("subprocess.run", return_value=cp):
            res = _gh_step().do_merge(MergeContext(pr=58))
        self.assertFalse(res.ok)
        self.assertIn("1", res.detail)              # exit code surfaced
        self.assertIn("not mergeable", res.detail)  # stderr surfaced


class GhMergeRun(unittest.TestCase):
    """GhMergeStep.run() inherits the base kill-switch + audit contract; these
    pin that the REAL gh seam is gated by it (no `gh` under a freeze)."""

    def test_clear_killswitch_enables_and_audits_ok(self):
        with tempfile.TemporaryDirectory() as d:                 # no flag file → clear
            audit = []
            step = GhMergeStep(repo_root=Path(d), runner_id="test",
                               clock=lambda: "T", log=lambda *a, **k: None)
            step.write_audit = lambda line: audit.append(line)
            cp = subprocess.CompletedProcess(args=[], returncode=0,
                                             stdout="enabled", stderr="")
            with mock.patch.dict(os.environ, {control.KILLSWITCH_ENV: ""}), \
                 mock.patch("subprocess.run", return_value=cp):
                rc = step.run(MergeContext(pr=58, sha="abc"))
            self.assertEqual(rc, EXIT_OK)
            self.assertEqual(_events(audit), ["MERGE_OK"])

    def test_killswitch_file_refuses_before_gh(self):
        # Part 3 #3: kill-switch engaged (flag file via tmp repo_root) → rc 24,
        # do_merge (→ gh) NEVER called, exactly one MERGE_KILLSWITCH_REFUSE line.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / control.KILLSWITCH_FLAG_NAME).write_text("", encoding="utf-8")
            audit = []
            step = GhMergeStep(repo_root=Path(d), runner_id="test",
                               clock=lambda: "T", log=lambda *a, **k: None)
            step.write_audit = lambda line: audit.append(line)
            with mock.patch.dict(os.environ, {control.KILLSWITCH_ENV: ""}), \
                 mock.patch("subprocess.run") as run:
                rc = step.run(MergeContext(pr=58, sha="abc"))
            self.assertEqual(rc, EXIT_KILLSWITCH)
            run.assert_not_called()                              # gh NEVER ran
            self.assertEqual(_events(audit), ["MERGE_KILLSWITCH_REFUSE"])


# ===========================================================================
# Phase B B2 — Part 2 / STEP B: evaluate(facts) is the pure §3 decision
# ===========================================================================

class Evaluate(unittest.TestCase):
    def _facts(self, **over):
        base = dict(pr=58, checks=_green_checks(), fence_state="SUCCESS",
                    labels=[SELF_REVIEW_LABEL], auto_merge_enabled=False,
                    head_sha="abc")
        base.update(over)
        return PrFacts(**base)

    def test_all_green_clean_labeled_merges(self):
        self.assertEqual(evaluate(self._facts()).action, MERGE)

    def test_fence_failure_skips_fenced(self):
        self.assertEqual(evaluate(self._facts(fence_state="FAILURE")).action, SKIP_FENCED)

    def test_required_check_pending_waits(self):
        checks = _green_checks()
        checks["Mandatory version-bump"] = "PENDING"
        self.assertEqual(evaluate(self._facts(checks=checks)).action, WAIT)

    def test_missing_self_review_label_waits(self):
        self.assertEqual(evaluate(self._facts(labels=[])).action, WAIT)

    def test_auto_merge_already_enabled_skips(self):
        self.assertEqual(evaluate(self._facts(auto_merge_enabled=True)).action, SKIP_ALREADY)

    # robustness beyond the required matrix:
    def test_fence_pending_waits_never_merges(self):
        # fence not yet clean (PENDING, not FAILURE) must NOT auto-merge.
        self.assertEqual(evaluate(self._facts(fence_state="PENDING")).action, WAIT)

    def test_already_enabled_takes_precedence_over_red_fence(self):
        # idempotency check comes first — never re-touch an armed PR.
        d = evaluate(self._facts(auto_merge_enabled=True, fence_state="FAILURE"))
        self.assertEqual(d.action, SKIP_ALREADY)


# ===========================================================================
# Phase B B2 — Part 2 / STEP A: gather_facts reads gh output robustly
# ===========================================================================

class GatherFacts(unittest.TestCase):
    def test_checks_parsed_when_gh_checks_exits_nonzero(self):
        # `gh pr checks` exits non-zero when any check is pending/failing but
        # STILL emits the JSON. gather_facts must read it — otherwise a red
        # fence reads as "no facts" → WAIT instead of SKIP_FENCED.
        rows = [{"name": FENCE_JOB, "state": "FAILURE"}] + \
               [{"name": c, "state": "SUCCESS"} for c in REQUIRED_CHECKS]
        checks_json = json.dumps(rows)
        view_json = json.dumps({"labels": [{"name": SELF_REVIEW_LABEL}],
                                "autoMergeRequest": None, "headRefOid": "abc"})

        class _W(PrWatcher):
            def _run(self, argv):
                if "checks" in argv:
                    return subprocess.CompletedProcess(argv, 8, checks_json, "")  # nonzero
                return subprocess.CompletedProcess(argv, 0, view_json, "")

        w = _W(repo_root=Path("."), runner_id="test", log=lambda *a, **k: None)
        facts = w.gather_facts(58)
        self.assertEqual(facts.fence_state, "FAILURE")
        self.assertEqual(evaluate(facts).action, SKIP_FENCED)   # not WAIT

    def test_duplicate_check_name_is_green_only_if_all_green(self):
        rows = [{"name": "X", "state": "SUCCESS"}, {"name": "X", "state": "FAILURE"}]
        view_json = json.dumps({"labels": [], "autoMergeRequest": None, "headRefOid": ""})

        class _W(PrWatcher):
            def _run(self, argv):
                if "checks" in argv:
                    return subprocess.CompletedProcess(argv, 1, json.dumps(rows), "")
                return subprocess.CompletedProcess(argv, 0, view_json, "")

        w = _W(repo_root=Path("."), runner_id="test", log=lambda *a, **k: None)
        self.assertEqual(w.gather_facts(1).checks["X"], "FAILURE")


# ===========================================================================
# Phase B B2 — Part 2 / STEP C: process_pr builds the locked MergeContext
# ===========================================================================

class ProcessPr(unittest.TestCase):
    def _watcher(self, facts):
        captured = {}

        class _Spy:
            def run(self, ctx):
                captured["ctx"] = ctx
                return EXIT_OK

        class _W(PrWatcher):
            def gather_facts(self, pr):
                return facts

            def make_merge_step(self):
                return _Spy()

            def announce_fenced(self, pr):
                captured["fenced"] = pr

        return _W(repo_root=Path("."), runner_id="test", log=lambda *a, **k: None), captured

    def test_merge_runs_step_with_locked_context(self):
        facts = PrFacts(pr=58, checks=_green_checks(), fence_state="SUCCESS",
                        labels=[SELF_REVIEW_LABEL], head_sha="deadbeef")
        w, cap = self._watcher(facts)
        self.assertEqual(w.process_pr(58).action, MERGE)
        ctx = cap["ctx"]
        self.assertEqual(ctx.pr, 58)
        self.assertEqual(ctx.sha, "deadbeef")
        self.assertEqual(ctx.scope_fence, "clean")        # locked — only reach MERGE when clean
        self.assertEqual(ctx.eval_delta, "deploy-time")   # locked — eval is a deploy gate (#6)

    def test_fenced_announces_and_does_not_merge(self):
        facts = PrFacts(pr=7, checks=_green_checks(), fence_state="FAILURE",
                        labels=[SELF_REVIEW_LABEL])
        w, cap = self._watcher(facts)
        self.assertEqual(w.process_pr(7).action, SKIP_FENCED)
        self.assertEqual(cap.get("fenced"), 7)
        self.assertNotIn("ctx", cap)                      # merge step NOT run

    def test_wait_does_nothing(self):
        facts = PrFacts(pr=9, checks=_green_checks(), fence_state="SUCCESS", labels=[])
        w, cap = self._watcher(facts)
        self.assertEqual(w.process_pr(9).action, WAIT)
        self.assertNotIn("ctx", cap)


# ===========================================================================
# Phase B B2 — Part 2 / STEP D: deploy trigger idempotency + P7-flaky backoff
# ===========================================================================

class DecideDeploy(unittest.TestCase):
    def test_new_sha_deploys(self):
        self.assertEqual(decide_deploy("X", "", "").action, DEPLOY)

    def test_already_deployed_skips(self):
        self.assertEqual(decide_deploy("X", "X", "").action, DEPLOY_SKIP_CURRENT)

    def test_failed_sha_backs_off(self):
        self.assertEqual(decide_deploy("X", "", "X").action, DEPLOY_SKIP_BACKOFF)

    def test_new_sha_after_failure_redeploys(self):
        self.assertEqual(decide_deploy("Y", "", "X").action, DEPLOY)


class _FakeDeployWatcher(PrWatcher):
    """PrWatcher with the SHA state in memory and the deploy-runner invocation
    counted — so STEP D is exercised with no git / no subprocess / no files."""

    def __init__(self, *, remote, deploy_rc):
        super().__init__(repo_root=Path("."), runner_id="test",
                         clock=lambda: "T", log=lambda *a, **k: None)
        self._remote = remote
        self._deploy_rc = deploy_rc
        self._last = ""
        self._failed = ""
        self.deploy_runs = 0
        self.flags = []

    def resolve_remote_main_sha(self):
        return self._remote

    def run_deploy_runner(self):
        self.deploy_runs += 1
        return self._deploy_rc

    def read_last_deployed_sha(self):
        return self._last

    def write_last_deployed_sha(self, sha):
        self._last = sha

    def read_failed_sha(self):
        return self._failed

    def write_failed_sha(self, sha):
        self._failed = sha

    def clear_failed_sha(self):
        self._failed = ""

    def flag_stan(self, event, *, sha="", rc=None, detail=""):
        self.flags.append((event, sha, rc))
        return event


class MaybeDeploy(unittest.TestCase):
    def test_failed_deploy_backs_off_on_same_sha(self):
        # The required test: two polls with the SAME main-sha after a
        # DEPLOY_FAILED → the second does NOT run the deploy runner.
        w = _FakeDeployWatcher(remote="X", deploy_rc=deploy_runner.EXIT_DEPLOY_FAILED)
        d1 = w.maybe_deploy()                                  # poll 1 → deploy, fails
        self.assertEqual(d1.action, DEPLOY)
        self.assertEqual(w.deploy_runs, 1)
        self.assertEqual(w._failed, "X")                      # SHA blacklisted
        self.assertEqual([f[0] for f in w.flags], ["DEPLOY_BACKOFF"])  # flagged once
        d2 = w.maybe_deploy()                                 # poll 2 → same sha
        self.assertEqual(d2.action, DEPLOY_SKIP_BACKOFF)
        self.assertEqual(w.deploy_runs, 1)                    # runner NOT re-invoked

    def test_ok_deploy_records_and_skips_next_poll(self):
        w = _FakeDeployWatcher(remote="X", deploy_rc=deploy_runner.EXIT_OK)
        w.maybe_deploy()
        self.assertEqual(w.deploy_runs, 1)
        self.assertEqual(w._last, "X")
        self.assertEqual(w._failed, "")
        d2 = w.maybe_deploy()                                 # idempotent
        self.assertEqual(d2.action, DEPLOY_SKIP_CURRENT)
        self.assertEqual(w.deploy_runs, 1)

    def test_killswitch_rc_does_not_blacklist(self):
        # A kill-switch refusal (24) is not a flaky-gate failure → do NOT
        # blacklist; once the freeze lifts the same SHA deploys.
        w = _FakeDeployWatcher(remote="X", deploy_rc=deploy_runner.EXIT_KILLSWITCH)
        w.maybe_deploy()
        self.assertEqual(w.deploy_runs, 1)
        self.assertEqual(w._failed, "")                       # not blacklisted
        self.assertEqual(w.flags, [])
        d2 = w.maybe_deploy()
        self.assertEqual(d2.action, DEPLOY)
        self.assertEqual(w.deploy_runs, 2)


if __name__ == "__main__":
    unittest.main()
