"""Characterization tests for the scope-fence (AUTONOMY_RUNBOOK_R-30 §5).

The fence is the cornerstone of "max autonomy over everything REVERSIBLE":
without negative coverage we'd have no proof it actually blocks a 🔴 diff.
Required contract (WP-0): a diff touching a denylist path FAILS (exit 2); a
clean diff PASSES (exit 0). This file pins that, plus:

  - PATH layer: every denylist family denied; clean app paths allowed;
    `.env.example` rescued by ALLOW; the fence's own files self-protected.
  - CONTENT layer: force-push / rm-rf-data / DROP TABLE / chroma reset
    caught; benign `.reset()` NOT caught (precision); SCANEXEMPT + already-
    path-denied files skipped (no self-DoS on the denylist's own patterns).
  - CLI exit-code contract (0 / 2 / 1) via --changed-path / --diff-file.
  - End-to-end against a throwaway git repo using the SHIPPED denylist.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.scope_fence.check_scope_fence import (  # noqa: E402
    DEFAULT_DENYLIST,
    Denylist,
    added_lines_by_file,
    check,
    glob_to_regex,
    load_denylist,
    main,
    parse_denylist,
    path_denied,
    scan_content,
    scan_paths,
)

SHIPPED = str(DEFAULT_DENYLIST)


def _diff_for(path: str, *added_lines: str) -> str:
    """Build a minimal unified diff (as `git diff --unified=0` emits) that
    ADDS the given lines to `path`."""
    head = (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(added_lines)} @@\n")
    return head + "".join(f"+{ln}\n" for ln in added_lines)


# ---------------------------------------------------------------------------
# glob_to_regex
# ---------------------------------------------------------------------------

class GlobToRegex(unittest.TestCase):
    def test_double_star_crosses_slashes(self):
        rx = glob_to_regex(".github/**")
        self.assertTrue(rx.match(".github/workflows/predeploy.yml"))
        self.assertTrue(rx.match(".github/x"))
        self.assertFalse(rx.match("github/x"))

    def test_single_star_stays_in_segment(self):
        rx = glob_to_regex("docker-compose*.yml")
        self.assertTrue(rx.match("docker-compose.yml"))
        self.assertTrue(rx.match("docker-compose.dev.yml"))
        self.assertFalse(rx.match("docker-compose/sub.yml"))  # * is not /

    def test_dot_is_literal(self):
        rx = glob_to_regex(".env")
        self.assertTrue(rx.match(".env"))
        self.assertFalse(rx.match("xenv"))

    def test_env_dot_star(self):
        rx = glob_to_regex(".env.*")
        self.assertTrue(rx.match(".env.local"))
        self.assertTrue(rx.match(".env.example"))
        self.assertFalse(rx.match(".env"))  # needs a trailing .<something>

    def test_full_path_anchored(self):
        rx = glob_to_regex("scripts/deploy.sh")
        self.assertTrue(rx.match("scripts/deploy.sh"))
        self.assertFalse(rx.match("x/scripts/deploy.sh"))
        self.assertFalse(rx.match("scripts/deploy.sh.bak"))


# ---------------------------------------------------------------------------
# parse_denylist / the shipped file
# ---------------------------------------------------------------------------

class ParseDenylist(unittest.TestCase):
    def test_shipped_denylist_parses_clean(self):
        dl, errors = parse_denylist(Path(SHIPPED).read_text(encoding="utf-8"), SHIPPED)
        self.assertEqual(errors, [], f"shipped denylist must parse with 0 errors: {errors}")
        self.assertTrue(dl.deny, "shipped denylist must have DENY rules")
        self.assertTrue(dl.content, "shipped denylist must have CONTENT rules")

    def test_directive_without_value_errors(self):
        _, errors = parse_denylist("DENY\n")
        self.assertEqual(len(errors), 1)

    def test_unknown_directive_errors(self):
        _, errors = parse_denylist("NUKE everything\n")
        self.assertEqual(len(errors), 1)

    def test_bad_content_regex_errors(self):
        _, errors = parse_denylist("CONTENT (unclosed\n")
        self.assertEqual(len(errors), 1)

    def test_comments_and_blanks_ignored(self):
        dl, errors = parse_denylist("# a comment\n\n   \nDENY foo\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(dl.deny), 1)

    def test_load_denylist_missing_file_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            load_denylist(Path("/no/such/denylist.txt"))
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# path_denied — every denylist family + clean paths + ALLOW rescue
# ---------------------------------------------------------------------------

class PathDenied(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dl = load_denylist(Path(SHIPPED))

    def assert_denied(self, path):
        self.assertIsNotNone(path_denied(path, self.dl), f"{path} must be DENIED")

    def assert_allowed(self, path):
        self.assertIsNone(path_denied(path, self.dl), f"{path} must be ALLOWED")

    def test_ci_workflows_denied(self):
        self.assert_denied(".github/workflows/predeploy.yml")
        self.assert_denied(".github/workflows/scope-fence.yml")

    def test_deploy_config_denied(self):
        for p in ("Dockerfile", ".dockerignore", "docker-compose.yml",
                  "docker-compose.dev.yml", "infra/nginx_wordcracker.conf",
                  "infra/cloudflared_config.yml", "systemd/wordcracker-status.service"):
            self.assert_denied(p)

    def test_secrets_denied_but_example_allowed(self):
        self.assert_denied(".env")
        self.assert_denied(".env.production")
        self.assert_allowed(".env.example")   # ALLOW rescue

    def test_deploy_and_ops_scripts_denied(self):
        for p in ("scripts/deploy.sh", "scripts/verify_deployed_image.sh",
                  "scripts/predeploy_gate.sh", "scripts/ship.ps1",
                  "scripts/install_systemd_units.sh", "build_lockfile.sh"):
            self.assert_denied(p)

    def test_destructive_and_db_scripts_denied(self):
        for p in ("scripts/migrations/migrate_user_meta_lang.py",
                  "scripts/build_index.py", "scripts/build_index_raw.py",
                  "scripts/build_burrows_vectors.py", "scripts/consolidate_raw.sh"):
            self.assert_denied(p)

    def test_data_trees_denied(self):
        self.assert_denied("data/chroma/chroma.sqlite3")
        self.assert_denied("raw_books/x.txt")
        self.assert_denied("media/y.png")

    def test_autonomy_machinery_self_protected(self):
        self.assert_denied("scripts/scope_fence/denylist.txt")
        self.assert_denied("scripts/scope_fence/check_scope_fence.py")
        self.assert_denied("scripts/autonomy/deploy_runner.sh")  # reserved
        self.assert_denied("AUTONOMY_LOG.md")

    def test_clean_app_paths_allowed(self):
        for p in ("scripts/v2/planner/builders/book.py", "scripts/rag_tools.py",
                  "scripts/v2/__version__.py", "tests/v2/test_scope_fence.py",
                  "README.md", "docs/audit/S1-S5_findings.md", "web/src/App.tsx",
                  ".env.example", "requirements.in"):
            self.assert_allowed(p)


# ---------------------------------------------------------------------------
# scan_paths / scan_content / check
# ---------------------------------------------------------------------------

class ScanPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dl = load_denylist(Path(SHIPPED))

    def test_denylist_path_yields_violation(self):
        v = scan_paths([".github/workflows/x.yml"], self.dl)
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0].kind, "path")

    def test_clean_paths_yield_nothing(self):
        v = scan_paths(["scripts/v2/foo.py", "README.md", "tests/v2/test_x.py"], self.dl)
        self.assertEqual(v, [])

    def test_mixed_reports_only_the_denied(self):
        v = scan_paths(["scripts/v2/foo.py", "Dockerfile", "docs/x.md"], self.dl)
        self.assertEqual([x.subject for x in v], ["Dockerfile"])


class AddedLinesByFile(unittest.TestCase):
    def test_tracks_target_and_excludes_header(self):
        diff = _diff_for("scripts/v2/a.py", "x = 1", "y = 2")
        files = added_lines_by_file(diff)
        self.assertEqual(files, {"scripts/v2/a.py": ["x = 1", "y = 2"]})

    def test_dev_null_target_has_no_added_lines(self):
        diff = ("diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-gone\n")
        self.assertEqual(added_lines_by_file(diff), {})


class ScanContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dl = load_denylist(Path(SHIPPED))

    def _scan(self, path, *lines):
        return scan_content(_diff_for(path, *lines), self.dl)

    def test_force_push_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", 'os.system("git push --force origin main")'))

    def test_refspec_force_push_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", 'run("git push origin +main:main")'))

    def test_history_rewrite_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", 'sh("git reset --hard origin/main")'))

    def test_rm_rf_data_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", '    os.system("rm -rf data/")'))

    def test_drop_table_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", '    cur.execute("DROP TABLE users")'))

    def test_delete_collection_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", "    client.delete_collection(name)"))

    def test_chroma_reset_caught(self):
        self.assertTrue(self._scan("scripts/v2/a.py", "    chroma_client.reset()"))

    def test_benign_reset_not_caught(self):
        # Precision: a non-chroma .reset() is normal app code, must not trip.
        self.assertEqual(self._scan("scripts/v2/a.py", "    counter.reset()", "    parser.reset()"), [])

    def test_benign_app_code_not_caught(self):
        self.assertEqual(
            self._scan("scripts/v2/a.py", "def build(): return 1", "    return data['x']"), [])

    def test_scanexempt_file_skipped(self):
        # The fence's own test file holds destructive strings as fixtures.
        self.assertEqual(self._scan("tests/v2/test_scope_fence.py", 'x = "rm -rf data/"'), [])

    def test_path_denied_file_skipped_in_content(self):
        # deploy.sh legitimately contains destructive ops; it's already a
        # PATH violation, so content scan must not double-flag (or self-DoS).
        self.assertEqual(self._scan("scripts/deploy.sh", "rm -rf data/old"), [])


class CheckCombined(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dl = load_denylist(Path(SHIPPED))

    def test_path_violation_only(self):
        v = check(["Dockerfile"], "", self.dl)
        self.assertEqual([x.kind for x in v], ["path"])

    def test_content_violation_only(self):
        v = check(["scripts/v2/a.py"], _diff_for("scripts/v2/a.py", 'os.system("rm -rf data/")'), self.dl)
        self.assertEqual([x.kind for x in v], ["content"])

    def test_fully_clean(self):
        v = check(["scripts/v2/a.py"], _diff_for("scripts/v2/a.py", "x = 1"), self.dl)
        self.assertEqual(v, [])


# ---------------------------------------------------------------------------
# main() — CLI exit-code contract (no git: --changed-path / --diff-file)
# ---------------------------------------------------------------------------

class MainExitCodes(unittest.TestCase):
    def test_clean_changed_path_exits_0(self):
        self.assertEqual(main(["--denylist", SHIPPED, "--changed-path", "scripts/v2/x.py"]), 0)

    def test_denylist_changed_path_exits_2(self):
        self.assertEqual(
            main(["--denylist", SHIPPED, "--changed-path", ".github/workflows/x.yml"]), 2)

    def test_destructive_content_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            df = Path(td) / "d.diff"
            df.write_text(_diff_for("scripts/v2/a.py", 'os.system("rm -rf data/")'), encoding="utf-8")
            self.assertEqual(main(["--denylist", SHIPPED, "--diff-file", str(df)]), 2)

    def test_missing_denylist_exits_1(self):
        with self.assertRaises(SystemExit) as cm:
            main(["--denylist", "/no/such.txt", "--changed-path", "x"])
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# End-to-end against a throwaway git repo (the real merge-base diff plumbing),
# driven through the SHIPPED denylist.
# ---------------------------------------------------------------------------

def _have_git() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class MainRealGit(unittest.TestCase):
    def setUp(self):
        if not _have_git():
            self.skipTest("git not on PATH")
        self._env = os.environ.copy()
        self._env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)
        self._git("init", "-q", "-b", "main")
        self._write("README.md", "base\n")
        self._commit("base on main")
        self._git("checkout", "-q", "-b", "feat")

    def tearDown(self):
        self._td.cleanup()

    def _git(self, *args):
        p = subprocess.run(["git", *args], cwd=str(self.repo), capture_output=True,
                           text=True, env=self._env)
        assert p.returncode == 0, f"git {args} failed: {p.stderr}"
        return p

    def _write(self, rel, text):
        fp = self.repo / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text, encoding="utf-8")

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-qm", msg)

    def _run(self):
        try:
            return main(["--repo", str(self.repo), "--denylist", SHIPPED, "--base-ref", "main"])
        except SystemExit as e:
            return e.code

    def test_clean_pr_passes(self):
        self._write("scripts/v2/new_tool.py", "def f():\n    return 1\n")
        self._commit("feat: clean app change")
        self.assertEqual(self._run(), 0)

    def test_denylist_path_pr_trips(self):
        self._write(".github/workflows/evil.yml", "name: evil\n")
        self._commit("feat: touch CI")
        self.assertEqual(self._run(), 2)

    def test_content_marker_pr_trips(self):
        self._write("scripts/v2/evil.py", 'import os\nos.system("rm -rf data/")\n')
        self._commit("feat: smuggle destructive op")
        self.assertEqual(self._run(), 2)


if __name__ == "__main__":
    unittest.main()
