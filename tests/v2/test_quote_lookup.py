"""Sprint 19+ — quote-lookup mode for author_attribution.

Stan 2026-05-19: «угадай автора отрывка "the fog came pouring in..."»
fell to clarify «вставь сам текст (хотя бы 500 слов)». Two problems:
  1. Entity extractor didn't surface the quoted passage as
     attribution_text → plan saw nothing to attribute.
  2. The clarify message demanded 500+ words even for short citations,
     which is a Burrows Delta requirement — quote lookup against FTS5
     works on 5+ words.

Fix:
  - `_extract_attribution_passage` captures quoted runs >=30 chars and
    >=5 words from raw_text → entities.raw_misc.attribution_text.
  - `_plan_author_attribution` dual-path:
      <200 words → lexical_search FTS5 phrase match (quote lookup)
      ≥200 words → existing author_attribution Burrows Delta path
  - Substantive passage (≥5 words) takes priority over book_lookup
    redirect — short famous citations would otherwise be misrouted.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import plan as plan_mod


class AttributionPassageExtraction(unittest.TestCase):

    def test_stan_verbatim_query(self):
        q = ('угадай автора отрывка: "the fog came pouring in at every '
             'chink and keyhole, and was so dense without, that although '
             'the court was of the narrowest, the houses opposite were '
             'mere phantoms"')
        e = ent_mod.extract(q)
        passage = (e.raw_misc or {}).get("attribution_text")
        self.assertIsNotNone(passage)
        self.assertIn("the fog came pouring in", passage)
        self.assertGreater(len(passage.split()), 20)

    def test_short_famous_citation(self):
        q = ('кто автор этого отрывка: '
             '"It was the best of times, it was the worst of times"')
        e = ent_mod.extract(q)
        passage = (e.raw_misc or {}).get("attribution_text")
        self.assertIsNotNone(passage)
        self.assertIn("best of times", passage)

    def test_no_quotes_no_passage(self):
        e = ent_mod.extract("угадай автора отрывка")
        self.assertNotIn("attribution_text", e.raw_misc or {})

    def test_short_word_quote_not_passage(self):
        """«примеры использования слова "ajar"» — 1 word in quotes is a
        target word, not an attribution passage."""
        e = ent_mod.extract('примеры использования слова "ajar"')
        self.assertNotIn("attribution_text", e.raw_misc or {})

    def test_book_title_quote_not_attribution(self):
        """«найди книгу "Pride and Prejudice"» — quoted is a known book
        title, NOT an attribution passage. KNOWN_BOOKS guard skips it."""
        e = ent_mod.extract('найди книгу "Pride and Prejudice"')
        self.assertNotIn("attribution_text", e.raw_misc or {})


class QuoteLookupRouting(unittest.TestCase):

    def test_short_passage_routes_lexical_search(self):
        e = ent_mod.extract(
            'угадай автора отрывка: "the fog came pouring in at every '
            'chink and keyhole, and was so dense"')
        p = plan_mod.build("author_attribution", e)
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.steps[0].tool, "lexical_search")
        # FTS5 phrase query — wrapped in quotes
        query = p.steps[0].args.get("query", "")
        self.assertTrue(query.startswith('"') and query.endswith('"'),
                         msg=f"FTS5 query not phrase-quoted: {query!r}")

    def test_substantive_passage_beats_book_lookup_redirect(self):
        """«кто автор этого отрывка "..."» with quoted famous citation
        — book_title gets set BUT passage takes priority → lexical
        search runs, not find_book(title="...")."""
        e = ent_mod.extract(
            'кто автор этого отрывка: '
            '"It was the best of times, it was the worst of times"')
        p = plan_mod.build("author_attribution", e)
        self.assertEqual(p.steps[0].tool, "lexical_search")

    def test_bibliographic_routes_find_book(self):
        """«кто автор Дракулы» (no passage) → still book_lookup chain."""
        e = ent_mod.extract("кто автор Дракулы")
        p = plan_mod.build("author_attribution", e)
        self.assertEqual(p.steps[0].tool, "find_book")

    def test_no_passage_no_book_clarifies(self):
        e = ent_mod.extract("угадай автора отрывка")
        p = plan_mod.build("author_attribution", e)
        self.assertTrue(p.needs_clarify)
        # New clarify message mentions both 5+ words (lookup) and
        # 200+ (stylometry), NOT the old 500-only minimum
        msg = p.clarify_question or ""
        self.assertIn("5+", msg)
        self.assertIn("200+", msg)
        self.assertNotIn("500 слов", msg)

    def test_clarify_long_passage_uses_burrows(self):
        """Synthetic ≥200-word passage should route to Burrows."""
        long_passage = " ".join(["word"] * 250)
        from scripts.v2.planner.entities import Entities
        e = Entities(raw_misc={"attribution_text": long_passage,
                                "raw_text": f'"{long_passage}"'})
        p = plan_mod.build("author_attribution", e)
        self.assertEqual(p.steps[0].tool, "author_attribution")


class FTS5PhraseQuerySanity(unittest.TestCase):
    """FTS5 phrase queries have edge cases — trailing punctuation breaks
    the phrase mode. Verify we strip it."""

    def test_trailing_punctuation_stripped(self):
        from scripts.v2.planner.entities import Entities
        e = Entities(raw_misc={
            "attribution_text": "the fog came pouring in...!",
            "raw_text": "x",
        })
        p = plan_mod.build("author_attribution", e)
        query = p.steps[0].args["query"]
        # Should not have trailing ! or .
        inner = query.strip('"')
        self.assertFalse(inner.endswith(("!", "?", ".")),
                          msg=f"FTS5 query has bad terminal: {query!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
