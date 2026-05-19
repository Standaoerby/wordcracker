"""Sprint 20 — Stan 2026-05-19 critic flag:

    Renderer wrote: «Вот список из 50 слов / Количество упоминаний
                     в произведениях Дойла»
    Tool returned:  19 words (after PROPN / surname / corpus-diff filtering)
    Critic:         «представлен список из 50 слов является вымышленным,
                     так как в tool_results указано только 19 слов»

Class of bug: renderer claims user's requested top-N even though the
tool returned fewer after filtering. Critic catches but the answer
already misled the user before the footer.

Three-layer defence:
  1. Tool wrappers surface `top_requested` + `top_returned` + a
     mandatory `_render_note` when they differ.
  2. RENDER_PROMPT rule 14 forbids using the requested number; the
     renderer must say the actual returned count.
  3. numeric_audit pre-flags count-claim hallucinations BEFORE the
     answer reaches the user — even if the requested number happens
     to be valid elsewhere in the tool data.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401
from scripts.v2._types import Coverage, ToolResult


def _fake_v1_response(top_words: int = 19) -> dict:
    """Mimic v1 affinity_by_author output with N filtered top_words."""
    return {
        "author_regex": "^Doyle,",
        "slug": "doyle",
        "pos_filter": None,
        "effective_min_corpus_count": 500,
        "total_unique_words": 12000,
        "top_words": [
            {"word": f"word{i}", "author_count": 100 - i,
              "corpus_count": 5000, "affinity": 50.0 - i}
            for i in range(top_words)
        ],
        "cached": True,
        "proper_noun_filter": "corpus-diff dropped 31, spaCy PROPN dropped 0",
    }


class ToolWrapperSurfacesDelta(unittest.TestCase):

    def test_affinity_by_author_under_filled(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                          return_value=_fake_v1_response(top_words=19)):
            r = affinity_by_author("^Doyle,", top=50)
        self.assertTrue(r.ok)
        self.assertEqual(r.data["top_requested"], 50)
        self.assertEqual(r.data["top_returned"], 19)
        # _render_note must mention the actual count
        note = r.data.get("_render_note") or ""
        self.assertIn("19", note)
        self.assertIn("50", note)
        # ToolWarning surfaces too
        codes = [w.code for w in r.warnings]
        self.assertIn("under_filled", codes)

    def test_affinity_by_author_full_match_no_note(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                          return_value=_fake_v1_response(top_words=50)):
            r = affinity_by_author("^Doyle,", top=50)
        self.assertEqual(r.data["top_returned"], 50)
        # No mismatch → no count-honesty note (other notes may exist)
        note = r.data.get("_render_note") or ""
        self.assertNotIn("ACTUAL COUNT", note)
        codes = [w.code for w in r.warnings]
        self.assertNotIn("under_filled", codes)

    def test_affinity_by_book_under_filled(self):
        from scripts.v2.tools.books.affinity_book import affinity_by_book
        v1 = {
            "pg_id": "PG1342",
            "title": "Pride and Prejudice",
            "top_words": [{"word": f"w{i}", "book_count": 30,
                            "corpus_count": 10000, "affinity": 20.0}
                           for i in range(12)],
            "n_tokens": 70000,
        }
        with mock.patch("scripts.learning_tools.affinity_by_book",
                          return_value=v1):
            r = affinity_by_book("PG1342", top=30)
        self.assertTrue(r.ok)
        self.assertEqual(r.data["top_requested"], 30)
        self.assertEqual(r.data["top_returned"], 12)
        codes = [w.code for w in r.warnings]
        self.assertIn("under_filled", codes)


class NumericAuditCatchesClaimMismatch(unittest.TestCase):

    def test_renderer_claim_50_data_19_flagged(self):
        from scripts.v2.numeric_audit import audit_numbers
        answer = (
            "Вот список из 50 слов — самые характерные для Конан Дойла "
            "по affinity-метрике. Полные данные в таблице."
        )
        records = [{
            "tool": "affinity_by_author",
            "data": {
                "top_requested": 50,
                "top_returned": 19,
                "top_words": [{"word": f"w{i}", "affinity": 30.0}
                                for i in range(19)],
            },
        }]
        report = audit_numbers(answer, records, intent="author_vocab")
        self.assertTrue(report.has_issues())
        # The flagged claim should mention 50
        flagged = report.mismatches[0]
        self.assertEqual(flagged.value, 50.0)
        self.assertEqual(int(flagged.nearest_in_data), 19)

    def test_renderer_claim_19_no_flag(self):
        from scripts.v2.numeric_audit import audit_numbers
        answer = "Вот список из 19 слов — после фильтрации имён собственных."
        records = [{
            "tool": "affinity_by_author",
            "data": {
                "top_requested": 50,
                "top_returned": 19,
                "top_words": [{"word": f"w{i}", "affinity": 30.0}
                                for i in range(19)],
            },
        }]
        report = audit_numbers(answer, records, intent="author_vocab")
        self.assertFalse(report.has_issues())

    def test_no_delta_no_flag(self):
        from scripts.v2.numeric_audit import audit_numbers
        answer = "Вот список из 30 слов."
        records = [{
            "tool": "affinity_by_author",
            "data": {
                "top_requested": 30,
                "top_returned": 30,
                "top_words": [{"word": f"w{i}", "affinity": 30.0}
                                for i in range(30)],
            },
        }]
        report = audit_numbers(answer, records, intent="author_vocab")
        self.assertFalse(report.has_issues())

    def test_top_n_phrasing_caught(self):
        """`top 50 слов` should be flagged when data says 19 returned."""
        from scripts.v2.numeric_audit import audit_numbers
        answer = "Top 50 любимых слов Дойла:\n\n| word | affinity |\n..."
        records = [{
            "tool": "affinity_by_author",
            "data": {"top_requested": 50, "top_returned": 19,
                      "top_words": []},
        }]
        report = audit_numbers(answer, records, intent="author_vocab")
        self.assertTrue(report.has_issues())
        self.assertEqual(report.mismatches[0].value, 50.0)

    def test_skip_when_audit_off(self):
        from scripts.v2 import numeric_audit
        with mock.patch.object(numeric_audit, "AUDIT_ENABLED", False):
            report = numeric_audit.audit_numbers(
                "Вот список из 50 слов",
                [{"tool": "affinity_by_author",
                  "data": {"top_requested": 50, "top_returned": 19,
                            "top_words": []}}],
                intent="author_vocab",
            )
        self.assertFalse(report.has_issues())


class RenderPromptHasRule14(unittest.TestCase):
    """RENDER_PROMPT must include the new count-honesty rule so the
    LLM knows to never use the requested count when filter dropped
    the list."""

    def test_rule_14_present(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("top_requested", RENDER_PROMPT)
        self.assertIn("top_returned", RENDER_PROMPT)
        self.assertIn("14.", RENDER_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
