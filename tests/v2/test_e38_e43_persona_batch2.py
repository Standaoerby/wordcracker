"""E38-E43 (2026-05-22) — second persona-batch from Stan's prod review.

Stan flagged 6 user-visible issues across 14 queries — leftover P0
contract bugs (same B-R14-7 class) and 2 UX leaks. All fixed in one
commit:

  E38 compare_authors view: «Cosine Similarity» / «Shared High Affinity»
      columns showed «—» for both authors. Case mismatch — entity stored
      «Cosine similarity» (lowercase 's'), template lookup was
      «Cosine Similarity» (title-case via metric_explanations.title()).
      Entity also missed «Shared High Affinity» key entirely.

  E39 corpus_overview: «Авторов: 0, Токенов: —». Wrapper read phantom
      keys spgc.get("n_authors") + spgc.get("n_tokens"). Actual
      corpus_meta.json schema (scripts/spgc_corpus_stats.py:87) has
      books_matched, total_tokens, vocab_size — NO n_authors.
      n_authors now computed lazily from _metadata_df().author.nunique().

  E40 word_collocates view: NPMI column = count column. Default
      metric="count" → metric_lc="count" → c.get(metric_lc) returned
      count value as npmi. View now leaves npmi=None when no real
      metric rerank ran.

  E41 book_archaic_words view: frequency column «—» for all rows.
      v1 returns row key `book_count` (learning_tools.py:768), wrapper
      read non-existent «count» / «frequency».

  E42 hybrid_search: scope_label «весь корпус (FTS5+semantic RRF)»
      leaked internal index implementation to user-facing headline.
      Changed to plain «во всём корпусе».

  E43 intent routing: «что у тебя с копирайтом» routed to corpus_meta
      (returned tool stats instead of policy answer). Now → out_of_scope
      so renderer can give PG-public-domain disclosure. «copyright
      coverage» count variant still routes correctly to corpus_meta.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class E38CompareAuthorsViewMetrics(unittest.TestCase):
    """E38 — compare_authors entity metrics keys match template lookup keys."""

    def test_cosine_and_shared_present_in_entity_metrics(self):
        from scripts.v2.tools.authors.affinity import compare_authors

        v1_nested = {
            "author1": {"regex": "^Poe,", "slug": "poe-edgar-allan",
                         "top_unique": [{"word": "raven", "affinity": 18.0}]},
            "author2": {"regex": "^Lovecraft,", "slug": "lovecraft-h-p",
                         "top_unique": [{"word": "cthulhu", "affinity": 22.0}]},
            "shared_high_affinity": [{"word": "horror", "affinity_1": 5.0,
                                       "affinity_2": 6.0}],
            "cosine_similarity": 0.012,
            "cosine_note": "low cosine — structural",
            "min_corpus_count": 200,
        }
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=v1_nested):
            r = compare_authors(author1_regex="^Poe,",
                                 author2_regex="^Lovecraft,")
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        entities = (view.payload or {}).get("entities") or []
        self.assertEqual(len(entities), 2)
        for ent in entities:
            metrics = ent.get("metrics") or {}
            # Title-case keys must match template lookup
            self.assertIn("Cosine Similarity", metrics,
                           f"entity {ent.get('name')} missing Cosine "
                           f"Similarity key (template renders «—»)")
            self.assertIn("Shared High Affinity", metrics)
            # Values must be populated
            self.assertEqual(metrics["Cosine Similarity"], 0.012)
            self.assertEqual(metrics["Shared High Affinity"], 1)


class E40WordCollocatesNpmiNotCount(unittest.TestCase):
    """E40 — NPMI column не равен count column when no metric rerank ran."""

    def test_npmi_none_when_metric_count(self):
        from scripts.v2.tools.words.collocates import word_collocates

        v1_raw = {
            "scope": {"author": "^Doyle,"},
            "word": "fog", "window": 4,
            "top_collocates": [
                {"word": "thick", "count": 635},
                {"word": "dense", "count": 523},
            ],
        }
        with mock.patch("scripts.rag_tools.word_collocates",
                         return_value=v1_raw):
            # Default metric="count"
            r = word_collocates(scope={"author": "^Doyle,"}, word="fog")
        self.assertTrue(r.ok)
        view = r.view
        cols = (view.payload or {}).get("collocates") or []
        self.assertGreaterEqual(len(cols), 2)
        # NPMI must NOT equal count (would mean wrapper put count in npmi)
        for c in cols:
            self.assertNotEqual(c["npmi"], c["count"],
                                 f"npmi ({c['npmi']}) == count ({c['count']}) — "
                                 f"persona bug: count leaked into npmi column")
            # When metric=count, npmi should be None
            self.assertIsNone(c["npmi"],
                               "no real metric rerank ran → npmi must be None")


class E41BookArchaicFrequency(unittest.TestCase):
    """E41 — book_archaic_words view reads v1's `book_count` key."""

    def test_frequency_populated_from_book_count(self):
        from scripts.v2.tools.books.readability import book_archaic_words

        v1_raw = {
            "id": "PG345",
            "checked_book_vocab": 5000,
            "top": [
                {"word": "amongst", "book_count": 142,
                 "source": "seed", "note": ""},
                {"word": "ye", "book_count": 89,
                 "source": "seed", "note": ""},
                {"word": "nay", "book_count": 56,
                 "source": "seed", "note": ""},
            ],
        }
        with mock.patch("scripts.learning_tools.book_archaic_words",
                         return_value=v1_raw):
            r = book_archaic_words(pg_id="PG345")
        self.assertTrue(r.ok)
        view = r.view
        rows = (view.payload or {}).get("rows") or []
        self.assertEqual(len(rows), 3)
        # Frequency must be the book_count value, not «—»
        self.assertEqual(rows[0]["frequency"], 142)
        self.assertEqual(rows[1]["frequency"], 89)


