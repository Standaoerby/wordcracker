"""E17 (2026-05-22) — book-scope intent + numeric audit false-flag.

ROOT CAUSE 1 (intent routing):
«характерные прилагательные в "The Picture of Dorian Gray"» routed
to author_vocab. The intent classifier had a rule (line 781) matching
«характерн* (слов*|прилаг*|глагол*)» that fires for ANY query, and
book_vocab patterns (line 823+) all required the literal word «книге»
before the title. Quoted title without «книге» was an unrecognized
book-scope signal.

ROOT CAUSE 2 (numeric audit false-flag):
The empty_state's «Применённые фильтры: min_corpus_count=200» line
made the answer contain 200. But `collect_data_numbers` only walked
`rec["data"]` + `rec["coverage"]` — not `rec["query"]` nor view's
empty_state.filters_applied / provenance.requested. So 200 was
flagged as «not in tool data» despite being the literal filter the
wrapper passed to v1.

Fixes:
  A. intent.py: new book_vocab pattern matches «(характерн|фирменн|
     любим|аффинн|signature|favorite|distinctive)* (слов|прилаг|глаг|
     сущ|лексик|adjectives|nouns|verbs|vocabulary|words) в "QUOTED"».
  B. numeric_audit.collect_data_numbers walks `query` and
     `_view_filter_values` of each record.
  C. rag_v2 builds records with `query` + `_view_filter_values`
     (via _harvest_view_filter_values helper).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.intent import classify
from scripts.v2 import numeric_audit as audit_mod


# ----------------------------------------------------------------------
# Part A — intent routing
# ----------------------------------------------------------------------

class BookScopeIntent(unittest.TestCase):
    """E17 fix A — book_vocab intent must catch quoted titles WITHOUT
    requiring the literal word «книге»."""

    def test_characteristic_adjectives_in_quoted_title(self):
        """Stan prod query that triggered the bug."""
        m = classify('характерные прилагательные в "The Picture of Dorian Gray"')
        self.assertEqual(m.label, "book_vocab",
                          f"expected book_vocab, got {m.label}")

    def test_signature_words_in_quoted_title(self):
        m = classify('фирменные слова в "Crime and Punishment"')
        self.assertEqual(m.label, "book_vocab")

    def test_favourite_verbs_in_russian_guillemets(self):
        m = classify('любимые глаголы в «Война и мир»')
        self.assertEqual(m.label, "book_vocab")

    def test_signature_words_english_in_quoted_title(self):
        m = classify('signature words in "Bleak House"')
        self.assertEqual(m.label, "book_vocab")

    def test_distinctive_vocabulary_in_quoted_title(self):
        m = classify('distinctive vocabulary in "Moby Dick"')
        self.assertEqual(m.label, "book_vocab")

    def test_affinity_words_in_quoted_title(self):
        m = classify('аффинные слова в "Dorian Gray"')
        self.assertEqual(m.label, "book_vocab")

    def test_plain_vocabulary_in_quoted_title(self):
        """Without a discriminator word — still book_vocab via fallback."""
        m = classify('слова в "Heart of Darkness"')
        self.assertEqual(m.label, "book_vocab")

    def test_no_quoted_title_falls_to_author_vocab(self):
        """When no quoted title present, original author_vocab pattern
        should still match."""
        m = classify("характерные прилагательные Уайльда")
        self.assertEqual(m.label, "author_vocab")

    def test_book_vocab_wins_over_author_vocab_on_priority(self):
        """book_vocab priority=100 > author_vocab=85. When BOTH patterns
        match, book_vocab must win."""
        from scripts.v2.planner.intent import PRIORITY
        self.assertGreater(PRIORITY["book_vocab"], PRIORITY["author_vocab"])


# ----------------------------------------------------------------------
# Part B — numeric audit harvest
# ----------------------------------------------------------------------

class NumericAuditQueryHarvest(unittest.TestCase):
    """E17 fix B — collect_data_numbers must walk `query` to legitimize
    filter values that appear in the answer."""

    def test_query_field_harvested(self):
        rec = {
            "tool": "affinity_by_book",
            "ok": True,
            "data": {"top": []},
            "coverage": {"books_matched": 1, "books_total": 1},
            "query": {"min_corpus_count": 200, "top": 30,
                       "pg_id": "PG174", "exclude_proper_nouns": True},
        }
        numbers = audit_mod.collect_data_numbers([rec])
        # Both filter values should now be in the trusted set
        self.assertIn(200.0, numbers,
                       "query.min_corpus_count must be harvested")
        self.assertIn(30.0, numbers,
                       "query.top must be harvested")

    def test_view_filter_values_harvested(self):
        rec = {
            "tool": "affinity_by_book",
            "ok": True,
            "data": {"top": []},
            "coverage": {"books_matched": 1, "books_total": 1},
            "_view_filter_values": {
                "empty_filters_applied": {"min_corpus_count": 200,
                                            "pos_filter": ["ADJ"]},
                "provenance_requested": {"top": 30,
                                          "min_corpus_count": 200},
            },
        }
        numbers = audit_mod.collect_data_numbers([rec])
        self.assertIn(200.0, numbers)
        self.assertIn(30.0, numbers)

    def test_filter_values_no_longer_flag_as_hallucination(self):
        """End-to-end: empty_state answer with «min_corpus_count=200»
        must NOT trip the audit."""
        answer = (
            'Нет фирменных слов для The Picture of Dorian Gray '
            'при min_corpus_count=200.\n\n'
            '**Применённые фильтры:**\n\n'
            '- `min_corpus_count = 200`\n'
            '- `pos_filter = [\'ADJ\']`\n'
        )
        rec = {
            "tool": "affinity_by_book",
            "ok": True,
            "data": {"top": [], "top_words": [],
                     "min_corpus_count_used": 10},
            "coverage": {"books_matched": 1, "books_total": 1},
            "query": {"pg_id": "PG174", "top": 30,
                       "pos_filter": ["ADJ"], "min_corpus_count": 200,
                       "exclude_proper_nouns": True},
            "_view_filter_values": {
                "empty_filters_applied": {"min_corpus_count": 200,
                                            "pos_filter": ["ADJ"]},
            },
            "warnings": [],
        }
        report = audit_mod.audit_numbers(answer, [rec], intent="book_vocab")
        # 200 must not be flagged — it's the legitimate filter value
        flagged_values = [m.value for m in report.mismatches]
        self.assertNotIn(200.0, flagged_values,
                          "200 (min_corpus_count) was wrongly flagged; "
                          "harvest of query/_view_filter_values is broken")


class HarvestViewFilterValuesHelper(unittest.TestCase):
    """E17 fix C — _harvest_view_filter_values pulls the right fields."""

    def test_returns_empty_for_none_view(self):
        from scripts.v2.rag_v2 import _harvest_view_filter_values
        self.assertEqual(_harvest_view_filter_values(None), {})

    def test_extracts_empty_state_filters_applied(self):
        from scripts.v2 import view_builders as vb
        from scripts.v2.rag_v2 import _harvest_view_filter_values
        from scripts.v2.view_types import EmptyReason
        view = vb.build_top_n_table(
            rows=[], columns=["rank", "word", "affinity"],
            empty_reason=EmptyReason.FILTERED_OUT,
            empty_message_ru="нет",
            empty_message_en="empty",
            empty_filters_applied={"min_corpus_count": 200,
                                     "pos_filter": ["ADJ"]},
        )
        aux = _harvest_view_filter_values(view)
        self.assertIn("empty_filters_applied", aux)
        self.assertEqual(aux["empty_filters_applied"]["min_corpus_count"], 200)

    def test_extracts_provenance_requested(self):
        from scripts.v2 import view_builders as vb
        from scripts.v2.rag_v2 import _harvest_view_filter_values
        view = vb.build_top_n_table(
            rows=[{"rank": 1, "word": "x", "affinity": "0.5"}],
            columns=["rank", "word", "affinity"],
            provenance=vb.make_provenance(
                requested={"top": 30, "min_corpus_count": 200},
                returned={"count": 1},
                sources=["SPGC"],
            ),
        )
        aux = _harvest_view_filter_values(view)
        self.assertIn("provenance_requested", aux)
        self.assertEqual(aux["provenance_requested"]["top"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
