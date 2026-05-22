"""E23-E26 (2026-05-22) — persona-beginner-researcher batch fixes.

Source: [[test_external_claude_2026-05-22_persona_beginner_researcher]]
external test report. 12 queries, strict 42%. Persona: новичок пишет
эссе про готический роман XIX века.

Failures grouped + fixed:

  E23 (Q5, Q8) — book_readability CEFR пуст. v1 returns `cefr_heuristic`
                 (rag_tools.py:1169), wrapper read `cefr/cefr_estimate`.

  E24 (Q4, Q12) — book_emotion view «Вхождений» column empty. v1 returns
                  share_among_primary_emotions (used by wrapper) but
                  count was set to None. Now populated from per_million.

  E25 (Q6) — «что сложнее ДЛЯ ЧТЕНИЯ — Дракула или Франкенштейн»
             matched book_readability (single) instead of
             book_readability_compare because compare patterns required
             literal «читать» word. Added «для чтения/понимания» variant.

  E26 (Q9) — book_similar «что почитать после Дракулы» returned «Project
             Gutenberg (1971-2009)» as top result, plus 1866 bookseller's
             catalogue, Greek-myths textbook, «History of English
             Romanticism». Added META-title blocklist to drop bibliography/
             catalogue/encyclopedia entries from recommendation results.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class E23ReadabilityCefr(unittest.TestCase):
    """E23 — book_readability view reads v1's `cefr_heuristic` key."""

    def test_cefr_heuristic_passes_through(self):
        from scripts.v2.tools.books.readability import book_readability

        v1_real_shape = {
            "id": "PG84", "pg_id": "PG84", "title": "Frankenstein",
            "author": "Shelley, Mary",
            "sampled_chars": 200000, "sentences": 1234, "words": 34567,
            "flesch_reading_ease": 56.8,
            "flesch_kincaid_grade": 11.3,
            "cefr_heuristic": "B2",   # v1's actual key
        }
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value=v1_real_shape):
            r = book_readability(pg_id="PG84")
        self.assertTrue(r.ok)
        view = r.view
        self.assertIsNotNone(view)
        cefr_payload = (view.payload or {}).get("cefr")
        self.assertEqual(cefr_payload, "B2",
                          "view must surface v1's cefr_heuristic value")

    def test_legacy_cefr_key_silently_dropped(self):
        """Phase 2 (R3/R4) — phantom `cefr` key is NO LONGER read; v1
        canonical is `cefr_heuristic`. The wrapper still produces an OK
        result (other readability fields propagate), but the CEFR slot
        in the view stays empty.
        """
        from scripts.v2.tools.books.readability import book_readability

        v1_legacy = {
            "id": "PG84", "pg_id": "PG84", "title": "Frankenstein",
            "flesch_reading_ease": 56.8,
            "flesch_kincaid_grade": 10.5,
            "cefr": "B2",  # phantom — wrapper ignores
        }
        with mock.patch("scripts.rag_tools.book_readability",
                         return_value=v1_legacy):
            r = book_readability(pg_id="PG84")
        self.assertTrue(r.ok)
        # Phantom `cefr` key is invisible to the wrapper — CEFR slot
        # is empty even though the legacy mock provided a value.
        self.assertIsNone((r.view.payload or {}).get("cefr"))


