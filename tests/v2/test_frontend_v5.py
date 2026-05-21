"""Frontend P8 — version footer + composer cap tests.

Closes B-R14-17 (oversized request poisons history → next request 400's)
at the structural level: composer JS rejects ≥8 KB before save.

Closes R14 TL;DR concern «версия не подтверждена»: version + active v5
flags surface in the UI header at render time.
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.chat_server as cs


class VersionFooter(unittest.TestCase):
    def test_build_version_strings_includes_analytics_version(self):
        disp, tip = cs._build_version_strings()
        from scripts.v2.__version__ import ANALYTICS_VERSION as v
        self.assertIn(f"v{v}", disp)
        self.assertIn(f"v{v}", tip)

    def test_no_v5_flags_shows_legacy(self):
        for k in ("WC_V5_FOUNDATION", "WC_V5_RESOLVER", "WC_V5_RENDERER",
                  "WC_V5_PROSE", "WC_V5_PIPELINE"):
            os.environ.pop(k, None)
        disp, tip = cs._build_version_strings()
        self.assertIn("legacy", disp)
        self.assertIn("none", tip)

    def test_v5_flags_visible_when_on(self):
        with mock.patch.dict(os.environ, {
            "WC_V5_RENDERER": "on",
            "WC_V5_PIPELINE": "on",
        }, clear=False):
            disp, tip = cs._build_version_strings()
        self.assertIn("v5:", disp)
        self.assertIn("renderer", disp)
        self.assertIn("pipeline", disp)
        self.assertIn("renderer", tip)


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
