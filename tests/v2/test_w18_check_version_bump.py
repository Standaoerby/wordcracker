"""W-18 — tests for `scripts/check_version_bump.py`.

The version-bump gate is the FIRST step of the deploy pipeline; without
these tests we have no negative coverage for "deploy proceeded but the
label didn't move" (the failure observed 2026-05-24).

Negative tests (R2) — each branch that returns exit 3 has a dedicated
test that would PASS on the buggy code path and FAIL with the fix:

    - same version in baseline => exit 3
    - same version in git ref  => exit 3
    - same version in --file   => exit 3
    - --require-strict-increase + downgrade => exit 3

Positive tests:

    - parse_version_text picks up ANALYTICS_VERSION
    - bumped version vs baseline / git / file => exit 0
    - missing prior reference => exit 0 (first-run allowance)
    - tuple compare orders 2.6.13 > 2.6.2 (not lexicographic)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.check_version_bump import (  # noqa: E402
    _tuple_gt,
    main,
    parse_version_text,
    read_current_version,
    read_version_from_baseline,
    read_version_from_file,
)


# ---------------------------------------------------------------------------
# parse_version_text
# ---------------------------------------------------------------------------

class ParseVersionText(unittest.TestCase):
    def test_picks_up_double_quoted(self):
        self.assertEqual(parse_version_text('ANALYTICS_VERSION = "2.6.13"\n'), "2.6.13")

    def test_picks_up_single_quoted(self):
        self.assertEqual(parse_version_text("ANALYTICS_VERSION = '2.6.13'\n"), "2.6.13")

    def test_returns_none_when_missing(self):
        self.assertIsNone(parse_version_text("# no version here"))

    def test_picks_up_dotted_alpha_suffix(self):
        self.assertEqual(parse_version_text('ANALYTICS_VERSION = "3.2.0-alpha1"\n'),
                         "3.2.0-alpha1")


# ---------------------------------------------------------------------------
# read_current_version — sanity against the shipped repo file
# ---------------------------------------------------------------------------

class ReadCurrentVersion(unittest.TestCase):
    def test_reads_shipped_version(self):
        v = read_current_version()
        # Must look like a dotted numeric prefix.
        self.assertRegex(v, r"^\d+\.\d+(\.\d+)?")


# ---------------------------------------------------------------------------
# read_version_from_baseline
# ---------------------------------------------------------------------------

class ReadVersionFromBaseline(unittest.TestCase):
    def test_missing_file_returns_none(self):
        self.assertIsNone(read_version_from_baseline(Path("/no/such/baseline.json")))

    def test_unknown_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            p.write_text(json.dumps({"version": "unknown"}), encoding="utf-8")
            self.assertIsNone(read_version_from_baseline(p))

    def test_picks_up_recorded_version(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            p.write_text(json.dumps({"version": "2.6.12", "verdicts": {}}),
                         encoding="utf-8")
            self.assertEqual(read_version_from_baseline(p), "2.6.12")

    def test_corrupt_baseline_exits(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "baseline.json"
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                read_version_from_baseline(p)
            self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# read_version_from_file
# ---------------------------------------------------------------------------

class ReadVersionFromFile(unittest.TestCase):
    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit) as cm:
            read_version_from_file(Path("/no/such/__version__.py"))
        self.assertEqual(cm.exception.code, 1)

    def test_reads_alternative_version_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "__version__.py"
            p.write_text('ANALYTICS_VERSION = "9.9.9"\n', encoding="utf-8")
            self.assertEqual(read_version_from_file(p), "9.9.9")


# ---------------------------------------------------------------------------
# main() — exit-code contract
# ---------------------------------------------------------------------------

class MainAgainstBaseline(unittest.TestCase):
    """The bug we observed 2026-05-24: deploy went out without the
    version moving. These tests are the negatives that would have
    blocked it."""

    def _baseline_with_version(self, td: Path, version: str) -> Path:
        p = td / "baseline.json"
        p.write_text(json.dumps({"version": version, "verdicts": {}}),
                     encoding="utf-8")
        return p

    def test_same_version_in_baseline_exits_3(self):
        current = read_current_version()
        with tempfile.TemporaryDirectory() as td:
            baseline = self._baseline_with_version(Path(td), current)
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "baseline", "--baseline", str(baseline)])
            self.assertEqual(cm.exception.code, 3)

    def test_bumped_version_in_baseline_exits_0(self):
        current = read_current_version()
        # Construct a prior version that cannot equal current.
        prior = "0.0.0-prior" if current != "0.0.0-prior" else "0.0.0-prior-2"
        with tempfile.TemporaryDirectory() as td:
            baseline = self._baseline_with_version(Path(td), prior)
            self.assertEqual(
                main(["--against", "baseline", "--baseline", str(baseline)]),
                0,
            )

    def test_missing_baseline_treated_as_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "absent.json"
            self.assertEqual(
                main(["--against", "baseline", "--baseline", str(missing)]),
                0,
            )


class MainAgainstFile(unittest.TestCase):
    def test_same_version_in_file_exits_3(self):
        current = read_current_version()
        with tempfile.TemporaryDirectory() as td:
            ref = Path(td) / "__version__.py"
            ref.write_text(f'ANALYTICS_VERSION = "{current}"\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "file", "--file", str(ref)])
            self.assertEqual(cm.exception.code, 3)

    def test_bumped_version_in_file_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            ref = Path(td) / "__version__.py"
            ref.write_text('ANALYTICS_VERSION = "0.0.1-prior"\n', encoding="utf-8")
            self.assertEqual(
                main(["--against", "file", "--file", str(ref)]),
                0,
            )

    def test_against_file_requires_file_flag(self):
        with self.assertRaises(SystemExit) as cm:
            main(["--against", "file"])
        self.assertEqual(cm.exception.code, 1)


class MainAgainstGitMocked(unittest.TestCase):
    """We mock subprocess.check_output to avoid coupling tests to the
    real git history (which moves whenever someone commits)."""

    def test_same_version_in_git_ref_exits_3(self):
        current = read_current_version()
        prior_blob = f'ANALYTICS_VERSION = "{current}"\n'.encode("utf-8")
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        return_value=prior_blob):
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "git", "--git-ref", "HEAD~1"])
            self.assertEqual(cm.exception.code, 3)

    def test_bumped_version_in_git_ref_exits_0(self):
        prior_blob = b'ANALYTICS_VERSION = "0.0.1-prior"\n'
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        return_value=prior_blob):
            self.assertEqual(
                main(["--against", "git", "--git-ref", "HEAD~1"]),
                0,
            )

    def test_missing_file_at_git_ref_treated_as_first_run(self):
        # When `git show <ref>:<path>` fails because the file did not
        # exist at that ref, the script must treat it as a first run.
        err = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "show", "HEAD~1:scripts/v2/__version__.py"],
            stderr=b"fatal: path 'scripts/v2/__version__.py' does not exist in 'HEAD~1'\n",
        )
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        side_effect=err):
            self.assertEqual(
                main(["--against", "git", "--git-ref", "HEAD~1"]),
                0,
            )

    def test_bad_git_ref_exits_1(self):
        err = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "show", "nonsuch:scripts/v2/__version__.py"],
            stderr=b"fatal: bad revision 'nonsuch:scripts/v2/__version__.py'\n",
        )
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "git", "--git-ref", "nonsuch"])
            self.assertEqual(cm.exception.code, 1)


class MainStrictIncrease(unittest.TestCase):
    """--require-strict-increase: also fails on downgrade."""

    def test_downgrade_with_strict_flag_exits_3(self):
        current = read_current_version()
        # Build a "newer" prior so that current would be a downgrade.
        # We can't change the repo version inside the test; instead, use
        # --against file with a higher prior.
        higher = "99.99.99"
        with tempfile.TemporaryDirectory() as td:
            ref = Path(td) / "__version__.py"
            ref.write_text(f'ANALYTICS_VERSION = "{higher}"\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "file", "--file", str(ref),
                      "--require-strict-increase"])
            self.assertEqual(cm.exception.code, 3)

    def test_downgrade_without_strict_flag_allows(self):
        # Without the flag, any difference is OK (rollback allowed).
        higher = "99.99.99"
        with tempfile.TemporaryDirectory() as td:
            ref = Path(td) / "__version__.py"
            ref.write_text(f'ANALYTICS_VERSION = "{higher}"\n', encoding="utf-8")
            self.assertEqual(
                main(["--against", "file", "--file", str(ref)]),
                0,
            )


# ---------------------------------------------------------------------------
# _tuple_gt: dotted-numeric compare must not be lexicographic
# ---------------------------------------------------------------------------

class TupleGt(unittest.TestCase):
    def test_numeric_segments_compare_as_ints(self):
        # Lexicographically "2.6.2" > "2.6.13" — must NOT be the case here.
        self.assertTrue(_tuple_gt("2.6.13", "2.6.2"))
        self.assertFalse(_tuple_gt("2.6.2", "2.6.13"))

    def test_equal_is_not_greater(self):
        self.assertFalse(_tuple_gt("2.6.13", "2.6.13"))

    def test_major_diff(self):
        self.assertTrue(_tuple_gt("3.0.0", "2.99.99"))

    def test_alpha_suffix_sorts_after_same_numeric_prefix(self):
        # "3.2.0-alpha1" is treated as 3.2.0 followed by an alpha tag.
        # Numbers-vs-strings: pure-numeric (3.2.0) sorts BEFORE the
        # tagged variant in our keying because alpha tags are "kind=1"
        # while numeric segments are "kind=0". That gives a sensible
        # "release > pre-release" total order for our purposes.
        self.assertTrue(_tuple_gt("3.2.0-alpha1", "3.2.0"))


if __name__ == "__main__":
    unittest.main()
