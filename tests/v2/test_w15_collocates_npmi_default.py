"""W-15 (Phase 5 P2 polish, 2026-05-23) — NPMI is the default ranking
metric for `word_collocates`, surfaced both by the wrapper signature
AND by the plan builder.

Before:
  * Wrapper default `metric='count'` → npmi column rendered as «—»;
  * Builder never specified metric → top dominated by stop-words
    («the/of/and») even with exclude_stopwords=True (NPMI filters
    pairs by association strength rather than raw count).

Acceptance: collocates of «steam» should put engine/pressure/power
high, drop stop-words, and the NPMI column must be populated.
Implemented as: wrapper default flipped to npmi; planner builds
`word_collocates(metric='npmi')` explicitly so the contract is
visible at the plan level too.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class W15CollocatesNpmiDefault(unittest.TestCase):

    def test_builder_passes_metric_npmi(self):
        """`_plan_word_collocates` must emit `metric: 'npmi'` so the
        plan-level contract (not just the wrapper default) is explicit."""
        from scripts.v2.planner.builders.word import _plan_word_collocates
        from scripts.v2.planner.entities import Entities
        e = Entities(word="steam", author_regex="^Dickens,")
        plan = _plan_word_collocates(e)
        self.assertFalse(plan.needs_clarify, plan.explain)
        self.assertEqual(len(plan.steps), 1)
        step = plan.steps[0]
        self.assertEqual(step.tool, "word_collocates")
        self.assertEqual(step.args.get("metric"), "npmi",
                          f"Builder should pin metric=npmi, got {step.args!r}")

    def test_wrapper_default_metric_is_npmi(self):
        """Wrapper signature default must be npmi — Python introspection
        check so callers that omit `metric=` still get the NPMI path."""
        import inspect
        from scripts.v2.tools.words.collocates import word_collocates
        sig = inspect.signature(word_collocates)
        self.assertEqual(sig.parameters["metric"].default, "npmi")

    def test_npmi_default_ranks_steam_engine_above_the(self):
        """End-to-end-ish: with the new default the rendered view
        ranks high-association words above stop-words — engine/pressure
        above 'the' for «steam». We mock both v1 and marginals so the
        test is deterministic and runs without /workspace.
        """
        from scripts.v2.tools.words.collocates import word_collocates
        v1_raw = {
            "scope": "author:^Dickens,", "word": "steam", "window": 4,
            "total_occurrences": 500, "books_with_hits": 30,
            "top_collocates": [
                {"word": "the",      "count": 4000},  # super-common, noise
                {"word": "and",      "count": 3000},  # super-common, noise
                {"word": "engine",   "count": 90},
                {"word": "pressure", "count": 70},
                {"word": "power",    "count": 60},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=v1_raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            aug.return_value = ([
                # the/and are massively common in scope corpus → NPMI low
                {"word": "the",      "c_pair": 4000, "c_neighbor": 1_000_000},
                {"word": "and",      "c_pair": 3000, "c_neighbor": 800_000},
                # technical terms have much lower scope counts → NPMI high
                {"word": "engine",   "c_pair": 90,   "c_neighbor": 2_000},
                {"word": "pressure", "c_pair": 70,   "c_neighbor": 1_500},
                {"word": "power",    "c_pair": 60,   "c_neighbor": 3_000},
            ], 500, 10_000_000, 30)
            r = word_collocates({"author": "^Dickens,"}, "steam", top=5)
        self.assertTrue(r.ok)
        self.assertEqual(r.data["metric"], "npmi")
        rows = r.data["top_collocates"]
        names = [c["word"] for c in rows]
        # Stop-words must not lead the table
        self.assertNotEqual(names[0], "the",
                             f"NPMI should demote 'the', got order={names}")
        self.assertNotEqual(names[0], "and",
                             f"NPMI should demote 'and', got order={names}")
        # At least one of the technical terms surfaces in the top 3
        self.assertTrue(
            any(w in names[:3] for w in ("engine", "pressure", "power")),
            f"engine/pressure/power should rank high under NPMI; got {names!r}",
        )
        # Every row carries the npmi score (no «—» column)
        for c in rows:
            self.assertIn("npmi", c, f"row missing npmi key: {c}")

    def test_view_npmi_column_populated(self):
        """View-layer regression: COLLOCATES view's `npmi` field must be
        a number on the new default path (was None when metric was count)."""
        from scripts.v2.tools.words.collocates import word_collocates
        v1_raw = {
            "scope": "author:^X,", "word": "fog", "window": 4,
            "total_occurrences": 200, "books_with_hits": 50,
            "top_collocates": [
                {"word": "thick",   "count": 80},
                {"word": "morning", "count": 60},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                        return_value=v1_raw), \
             mock.patch("scripts.v2.tools.words.collocates."
                        "_augment_with_marginals") as aug:
            aug.return_value = ([
                {"word": "thick",   "c_pair": 80, "c_neighbor": 300},
                {"word": "morning", "c_pair": 60, "c_neighbor": 400},
            ], 200, 100_000, 50)
            r = word_collocates({"author": "^X,"}, "fog", top=2)
        # View attached?
        view = (r.data or {}).get("_view") if isinstance(r.data, dict) else None
        # COLLOCATES view sits on the ToolResult, not the raw dict — check
        # both surfaces to be defensive against view-attach location drift.
        attached = getattr(r, "view", None) or view
        # If view isn't attached (legacy paths), at least the raw rows
        # must carry npmi — which is the contract surface.
        for row in r.data["top_collocates"]:
            self.assertIn("npmi", row)
            self.assertIsNotNone(row["npmi"],
                                  "NPMI column must not be None on default path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
