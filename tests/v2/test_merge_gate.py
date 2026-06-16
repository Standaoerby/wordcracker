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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy import control  # noqa: E402
from scripts.autonomy.merge_gate import (  # noqa: E402
    MergeStep,
    MergeContext,
    MergeResult,
    EXIT_OK,
    EXIT_KILLSWITCH,
    EXIT_MERGE_FAILED,
)


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


if __name__ == "__main__":
    unittest.main()
