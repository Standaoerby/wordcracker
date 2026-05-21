"""Phase 4+5 pipeline wiring tests.

Validates that:
  - WC_V5_RENDERER=on switches _dispatch_render to render_v5
  - WC_V5_RENDERER=off (default) keeps legacy _llm_render path
  - WC_V5_PIPELINE=on creates a v5 envelope with trace + budget
  - WC_V5_PIPELINE=off (default) keeps envelope=None
  - Envelope extras populate log_request records without breaking
    existing shape
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import rag_v2 as r2
from scripts.v2._types import ToolResult


class V5EnvelopeFactory(unittest.TestCase):
    def test_returns_none_when_flag_off(self):
        with mock.patch.dict(os.environ, {"WC_V5_PIPELINE": "off"},
                              clear=False):
            env = r2._v5_pipeline_envelope("test query")
        self.assertIsNone(env)

    def test_creates_envelope_when_flag_on(self):
        with mock.patch.dict(os.environ, {"WC_V5_PIPELINE": "on"},
                              clear=False):
            env = r2._v5_pipeline_envelope("test query")
        self.assertIsNotNone(env)
        self.assertIn("trace", env)
        self.assertIn("budget", env)
        self.assertIn("t0", env)
        self.assertEqual(env["trace"].query_raw, "test query")
        # Budget has sensible defaults
        self.assertGreater(env["budget"].wall_clock_s, 0)


class V5EnvelopeExtras(unittest.TestCase):
    def test_none_envelope_returns_empty_extras(self):
        extras = r2._v5_envelope_extras(None)
        self.assertEqual(extras, {})

    def test_active_envelope_returns_v5_keys(self):
        with mock.patch.dict(os.environ, {"WC_V5_PIPELINE": "on"},
                              clear=False):
            env = r2._v5_pipeline_envelope("q")
        extras = r2._v5_envelope_extras(env, intent_label="author_vocab")
        self.assertIn("v5_trace_id", extras)
        self.assertEqual(extras["v5_intent"], "author_vocab")
        self.assertIn("v5_budget_max_s", extras)
        self.assertIn("v5_budget_used_s", extras)
        self.assertFalse(extras["v5_budget_exceeded"])    # just created

    def test_render_meta_propagates(self):
        with mock.patch.dict(os.environ, {"WC_V5_PIPELINE": "on"},
                              clear=False):
            env = r2._v5_pipeline_envelope("q")
        render_meta = {
            "view_type": "top_n_table",
            "prose_used": True,
            "prose_audit_failed": False,
            "phase_a_ms": 5,
            "phase_b_ms": 1500,
            "fallback_reason": None,
            "skeleton_chars": 400,
        }
        extras = r2._v5_envelope_extras(env, intent_label="author_vocab",
                                          render_meta=render_meta)
        self.assertEqual(extras["v5_render_view_type"], "top_n_table")
        self.assertTrue(extras["v5_render_prose_used"])
        self.assertEqual(extras["v5_render_phase_a_ms"], 5)
        self.assertEqual(extras["v5_render_phase_b_ms"], 1500)

    def test_fallback_reason_surfaces(self):
        with mock.patch.dict(os.environ, {"WC_V5_PIPELINE": "on"},
                              clear=False):
            env = r2._v5_pipeline_envelope("q")
        extras = r2._v5_envelope_extras(env, render_meta={
            "view_type": "error_friendly",
            "fallback_reason": "tool_broken",
            "prose_used": False,
            "prose_audit_failed": False,
            "phase_a_ms": 2, "phase_b_ms": 0,
            "skeleton_chars": 200,
        })
        self.assertEqual(extras["v5_render_fallback"], "tool_broken")


class DispatchRenderFlag(unittest.TestCase):
    """When WC_V5_RENDERER=on, _dispatch_render uses render_v5."""

    def test_flag_off_uses_legacy(self):
        """With flag off, _dispatch_render calls _llm_render."""
        with mock.patch.dict(os.environ, {"WC_V5_RENDERER": "off"},
                              clear=False):
            with mock.patch.object(r2, "_llm_render",
                                     return_value=("legacy answer", {})) as legacy:
                ans, meta = r2._dispatch_render(
                    "q", plan=None, results=[],
                    model="x", ollama_host="x", history=None,
                )
        self.assertEqual(ans, "legacy answer")
        legacy.assert_called_once()

    def test_flag_on_uses_v5(self):
        """With flag on, _dispatch_render calls render_v5."""
        fake_result = ("v5 answer", {"view_type": "top_n_table",
                                       "fallback_reason": None,
                                       "skeleton_chars": 100,
                                       "prose_used": False,
                                       "prose_audit_failed": False,
                                       "phase_a_ms": 1, "phase_b_ms": 0,
                                       "verification_failures": []})
        with mock.patch.dict(os.environ, {"WC_V5_RENDERER": "on"},
                              clear=False):
            # Patch render_v5 inside the imported module
            from scripts.v2 import render_v5 as r5
            with mock.patch.object(r5, "render_v5", return_value=fake_result):
                ans, meta = r2._dispatch_render(
                    "q", plan=None, results=[],
                    model="x", ollama_host="x", history=None,
                )
        self.assertEqual(ans, "v5 answer")
        self.assertEqual(meta["view_type"], "top_n_table")

    def test_v5_exception_falls_back_to_legacy(self):
        """If v5 path raises, _dispatch_render logs + falls back."""
        with mock.patch.dict(os.environ, {"WC_V5_RENDERER": "on"},
                              clear=False):
            from scripts.v2 import render_v5 as r5
            with mock.patch.object(r5, "render_v5",
                                     side_effect=RuntimeError("v5 broke")):
                with mock.patch.object(r2, "_llm_render",
                                         return_value=("legacy fallback", {})):
                    ans, meta = r2._dispatch_render(
                        "q", plan=None, results=[],
                        model="x", ollama_host="x", history=None,
                    )
        self.assertEqual(ans, "legacy fallback")


class BackwardCompat(unittest.TestCase):
    """Smoke: existing tests don't break when v5 wiring code is loaded."""

    def test_dispatch_render_exists(self):
        self.assertTrue(callable(r2._dispatch_render))

    def test_envelope_helpers_exist(self):
        self.assertTrue(callable(r2._v5_pipeline_envelope))
        self.assertTrue(callable(r2._v5_envelope_extras))


if __name__ == "__main__":
    unittest.main(verbosity=2)
