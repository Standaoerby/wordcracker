"""Frontend P8 — version footer + composer cap tests.

Closes B-R14-17 (oversized request poisons history → next request 400's)
at the structural level: composer JS rejects ≥8 KB before save.

Closes R14 TL;DR concern «версия не подтверждена»: version surfaces in
the UI header at render time.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.chat_server as cs


class VersionFooter(unittest.TestCase):
    def test_build_version_strings_includes_analytics_version(self):
        disp, tip = cs._build_version_strings()
        from scripts.v2.__version__ import ANALYTICS_VERSION as v
        self.assertIn(f"v{v}", disp)
        self.assertIn(f"v{v}", tip)

    def test_pipeline_label_in_display(self):
        # Pipeline shape is documented in the version pill regardless of
        # which feature flags are set in env.
        disp, tip = cs._build_version_strings()
        self.assertIn("planner", disp)
        self.assertIn("renderer", disp)
        self.assertIn("planner", tip)


class PageRender(unittest.TestCase):
    """Smoke: PAGE has the placeholders + they render via replace()."""

    def test_page_has_version_placeholder(self):
        self.assertIn("__VERSION_DISPLAY__", cs.PAGE)
        self.assertIn("__VERSION_TOOLTIP__", cs.PAGE)

    def test_page_has_composer_cap_placeholder(self):
        self.assertIn("__COMPOSER_MAX_BYTES__", cs.PAGE)

    def test_placeholders_substituted_correctly(self):
        disp, tip = cs._build_version_strings()
        rendered = (cs.PAGE
                    .replace("__ASSISTANT_NAME__", "TestBot")
                    .replace("__VERSION_DISPLAY__", disp)
                    .replace("__VERSION_TOOLTIP__", tip)
                    .replace("__COMPOSER_MAX_BYTES__", str(cs.COMPOSER_MAX_BYTES)))
        self.assertNotIn("__VERSION_DISPLAY__", rendered)
        self.assertNotIn("__COMPOSER_MAX_BYTES__", rendered)
        self.assertIn("TestBot", rendered)
        self.assertIn(disp, rendered)
        self.assertIn(str(cs.COMPOSER_MAX_BYTES), rendered)


class ComposerCap(unittest.TestCase):
    def test_composer_max_bytes_set(self):
        self.assertEqual(cs.COMPOSER_MAX_BYTES, 8 * 1024)
        # Must be less than the server-side history clip so frontend
        # gets the friendly error before server returns 400.
        self.assertLess(cs.COMPOSER_MAX_BYTES, cs.MAX_PAYLOAD_BYTES)

    def test_composer_js_present(self):
        """Composer reject logic must be in the rendered page —
        otherwise B-R14-17 regression returns."""
        self.assertIn("COMPOSER_MAX_BYTES", cs.PAGE)
        self.assertIn("composerByteSize", cs.PAGE)
        # Friendly Russian error visible
        self.assertIn("Запрос слишком длинный", cs.PAGE)
        # Must reference /admin/ as fallback for large texts
        self.assertIn("/admin/", cs.PAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
