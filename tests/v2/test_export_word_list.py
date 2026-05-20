"""Sprint 20+ B3 — export_word_list intent + plan + history wiring.

Stan Round 11 beginner-researcher test:
  - «выгрузи в anki»        → expected: format prior list as Anki TSV
  - «csv pls»               → expected: format as CSV with header
  - «дай в markdown»        → expected: pipe-table
  - «json plz»              → expected: JSON array

All used to fall to clarify before this sprint.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.history import (
    _is_export_followup,
    _looks_like_followup,
    infer_followup_intent,
    merge_with_history,
)
from scripts.v2.planner.plan import _plan_export_word_list


class ExportFormatDetection(unittest.TestCase):

    def test_anki(self):
        e = extract("выгрузи слова в anki")
        self.assertEqual(e.export_format, "anki")

    def test_csv(self):
        e = extract("дай csv")
        self.assertEqual(e.export_format, "csv")

    def test_json(self):
        e = extract("export to json")
        self.assertEqual(e.export_format, "json")

    def test_markdown(self):
        e = extract("save as markdown")
        self.assertEqual(e.export_format, "markdown")

    def test_md_ext(self):
        e = extract("дай в .md")
        self.assertEqual(e.export_format, "markdown")

    def test_tsv(self):
        e = extract("выгрузи в tsv")
        self.assertEqual(e.export_format, "tsv")

    def test_excel_aliased_to_csv(self):
        e = extract("выгрузи в excel")
        self.assertEqual(e.export_format, "csv")

    def test_obsidian_aliased_to_markdown(self):
        e = extract("дай в obsidian")
        self.assertEqual(e.export_format, "markdown")

    def test_no_format_returns_none(self):
        e = extract("посоветуй книгу")
        self.assertIsNone(e.export_format)


class IntentClassification(unittest.TestCase):

    def test_vyguruzi_anki(self):
        m = classify("выгрузи в anki")
        self.assertEqual(m.label, "export_word_list")

    def test_csv_pls(self):
        m = classify("csv pls")
        self.assertEqual(m.label, "export_word_list")

    def test_bare_anki(self):
        m = classify("anki")
        self.assertEqual(m.label, "export_word_list")

    def test_export_to_json(self):
        m = classify("export to json")
        self.assertEqual(m.label, "export_word_list")

    def test_save_as_markdown(self):
        m = classify("save as markdown")
        self.assertEqual(m.label, "export_word_list")

    def test_unrelated_does_not_match(self):
        # Don't accidentally hijack non-export queries that mention csv
        m = classify("посоветуй книгу")
        self.assertNotEqual(m.label, "export_word_list")


class HistoryFollowupDetection(unittest.TestCase):

    def test_looks_like_followup_csv(self):
        self.assertTrue(_looks_like_followup("выгрузи в csv"))

    def test_is_export_followup_anki(self):
        self.assertTrue(_is_export_followup("дай в anki"))

    def test_is_export_followup_bare(self):
        self.assertTrue(_is_export_followup("csv"))

    def test_not_export_followup_unrelated(self):
        self.assertFalse(_is_export_followup("посоветуй книгу"))

    def test_infer_followup_routes_to_export(self):
        history = [
            {"role": "user", "content": "топ-10 любимых слов Дойла"},
            {"role": "assistant",
             "content": "| word | freq |\n|------|------|\n| tuppence | 5 |"},
        ]
        result = infer_followup_intent("выгрузи в anki", history)
        self.assertEqual(result, "export_word_list")

    def test_infer_followup_export_without_prior_word_list(self):
        history = [
            {"role": "user", "content": "как тебя зовут"},
            {"role": "assistant", "content": "я Wordcracker"},
        ]
        result = infer_followup_intent("выгрузи в csv", history)
        self.assertEqual(result, "export_word_list")  # still routes; plan clarifies


class MergeWithHistoryExtraction(unittest.TestCase):

    def test_extracts_prior_words(self):
        history = [
            {"role": "user", "content": "топ слов Дойла"},
            {"role": "assistant",
             "content": ("| word | freq |\n|------|------|\n"
                         "| tuppence | 5 |\n| embroidery | 4 |\n"
                         "| stitching | 3 |")},
        ]
        merged = merge_with_history(extract("выгрузи в anki"), history,
                                     "выгрузи в anki")
        prior = merged.raw_misc.get("_prior_words")
        self.assertEqual(prior, ["tuppence", "embroidery", "stitching"])

    def test_no_prior_words_when_no_table(self):
        history = [
            {"role": "user", "content": "что такое корпус"},
            {"role": "assistant", "content": "корпус — это коллекция текстов"},
        ]
        merged = merge_with_history(extract("выгрузи в csv"), history,
                                     "выгрузи в csv")
        # No table → no prior_words extracted
        self.assertNotIn("_prior_words", merged.raw_misc or {})


class PlanBuilder(unittest.TestCase):

    def test_no_prior_words_surfaces_clarify(self):
        e = extract("выгрузи в anki")
        plan = _plan_export_word_list(e)
        self.assertTrue(plan.needs_clarify)
        self.assertIn("anki", plan.clarify_question)
        self.assertEqual(plan.steps, [])

    def test_with_prior_words_render_only(self):
        e = extract("выгрузи в anki")
        e.raw_misc = {**(e.raw_misc or {}),
                      "_prior_words": ["tuppence", "embroidery", "stitching"],
                      "_prior_words_total": 3}
        plan = _plan_export_word_list(e)
        self.assertFalse(plan.needs_clarify)
        # No tools — just render
        self.assertEqual(plan.steps, [])
        # render_notes carries words + format spec
        joined = " ".join(plan.render_notes)
        self.assertIn("tuppence", joined)
        self.assertIn("anki", joined.lower())
        self.assertIn("TSV", joined)

    def test_csv_format_spec(self):
        e = extract("выгрузи в csv")
        e.raw_misc = {**(e.raw_misc or {}),
                      "_prior_words": ["a", "b"],
                      "_prior_words_total": 2}
        plan = _plan_export_word_list(e)
        joined = " ".join(plan.render_notes)
        self.assertIn("CSV", joined)
        self.assertIn("header", joined.lower())

    def test_markdown_format_spec(self):
        e = extract("дай в markdown")
        e.raw_misc = {**(e.raw_misc or {}),
                      "_prior_words": ["a"],
                      "_prior_words_total": 1}
        plan = _plan_export_word_list(e)
        joined = " ".join(plan.render_notes)
        self.assertIn("Markdown", joined)
        self.assertIn("pipe-table", joined)

    def test_json_format_spec(self):
        e = extract("export to json")
        e.raw_misc = {**(e.raw_misc or {}),
                      "_prior_words": ["a"],
                      "_prior_words_total": 1}
        plan = _plan_export_word_list(e)
        joined = " ".join(plan.render_notes)
        self.assertIn("JSON", joined)

    def test_caps_at_50_words(self):
        e = extract("выгрузи в csv")
        words = [f"word_{i}" for i in range(75)]
        e.raw_misc = {**(e.raw_misc or {}),
                      "_prior_words": words,
                      "_prior_words_total": 75}
        plan = _plan_export_word_list(e)
        # explain mentions cap
        self.assertIn("50", plan.explain)
        self.assertIn("75", plan.explain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
