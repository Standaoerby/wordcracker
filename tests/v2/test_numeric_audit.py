"""Sprint 16 Phase D — numeric audit tests.

Pure-function module, no I/O. Covers: number extraction (RU/EN/% / K
suffix / thousand separators), data harvesting (recursive walk +
list-length awareness), match tolerance, year-trust path, footer
formatting, intent skip, and the «doyle 47 vs 200 hallucination»
target scenario."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.numeric_audit import (
    AuditReport, NumericMismatch,
    _extract_numbers, _is_matched, _is_year_like, _parse_number,
    annotate_with_audit, audit_numbers, collect_data_numbers,
)


class NumberExtraction(unittest.TestCase):
    def test_plain_integer(self):
        nums = _extract_numbers("В корпусе 47 книг.")
        vals = [v for v, _, _ in nums]
        self.assertIn(47.0, vals)

    def test_decimal(self):
        nums = _extract_numbers("Cosine 0.78 близко.")
        vals = [v for v, _, _ in nums]
        self.assertIn(0.78, vals)

    def test_thousand_separator_comma(self):
        nums = _extract_numbers("Wodehouse написал 1,234 произведения.")
        vals = [v for v, _, _ in nums]
        self.assertIn(1234.0, vals)

    def test_thousand_separator_space(self):
        nums = _extract_numbers("В корпусе 55 000 книг.")
        vals = [v for v, _, _ in nums]
        self.assertIn(55000.0, vals)

    def test_K_suffix(self):
        nums = _extract_numbers("Около 55K книг.")
        vals = [v for v, _, _ in nums]
        self.assertIn(55_000.0, vals)

    def test_cyrillic_K_suffix(self):
        nums = _extract_numbers("Около 55К книг.")
        vals = [v for v, _, _ in nums]
        self.assertIn(55_000.0, vals)

    def test_thousand_word(self):
        nums = _extract_numbers("Holmes встречается 4 тыс раз.")
        vals = [v for v, _, _ in nums]
        self.assertIn(4_000.0, vals)

    def test_million_suffix(self):
        nums = _extract_numbers("3.5 млн токенов")
        vals = [v for v, _, _ in nums]
        self.assertIn(3_500_000.0, vals)

    def test_percent_stays_as_written(self):
        nums = _extract_numbers("Точность 78%.")
        vals = [v for v, _, _ in nums]
        # We keep the surface number; audit can match against 78 in data
        # if the renderer wrote "78" for a 0.78 prob, that's a phrasing
        # judgement call we don't second-guess here.
        self.assertIn(78.0, vals)

    def test_no_false_match_on_dotted_id(self):
        """`PG1342` and `v2.10` shouldn't yield bogus numbers."""
        nums = _extract_numbers("Релиз v2.10 содержит PG1342.")
        vals = [v for v, _, _ in nums]
        # v2.10 → not extracted (preceded by word char)
        # PG1342 → not extracted (preceded by word char)
        self.assertNotIn(1342.0, vals)


class DataHarvest(unittest.TestCase):
    def test_walks_nested_dict(self):
        rec = {"data": {"top": [{"count": 47}, {"count": 30}],
                        "total": 100}, "coverage": {"books_matched": 12}}
        nums = collect_data_numbers([rec])
        self.assertIn(47.0, nums)
        self.assertIn(30.0, nums)
        self.assertIn(100.0, nums)
        self.assertIn(12.0, nums)

    def test_extracts_numbers_from_strings(self):
        """A bio string «1881-1955» should yield both years for matching."""
        rec = {"data": {"author": "P. G. Wodehouse",
                        "bio": "Лет жизни: 1881-1955"}}
        nums = collect_data_numbers([rec])
        self.assertIn(1881.0, nums)
        self.assertIn(1955.0, nums)

    def test_includes_list_lengths(self):
        """«Top 10» phrasing in answer matches len(list)=10."""
        rec = {"data": {"top": list(range(10))}}
        nums = collect_data_numbers([rec])
        self.assertIn(10.0, nums)

    def test_handles_none_safely(self):
        nums = collect_data_numbers([{"data": None}, {}, {"data": "no nums"}])
        self.assertIsInstance(nums, set)

    def test_excludes_bools(self):
        rec = {"data": {"ok": True, "found": False, "count": 5}}
        nums = collect_data_numbers([rec])
        self.assertIn(5.0, nums)
        # bool 1.0 / 0.0 should NOT be in the set
        self.assertNotIn(1.0, nums - {5.0})


class MatchTolerance(unittest.TestCase):
    def test_exact_match(self):
        matched, near, _ = _is_matched(47.0, {47.0, 12.0})
        self.assertTrue(matched)
        self.assertEqual(near, 47.0)

    def test_within_tolerance(self):
        # 50 vs nearest 47 = 6% < 10%
        matched, near, _ = _is_matched(50.0, {47.0})
        self.assertTrue(matched)

    def test_outside_tolerance(self):
        matched, near, _ = _is_matched(200.0, {47.0, 30.0})
        self.assertFalse(matched)
        self.assertEqual(near, 47.0)