class E24EmotionCounts(unittest.TestCase):
    """E24 — book_emotion_profile populates count column from
    per_million when share is the source."""

    def test_per_million_surfaces_as_count(self):
        from scripts.v2.tools.books.top_books import book_emotion_profile

        # Realistic v1 shape — all 8 NRC emotions, shares sum to 1.0
        v1_real_shape = {
            "id": "PG84", "title": "Frankenstein", "author": "Shelley",
            "total_tokens": 78231,
            "share_among_primary_emotions": {
                "fear": 0.302, "sadness": 0.201, "anticipation": 0.181,
                "trust": 0.113, "anger": 0.089, "joy": 0.058,
                "disgust": 0.031, "surprise": 0.025,
            },
            "per_million": {
                "fear": 4823.1, "sadness": 3210.4, "anticipation": 2901.7,
                "trust": 1810.5, "anger": 1421.0, "joy": 921.2,
                "disgust": 502.3, "surprise": 388.1,
            },
        }
        with mock.patch("scripts.rag_tools.book_emotion_profile",
                         return_value=v1_real_shape):
            r = book_emotion_profile(pg_id="PG84")
        self.assertTrue(r.ok)
        emotions = (r.view.payload or {}).get("emotions") or []
        # Find fear entry
        fear = next((e for e in emotions if e["emotion"] == "fear"), None)
        self.assertIsNotNone(fear)
        self.assertIsNotNone(fear["count"],
                              "count must not be None when per_million is "
                              "available; persona Q4/Q12 saw «Вхождений» column "
                              "empty")
        # Count comes from per_million rounded to int
        self.assertEqual(fear["count"], 4823)


class E25ReadabilityCompareRouting(unittest.TestCase):
    """E25 — book_readability_compare ловит «для чтения» вариант."""

    def test_dlya_chteniya_or_routes_to_compare(self):
        """Persona Q6 exact phrasing — was breaking on this."""
        from scripts.v2.planner.intent import classify
        m = classify('что сложнее для чтения — "Дракула" или "Франкенштейн"')
        self.assertEqual(m.label, "book_readability_compare",
                          f"expected book_readability_compare, got {m.label}")

    def test_chto_slozhnee_dlya_chteniya(self):
        from scripts.v2.planner.intent import classify
        m = classify("что сложнее для чтения, Дракула или Франкенштейн")
        self.assertEqual(m.label, "book_readability_compare")

    def test_legacy_chitat_variant_still_works(self):
        """Existing «читать ... или» patterns must keep working."""
        from scripts.v2.planner.intent import classify
        m = classify("что сложнее читать, Дракула или Франкенштейн")
        self.assertEqual(m.label, "book_readability_compare")

    def test_single_book_readability_unaffected(self):
        """Single-book readability query must NOT escalate to compare."""
        from scripts.v2.planner.intent import classify
        m = classify("уровень сложности Pride and Prejudice")
        self.assertEqual(m.label, "book_readability")


class E26MetaBlocklist(unittest.TestCase):
    """E26 — find_book_by_topic drops Project Gutenberg / catalogue /
    bibliography entries from recommendation results."""

    def test_is_meta_title_helper(self):
        from scripts.v2.tools.books.find_book_by_topic import _is_meta_title

        # Positives — persona Q9 actual offenders
        self.assertTrue(_is_meta_title("Project Gutenberg (1971-2009)"))
        self.assertTrue(_is_meta_title("Catalogue of London Books, 1866"))
        self.assertTrue(_is_meta_title("A History of English Romanticism"))
        self.assertTrue(_is_meta_title("Bibliography of Modern Drama"))
        self.assertTrue(_is_meta_title("Manual of English Literature"))
        self.assertTrue(_is_meta_title("Encyclopedia of British Authors"))
        self.assertTrue(_is_meta_title("The Cambridge History of Drama"))

        # Negatives — real fiction must NOT be dropped
        self.assertFalse(_is_meta_title("Dracula"))
        self.assertFalse(_is_meta_title("Frankenstein"))
        self.assertFalse(_is_meta_title("The Picture of Dorian Gray"))
        self.assertFalse(_is_meta_title("The Castle of Otranto"))
        self.assertFalse(_is_meta_title("Northanger Abbey"))
        # Edge — book title containing «catalogue» as fiction is rare
        # but should NOT get dropped if it's not «catalogue of …»
        self.assertFalse(_is_meta_title("Mr Witt's Widow"))

    def test_none_and_empty_handled(self):
        from scripts.v2.tools.books.find_book_by_topic import _is_meta_title
        self.assertFalse(_is_meta_title(None))
        self.assertFalse(_is_meta_title(""))
        self.assertFalse(_is_meta_title("   "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