class E42ScopeLabelUserFacing(unittest.TestCase):
    """E42 — hybrid_search scope_label has no «FTS5+semantic RRF» leak."""

    def test_scope_label_clean(self):
        from scripts.v2._types import ToolResult, Coverage
        from scripts.v2.tools.search.hybrid import hybrid_search

        lex_matches = [
            {"pg_id": "PG1", "snippet": "test",
             "title": "T1", "author": "A1"},
        ]
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=1, books_total=-1),
            query={"query": "x"},
        )
        def _dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            return ToolResult.fail(tool="semantic_search",
                                   err_type="internal", message="off")
        with mock.patch("scripts.v2.tools.search.hybrid.dispatch",
                         side_effect=_dispatch):
            r = hybrid_search(query="x", k=12)
        scope_label = (r.view.payload or {}).get("scope_label") or ""
        self.assertNotIn("FTS5", scope_label,
                          "FTS5 is internal index name — must not leak")
        self.assertNotIn("RRF", scope_label,
                          "RRF is internal algo name — must not leak")
        self.assertIn("корпус", scope_label.lower(),
                       "scope still must convey «corpus» meaning")


class E43CopyrightPolicyIntent(unittest.TestCase):
    """E43 — copyright/license POLICY questions → OOS (not corpus_meta)."""

    def test_policy_phrasings_route_to_oos(self):
        from scripts.v2.planner.intent import classify
        policy_qs = [
            "что у тебя с копирайтом",
            "что у тебя с копирайтом?",
            "как с лицензией",
            "как у тебя с лицензией",
            "можно ли использовать книги",
            "можно ли копировать",
            "лицензия",
            "авторские права",
        ]
        for q in policy_qs:
            with self.subTest(q=q):
                self.assertEqual(classify(q).label, "out_of_scope",
                                  msg=q)

    def test_count_phrasings_still_corpus_meta(self):
        """«copyright coverage» / share / count — enumeration, not policy."""
        from scripts.v2.planner.intent import classify
        self.assertEqual(classify("copyright coverage в books").label,
                          "corpus_meta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
