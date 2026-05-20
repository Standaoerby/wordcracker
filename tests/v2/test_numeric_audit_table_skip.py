"""Sprint 20+ B13 — numeric audit should skip markdown table cells.

Round 11 finding: when the renderer formats results into a markdown
table, numbers in cells (counts, share %, row indices) trigger false-
positive audit mismatches at high rate. Specifically:
  - share % columns derived by the LLM (not surfaced verbatim by tools)
  - per-row counts that match list lengths or aggregates the audit
    doesn't recognize

Fix: audit_numbers strips lines that look like `| cell | cell |` before
extracting numbers. Numbers in PROSE outside the table still get
audited (the actual use case the audit protects against — fabricated
totals in narrative like «всего около 200 раз»).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.numeric_audit import (
    AuditReport,
    _strip_table_content,
    audit_numbers,
)


class StripTableContent(unittest.TestCase):

    def test_strips_pipe_table_rows(self):
        text = (
            "Topp слов автора:\n"
            "| word | count | share |\n"
            "|------|-------|-------|\n"
            "| burger | 47 | 12.3% |\n"
            "| stitching | 32 | 8.5% |\n"
            "\nЭти слова характерны для Doyle."
        )
        stripped = _strip_table_content(text)
        self.assertNotIn("47", stripped)
        self.assertNotIn("12.3", stripped)
        self.assertNotIn("32", stripped)
        # Prose still present
        self.assertIn("характерны для Doyle", stripped)

    def test_no_table_returns_input(self):
        text = "Это просто prose без таблиц с числами 47 и 32."
        self.assertEqual(_strip_table_content(text), text)

    def test_handles_minimal_table(self):
        text = "| a | b |\n| 1 | 2 |"
        stripped = _strip_table_content(text)
        # Both rows look like pipe-table → stripped to empty/whitespace
        self.assertNotIn("1", stripped)
        self.assertNotIn("2", stripped)


class AuditSkipsTableCellNumbers(unittest.TestCase):

    def test_table_cell_47_not_flagged_even_when_absent_in_data(self):
        """Renderer table renders 47 in a cell. Tool data didn't surface
        47 directly (it's a derived count). Audit should NOT flag it."""
        answer = (
            "Любимые слова Doyle:\n\n"
            "| # | word | count |\n"
            "|---|------|-------|\n"
            "| 1 | burger | 47 |\n"
            "| 2 | stitching | 32 |\n"
        )
        records = [
            {"tool": "top_ngrams_by_author",
             "data": {"top": [{"word": "burger"}, {"word": "stitching"}]},
             "coverage": {"books_matched": 30, "books_total": 30}},
        ]
        report = audit_numbers(answer, records, intent="author_top_words")
        # 47 and 32 are inside the table → not extracted → not flagged
        for m in report.mismatches:
            self.assertNotEqual(m.value, 47.0,
                                msg=f"47 flagged: {m.context}")
            self.assertNotEqual(m.value, 32.0,
                                msg=f"32 flagged: {m.context}")

    def test_prose_numbers_still_audited(self):
        """Even with a table present, prose claims get checked."""
        answer = (
            "У автора около 5000 упоминаний этого слова.\n\n"
            "| word | count |\n"
            "|------|-------|\n"
            "| burger | 47 |\n"
        )
        records = [
            {"tool": "top_ngrams_by_author",
             "data": {"top": [{"word": "burger"}], "total": 50},
             "coverage": {"books_matched": 30, "books_total": 30}},
        ]
        report = audit_numbers(answer, records, intent="author_top_words")
        # 5000 is in prose and far from 50 → SHOULD be flagged
        flagged_values = [m.value for m in report.mismatches]
        self.assertIn(5000.0, flagged_values,
                      msg=f"5000 not flagged; mismatches={report.mismatches}")

    def test_share_percent_in_table_not_flagged(self):
        """Renderer-computed share % in a cell is derived — not surfaced
        by tool. Audit should not flag it."""
        answer = (
            "Распределение по эмоциям:\n\n"
            "| emotion | count | share |\n"
            "|---------|-------|-------|\n"
            "| joy | 142 | 28.4% |\n"
            "| fear | 89 | 17.8% |\n"
        )
        records = [
            {"tool": "book_emotion_profile",
             "data": {"counts": {"joy": 142, "fear": 89}},
             "coverage": {"books_matched": 1, "books_total": 1}},
        ]
        report = audit_numbers(answer, records, intent="book_emotion")
        flagged_values = [m.value for m in report.mismatches]
        # 28.4 and 17.8 are derived shares — not in tool data — but
        # they're inside the table so audit doesn't see them.
        self.assertNotIn(28.4, flagged_values)
        self.assertNotIn(17.8, flagged_values)


if __name__ == "__main__":
    unittest.main(verbosity=2)
