"""W-12 tests — symmetric `words_appearing_after` tool exists and the
plan routes rising-direction queries to it.

Stan 2026-05-23 (Phase 4) test bench:
    «слова, вышедшие из употребления» работает (words_disappearing_after),
    зеркальный «слова, ставшие чаще» → «нет такой функции».

W-12 acceptance:
    1. Парный инструмент words_appearing/rising_after существует.
    2. Запрос про растущие слова возвращает ранжированный список
       по росту частоты.

R5 compliance: positive (rise direction → appearing tool), negative
(drop direction stays on disappearing tool — no regression).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import build
from scripts.v2.planner.builders.word import _plan_word_timeline


# ---------------------------------------------------------------------------
# Tool registration — words_appearing_after exists at v1 + v2 layers
# ---------------------------------------------------------------------------


class WordsAppearingAfterToolExists(unittest.TestCase):

    def test_v1_function_importable(self):
        from scripts.rag_tools import words_appearing_after
        self.assertTrue(callable(words_appearing_after))

    def test_v1_in_tool_dispatch(self):
        from scripts.rag_tools import TOOL_DISPATCH
        self.assertIn("words_appearing_after", TOOL_DISPATCH)

    def test_v2_wrapper_importable(self):
        from scripts.v2.tools.words.timeline import words_appearing_after
        self.assertTrue(callable(words_appearing_after))

    def test_v2_in_tool_registry(self):
        # Tools register via the `@tool(...)` decorator on import. We
        # have to import the whole `scripts.v2.tools` package (which
        # walks every tool module) before peeking at REGISTRY.
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY
        self.assertIn("words_appearing_after", REGISTRY)

    def test_v1_schema_declared(self):
        from scripts.v2.contracts.schemas import V1WordsAppearingAfter
        # Mirror of disappearing schema — rise_ratio in row_keys
        self.assertIn("rise_ratio", V1WordsAppearingAfter.__row_keys__)


# ---------------------------------------------------------------------------
# Plan routing — rise vs drop direction picks the right tool
# ---------------------------------------------------------------------------


class WordTimelineRoutesByDirection(unittest.TestCase):

    def test_stavshie_chashche_routes_to_appearing(self):
        # Stan's W-12 critical case
        e = extract("слова, ставшие чаще после 1920")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools)
        self.assertNotIn("words_disappearing_after", tools)

    def test_poyavivshiesya_routes_to_appearing(self):
        e = extract("какие слова появились после 1900")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools)

    def test_emerging_routes_to_appearing(self):
        e = extract("emerging vocabulary after 1900")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools)

    def test_trending_routes_to_appearing(self):
        e = extract("trending words after 1920")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools)

    def test_rising_routes_to_appearing(self):
        e = extract("rising words after 1900")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_appearing_after", tools)

    def test_drop_direction_keeps_disappearing(self):
        # Negative — drop direction still uses the old tool, no regression
        e = extract("слова, вышедшие из употребления после 1920")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_disappearing_after", tools)
        self.assertNotIn("words_appearing_after", tools)

    def test_disappeared_english_keeps_disappearing(self):
        e = extract("words that disappeared after 1900")
        plan = build("word_timeline", e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("words_disappearing_after", tools)


# ---------------------------------------------------------------------------
# Intent classifier picks up rise-direction surface forms
# ---------------------------------------------------------------------------


class RiseDirectionIntentRoutes(unittest.TestCase):

    def test_stavshie_chashche_classifies_as_timeline(self):
        self.assertEqual(classify("слова, ставшие чаще после 1920").label,
                         "word_timeline")

    def test_kakie_slova_poyavilis(self):
        self.assertEqual(
            classify("какие слова появились после 1900").label,
            "word_timeline",
        )

    def test_emerging_vocabulary(self):
        self.assertEqual(classify("emerging vocabulary after 1900").label,
                         "word_timeline")

    def test_trending_words(self):
        self.assertEqual(classify("trending words after 1920").label,
                         "word_timeline")


# ---------------------------------------------------------------------------
# v2 wrapper integration — rise_ratio ranking + error path + view
# ---------------------------------------------------------------------------


class WordsAppearingAfterWrapperBehavior(unittest.TestCase):

    def test_rows_ranked_by_rise_ratio_descending(self):
        from scripts.v2.tools.words import timeline as mod
        fake_v1 = {
            "year_cutoff": 1900, "basis": "auto",
            "pre_bucket": {"books": 1000, "total_tokens": 5_000_000},
            "post_bucket": {"books": 500, "total_tokens": 2_000_000},
            "min_post_per_million": 50.0,
            "top": [
                {"word": "telephone", "pre_per_million": 0.1,
                 "post_per_million": 200.0, "rise_ratio": 2000.0,
                 "pre_count": 1, "post_count": 400},
                {"word": "automobile", "pre_per_million": 2.0,
                 "post_per_million": 150.0, "rise_ratio": 75.0,
                 "pre_count": 10, "post_count": 300},
            ],
            "_elapsed_s": 1.2,
        }
        with patch("scripts.rag_tools.words_appearing_after",
                   return_value=fake_v1):
            result = mod.words_appearing_after(year=1900, top=10)
        self.assertTrue(result.ok)
        rows = (result.data or {}).get("top") or []
        self.assertEqual(len(rows), 2)
        # First row has highest rise_ratio
        self.assertEqual(rows[0]["word"], "telephone")
        self.assertGreater(rows[0]["rise_ratio"], rows[1]["rise_ratio"])

    def test_error_path_returns_not_ok(self):
        from scripts.v2.tools.words import timeline as mod
        fake_err = {"error": "not enough books in one bucket",
                    "pre_books": 5, "post_books": 0}
        with patch("scripts.rag_tools.words_appearing_after",
                   return_value=fake_err):
            result = mod.words_appearing_after(year=1900)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.type, "not_found")

    def test_view_has_rise_factor_column(self):
        from scripts.v2.tools.words import timeline as mod
        fake_v1 = {
            "year_cutoff": 1900, "basis": "auto",
            "pre_bucket": {"books": 100, "total_tokens": 1_000_000},
            "post_bucket": {"books": 100, "total_tokens": 1_000_000},
            "min_post_per_million": 50.0,
            "top": [
                {"word": "radio", "pre_per_million": 0.5,
                 "post_per_million": 100.0, "rise_ratio": 200.0,
                 "pre_count": 1, "post_count": 100},
            ],
            "_elapsed_s": 0.5,
        }
        with patch("scripts.rag_tools.words_appearing_after",
                   return_value=fake_v1):
            result = mod.words_appearing_after(year=1900, top=5)
        self.assertIsNotNone(result.view, "W-12 wrapper must attach a view")
        payload = getattr(result.view, "payload", None) or {}
        cols = list(payload.get("columns") or [])
        self.assertIn("rise_factor", cols,
                      msg=f"view columns must include rise_factor; got {cols!r}")
        # Headline mentions «появивш» or «ставш» — rise framing
        headline = (getattr(result.view, "headline", None) or "").lower()
        self.assertTrue(
            "появивш" in headline or "ставш" in headline,
            msg=f"headline must reflect rise direction; got {headline!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
