"""Sprint 19+ — book_readability sampled vs total word count.

Stan 2026-05-19: «уровень сложности Pride and Prejudice» вернул
`words=34427` которое renderer подал как «общее количество слов в
книге». Реально это count внутри первых 200k chars sample (для
скорости Flesch/FK). Реальная книга P&P — ~122k слов. Numeric audit
не отловил т.к. 34427 действительно есть в data.

Fix:
  - v2 wrapper читает counts file и добавляет `total_words_estimate`
  - также копирует `words` в `words_sampled_for_metric` для ясности
  - `_render_note` инструктирует renderer использовать
    total_words_estimate для «сколько слов / страниц в книге»
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TotalWordsAugmentation(unittest.TestCase):

    def _make_counts_file(self, total_target: int) -> Path:
        """Create a tempfile counts file with words summing to total_target."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix="_counts.txt",
            delete=False,
        )
        # 100 fake words; counts sum to target
        per_word = total_target // 100
        remainder = total_target - per_word * 99
        for i in range(99):
            tmp.write(f"word{i}\t{per_word}\n")
        tmp.write(f"word99\t{remainder}\n")
        tmp.close()
        return Path(tmp.name)

    def _v1_response(self, words_sampled: int = 34427):
        """Mimic v1 book_readability output (sampled count only)."""
        return {
            "id": "PG1342", "pg_id": "PG1342",
            "title": "Pride and Prejudice", "author": "Austen, Jane",
            "user_uploaded": False,
            "sampled_chars":      200_000,
            "sentences":          1654,
            "words":              words_sampled,
            "avg_sentence_length_words": 20.8,
            "avg_syllables_per_word":    1.41,
            "flesch_reading_ease": 58.8,
            "flesch_kincaid_grade": 10.9,
            "cefr_heuristic":     "B2",
        }

    def test_total_words_added_to_response(self):
        from scripts.v2.tools.books.readability import book_readability
        counts_file = self._make_counts_file(122_000)
        try:
            with mock.patch("scripts.rag_tools.book_readability",
                             return_value=self._v1_response()), \
                 mock.patch("scripts.rag_tools._counts_path",
                             return_value=counts_file):
                r = book_readability("PG1342")
            self.assertTrue(r.ok)
            self.assertEqual(r.data["total_words_estimate"], 122_000)
            # The original sampled count is preserved as words_sampled_for_metric
            self.assertEqual(r.data["words_sampled_for_metric"], 34427)
            # words field unchanged (legacy callers can still read it)
            self.assertEqual(r.data["words"], 34427)
        finally:
            counts_file.unlink(missing_ok=True)

    def test_render_note_explains_discrepancy(self):
        from scripts.v2.tools.books.readability import book_readability
        counts_file = self._make_counts_file(122_000)
        try:
            with mock.patch("scripts.rag_tools.book_readability",
                             return_value=self._v1_response()), \
                 mock.patch("scripts.rag_tools._counts_path",
                             return_value=counts_file):
                r = book_readability("PG1342")
            note = r.data.get("_render_note", "")
            # Should mention sampled count, total, and instruct usage
            self.assertIn("34427", note)
            self.assertIn("122,000", note)
            self.assertIn("total_words_estimate", note)
        finally:
            counts_file.unlink(missing_ok=True)

    def test_missing_counts_file_no_crash(self):
        """If counts file doesn't exist, return v1 response unchanged."""
        from scripts.v2.tools.books.readability import book_readability
        missing = Path("/nonexistent/dir/PGxxx_counts.txt")
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value=self._v1_response()), \
             mock.patch("scripts.rag_tools._counts_path",
                         return_value=missing):
            r = book_readability("PGxxx")
        self.assertTrue(r.ok)
        # No augmentation, but tool still ok
        self.assertNotIn("total_words_estimate", r.data)

    def test_v1_error_propagates(self):
        from scripts.v2.tools.books.readability import book_readability
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value={"error": "raw text not in /data/raw_text/"}):
            r = book_readability("PG999999")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
