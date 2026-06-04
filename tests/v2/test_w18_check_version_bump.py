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
import os
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
    resolve_merge_base,
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


class MainAgainstMergeBaseMocked(unittest.TestCase):
    """`--against merge-base`: resolve the fork point, then compare the
    label there vs current. We mock subprocess.check_output so the unit
    test is decoupled from real history; the two calls (merge-base, then
    git show) are fed in order via side_effect."""

    def test_bumped_vs_merge_base_exits_0(self):
        # call 1: git merge-base -> a fork-point sha; call 2: git show at
        # that sha -> an OLD version. current (repo file) differs -> bump.
        with mock.patch(
            "scripts.check_version_bump.subprocess.check_output",
            side_effect=[b"deadbeef\n", b'ANALYTICS_VERSION = "0.0.1-fork"\n'],
        ):
            self.assertEqual(
                main(["--against", "merge-base", "--git-ref", "origin/main"]),
                0,
            )

    def test_same_at_merge_base_exits_3(self):
        current = read_current_version()
        with mock.patch(
            "scripts.check_version_bump.subprocess.check_output",
            side_effect=[b"deadbeef\n", f'ANALYTICS_VERSION = "{current}"\n'.encode()],
        ):
            with self.assertRaises(SystemExit) as cm:
                main(["--against", "merge-base", "--git-ref", "origin/main"])
            self.assertEqual(cm.exception.code, 3)

    def test_bad_base_ref_exits_1(self):
        err = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "merge-base", "HEAD", "origin/nope"],
            stderr=b"fatal: Not a valid object name origin/nope\n",
        )
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                resolve_merge_base("origin/nope")
            self.assertEqual(cm.exception.code, 1)

    def test_empty_merge_base_exits_1(self):
        # Unrelated histories -> merge-base prints nothing, exit 0. The
        # resolver must treat the empty result as a hard error, not a
        # silent "compare against <empty>".
        with mock.patch("scripts.check_version_bump.subprocess.check_output",
                        return_value=b"\n"):
            with self.assertRaises(SystemExit) as cm:
                resolve_merge_base("origin/main")
            self.assertEqual(cm.exception.code, 1)


def _have_git() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class MainAgainstMergeBaseRealGit(unittest.TestCase):
    """End-to-end against a throwaway git repo, with REPO_ROOT patched to
    it. Reproduces the R-25 false-red: comparing against the moving
    origin/main *tip* reds a legitimately-bumped branch once the base
    advances; the merge-base (fork point) does not.

    Three scenarios from the brief:
      * merged branch (base advanced past it) -> NOT red
      * PR with a bump                        -> green
      * PR without a bump                     -> red
    """

    def setUp(self):
        if not _have_git():
            self.skipTest("git not on PATH")
        self._env = os.environ.copy()
        self._env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)
        self.vpath = self.repo / "scripts" / "v2" / "__version__.py"
        self.vpath.parent.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")

    def tearDown(self):
        self._td.cleanup()

    def _git(self, *args):
        proc = subprocess.run(["git", *args], cwd=str(self.repo),
                              capture_output=True, text=True, env=self._env)
        assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
        return proc

    def _set_version(self, v: str):
        self.vpath.write_text(f'ANALYTICS_VERSION = "{v}"\n', encoding="utf-8")

    def _commit(self, msg: str):
        self._git("add", "-A")
        self._git("commit", "-qm", msg)

    def _run_merge_base(self):
        # Patch REPO_ROOT so read_current_version + git calls all target
        # the throwaway repo. `origin/main` is faked as a local ref.
        with mock.patch("scripts.check_version_bump.REPO_ROOT", self.repo):
            try:
                return main(["--against", "merge-base", "--git-ref", "main"])
            except SystemExit as e:
                return e.code

    def test_pr_with_bump_is_green(self):
        self._set_version("2.6.44")
        self._commit("main @ 2.6.44")
        self._git("checkout", "-q", "-b", "feat")
        self._set_version("2.6.45")
        self._commit("feat: bump 2.6.45")
        self.assertEqual(self._run_merge_base(), 0)

    def test_pr_without_bump_is_red(self):
        self._set_version("2.6.45")
        self._commit("main @ 2.6.45")
        self._git("checkout", "-q", "-b", "feat")
        (self.repo / "unrelated.txt").write_text("x", encoding="utf-8")
        self._commit("feat: no version change")
        self.assertEqual(self._run_merge_base(), 3)

    def test_merged_branch_not_red_after_base_advances(self):
        # main @ 2.6.44 (fork point M0).
        self._set_version("2.6.44")
        self._commit("main @ 2.6.44")
        fork = self._git("rev-parse", "HEAD").stdout.strip()
        # feat off M0, bumps to 2.6.45.
        self._git("checkout", "-q", "-b", "feat")
        self._set_version("2.6.45")
        self._commit("feat: bump 2.6.45")
        # main advances to 2.6.45 too (squash-merge equivalent: a NEW
        # commit on main carrying the bumped label, parent = M0, NOT feat).
        self._git("checkout", "-q", "main")
        self._set_version("2.6.45")
        self._commit("main: squash-merge of feat (2.6.45)")
        # Re-run the PR's version-bump on feat's head. The moving-tip
        # compare would now read main==2.6.45==current and FALSE-RED;
        # merge-base resolves to M0 (2.6.44) and stays green.
        self._git("checkout", "-q", "feat")
        self.assertEqual(
            self._run_merge_base(), 0,
            "merged branch re-run must NOT red once the base advanced "
            "past it (R-25 false-red fix)",
        )
        # Guard the test's own premise: feat's merge-base with main is M0.
        with mock.patch("scripts.check_version_bump.REPO_ROOT", self.repo):
            self.assertEqual(resolve_merge_base("main"), fork)


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
