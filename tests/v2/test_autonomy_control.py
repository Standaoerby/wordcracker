"""Characterization tests for the shared autonomy control surface
(scripts/autonomy/control.py — AUTONOMY_RUNBOOK_R-30 §7). WP-0 #5.

Pins the two halves of Stan's "видимость и контроль":

  - kill-switch (`read_killswitch`): env-truthy OR flag-file engages; falsey
    env values never engage; env is checked first (source label);
  - audit-log (`format_audit_line` / `append_audit`): a byte-exact line format
    with a two-space field separator (so the EVENT is always the 2nd token a
    reader splits out), `-` for empty fields, append-only writes;
  - the headline §7 invariant: ONE freeze blocks BOTH autonomous paths — the
    same flag file makes the deploy runner refuse to deploy AND the merge-step
    refuse to merge, each recording its refusal.

Pure stdlib, no env / no live endpoint: every input (repo_root, environ, clock)
is injected.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.autonomy import control  # noqa: E402


def _events(lines):
    """The EVENT token (2nd field) of each audit line — the reader contract."""
    return [ln.split("  ")[1] for ln in lines]


# ---------------------------------------------------------------------------
# Kill-switch read
# ---------------------------------------------------------------------------

class ReadKillswitch(unittest.TestCase):
    def test_clear_when_unset_and_no_flag(self):
        with tempfile.TemporaryDirectory() as td:
            ks = control.read_killswitch(td, environ={})
            self.assertFalse(ks.engaged)
            self.assertEqual(ks.source, "clear")

    def test_env_truthy_engages_source_env(self):
        with tempfile.TemporaryDirectory() as td:
            ks = control.read_killswitch(td, environ={"WC_AUTONOMY_KILLSWITCH": "1"})
            self.assertTrue(ks.engaged)
            self.assertEqual(ks.source, "env")

    def test_env_falsey_values_do_not_engage(self):
        with tempfile.TemporaryDirectory() as td:
            for v in ("", "0", "false", "no", "off", "  OFF  ", "False"):
                ks = control.read_killswitch(td, environ={"WC_AUTONOMY_KILLSWITCH": v})
                self.assertFalse(ks.engaged, f"{v!r} must NOT engage the freeze")

    def test_env_other_truthy_values_engage(self):
        with tempfile.TemporaryDirectory() as td:
            for v in ("1", "true", "yes", "on", "freeze"):
                ks = control.read_killswitch(td, environ={"WC_AUTONOMY_KILLSWITCH": v})
                self.assertTrue(ks.engaged, f"{v!r} must engage the freeze")

    def test_flag_file_engages_source_file(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "AUTONOMY_KILLSWITCH").write_text("freeze", encoding="utf-8")
            ks = control.read_killswitch(td, environ={})
            self.assertTrue(ks.engaged)
            self.assertEqual(ks.source, "file")

    def test_env_takes_precedence_over_flag_for_source(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "AUTONOMY_KILLSWITCH").write_text("x", encoding="utf-8")
            ks = control.read_killswitch(td, environ={"WC_AUTONOMY_KILLSWITCH": "1"})
            self.assertTrue(ks.engaged)
            self.assertEqual(ks.source, "env")


# ---------------------------------------------------------------------------
# Audit-line format — the stable, machine- + human-readable shape
# ---------------------------------------------------------------------------

class FormatAuditLine(unittest.TestCase):
    def test_golden_full_line(self):
        line = control.format_audit_line(
            "2026-06-16T00:00:00Z", "MERGE_OK",
            [("pr", 58), ("sha", "abc123"), ("runner", "merge_step")],
            "auto-merged")
        self.assertEqual(
            line,
            "2026-06-16T00:00:00Z  MERGE_OK  pr=58  sha=abc123  "
            "runner=merge_step  :: auto-merged")

    def test_head_only_when_no_fields_no_detail(self):
        self.assertEqual(control.format_audit_line("T", "E"), "T  E")

    def test_detail_appended_after_double_colon_sentinel(self):
        self.assertEqual(control.format_audit_line("T", "E", [], "why"), "T  E  :: why")

    def test_two_space_separator_keeps_event_as_second_token(self):
        # The reader splits on the two-space separator and reads index 1 as the
        # EVENT — this is the parse contract every consumer relies on.
        line = control.format_audit_line("2026-06-16T00:00:00Z", "DEPLOY_OK",
                                         [("target", "x"), ("runner", "r")])
        self.assertEqual(line.split("  ")[1], "DEPLOY_OK")


class NowUtc(unittest.TestCase):
    def test_shape_is_sortable_zulu_no_double_space(self):
        s = control.now_utc()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn("  ", s)  # must never collide with the field separator


class AppendAudit(unittest.TestCase):
    def test_appends_and_creates_parent(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / control.AUDIT_LOG_NAME
            control.append_audit(p, "line one")
            control.append_audit(p, "line two")
            self.assertEqual(p.read_text(encoding="utf-8"), "line one\nline two\n")


# ---------------------------------------------------------------------------
# Headline §7 invariant: ONE freeze blocks BOTH autonomous paths
# ---------------------------------------------------------------------------

class KillSwitchBlocksBothPaths(unittest.TestCase):
    """Runbook §7: Stan's single freeze must stop BOTH the deploy and the merge.
    The same flag file → the deploy runner refuses to deploy AND the merge-step
    refuses to merge, each writing its own refusal to the one audit-log."""

    def setUp(self):
        # Neutralise any ambient env freeze so the flag file is the sole trigger.
        self._saved = os.environ.pop("WC_AUTONOMY_KILLSWITCH", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["WC_AUTONOMY_KILLSWITCH"] = self._saved

    def test_flag_file_blocks_deploy_and_merge(self):
        from scripts.autonomy.deploy_runner import (
            DeployRunner, EXIT_KILLSWITCH as DEPLOY_KS)
        from scripts.autonomy.merge_gate import (
            MergeStep, MergeContext, EXIT_KILLSWITCH as MERGE_KS)

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / control.KILLSWITCH_FLAG_NAME).write_text("freeze", encoding="utf-8")
            audit = Path(td) / control.AUDIT_LOG_NAME
            quiet = dict(clock=lambda: "2026-06-16T00:00:00Z", log=lambda *a, **k: None)

            # Deploy path refuses BEFORE any git/docker effect (kill-switch is
            # the very first thing run() checks), so the real runner is safe here.
            dep = DeployRunner(repo_root=Path(td), audit_path=audit, **quiet)
            self.assertEqual(dep.run(), DEPLOY_KS)

            # Merge path refuses BEFORE do_merge (so the unwired seam never fires).
            mrg = MergeStep(repo_root=Path(td), audit_path=audit, **quiet)
            self.assertEqual(mrg.run(MergeContext(pr=58)), MERGE_KS)

            # Both refusals recorded, append-only, in order.
            lines = audit.read_text(encoding="utf-8").splitlines()
            self.assertEqual(_events(lines),
                             ["KILLSWITCH_REFUSE", "MERGE_KILLSWITCH_REFUSE"])


if __name__ == "__main__":
    unittest.main()