class YearLike(unittest.TestCase):
    def test_plain_year_trusted(self):
        self.assertTrue(_is_year_like(1881.0, "Wodehouse родился в 1881."))

    def test_year_with_books_context_not_trusted(self):
        self.assertFalse(_is_year_like(1881.0, "Wodehouse написал 1881 книг."))

    def test_modern_year_trusted(self):
        self.assertTrue(_is_year_like(2000.0, "в 2000 году"))

    def test_out_of_range_not_year(self):
        self.assertFalse(_is_year_like(1200.0, "в 1200 году"))


class AuditEndToEnd(unittest.TestCase):
    """The target scenario: catch the «Doyle 47 vs 200» hallucination."""

    def test_target_hallucination_caught(self):
        tool_records = [{
            "tool": "corpus_stats_by_author",
            "data": {"books_matched": 47, "total_tokens": 5_000_000},
            "coverage": {"books_matched": 47, "books_total": -1},
        }]
        answer = "У Doyle в корпусе около 200 книг и 5 млн токенов."
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        self.assertTrue(report.has_issues())
        mismatch_vals = {m.value for m in report.mismatches}
        self.assertIn(200.0, mismatch_vals)
        # The 5 млн = 5,000,000 token claim IS in data → not flagged
        self.assertNotIn(5_000_000.0, mismatch_vals)

    def test_clean_answer_no_issues(self):
        tool_records = [{
            "tool": "corpus_stats_by_author",
            "data": {"books_matched": 47, "total_tokens": 5_000_000},
            "coverage": {"books_matched": 47, "books_total": -1},
        }]
        answer = "У Doyle 47 книг, 5 млн токенов."
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        self.assertFalse(report.has_issues())

    def test_top_10_count_matches_list_length(self):
        """«top 10» should match len(top_list)=10."""
        tool_records = [{
            "tool": "top_authors_by",
            "data": {"top": [{"author": f"A{i}", "books": 30 - i}
                              for i in range(10)]},
        }]
        answer = "Топ 10 авторов: A1, A2 ... Самый плодовитый — 30 книг."
        report = audit_numbers(answer, tool_records, intent="top_authors_books")
        self.assertFalse(report.has_issues())

    def test_intent_skip_introduction(self):
        report = audit_numbers("Я Словоёб, у меня 32 инструмента",
                                [{"data": {"foo": 1}}],
                                intent="introduction")
        self.assertFalse(report.has_issues())
        self.assertIsNotNone(report.skipped_reason)

    def test_empty_tool_records_skips(self):
        report = audit_numbers("123 книг", [], intent="author_metadata")
        self.assertFalse(report.has_issues())

    def test_small_numbers_ignored_by_default(self):
        """min_value default=5 — answer with 1, 2, 3 only doesn't audit."""
        report = audit_numbers(
            "Этих авторов 3, у каждого по 2 книги.",
            [{"data": {"top": [{"a": "X", "b": 100}]}}],
            intent="author_metadata",
        )
        self.assertFalse(report.has_issues())

    def test_min_value_can_be_lowered(self):
        """Caller can tighten the floor."""
        report = audit_numbers(
            "У Doyle 200 книг.",
            [{"data": {"books_matched": 3}}],
            intent="author_metadata",
            min_value=1,
        )
        self.assertTrue(report.has_issues())

    def test_caps_at_3_mismatches(self):
        # 5 fabricated numbers; only 3 should be reported
        tool_records = [{"data": {"single_value": 42}}]
        answer = "Сразу пять: 100 книг, 200 слов, 300 раз, 400 строк, 500 случаев."
        report = audit_numbers(answer, tool_records, intent="author_metadata")
        self.assertEqual(len(report.mismatches), 3)


class Annotation(unittest.TestCase):
    def test_clean_answer_unchanged(self):
        report = AuditReport()
        self.assertEqual(annotate_with_audit("clean answer", report),
                          "clean answer")

    def test_mismatches_produce_footer(self):
        report = AuditReport(mismatches=[
            NumericMismatch(value=200.0, formatted="200",
                            context="около 200 книг сделал",
                            nearest_in_data=47.0,
                            nearest_distance_pct=325.5),
        ])
        out = annotate_with_audit("Doyle написал около 200 книг.", report)
        self.assertIn("Numeric audit", out)
        self.assertIn("`200`", out)
        # nearest_in_data should be surfaced
        self.assertIn("47", out)


class ParseNumber(unittest.TestCase):
    """Edge cases for the parser."""
    def test_integer_no_suffix(self):
        self.assertEqual(_parse_number("42", None), 42.0)

    def test_decimal_no_suffix(self):
        self.assertEqual(_parse_number("3.14", None), 3.14)

    def test_thousand_sep_comma(self):
        self.assertEqual(_parse_number("1,234", None), 1234.0)

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_number("abc", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
