"""E27 (S-R4, 2026-05-29) — compare_authors must scrub author surnames +
character names from each side's signature list.

Before:
    compare_authors passed v1's nested author1/author2.top_unique straight
    through (only flattened to top_unique_a/b). affinity_by_author scrubs
    its `top` via _drop_author_self_name → _LITERARY_PROPN_BLACKLIST →
    filter_surnames, but compare_authors never wired the same chain. Stan
    Q3: «сравни Wodehouse и Wilde» surfaced wodehouse/wilde (self-names) +
    jeeves/goring (characters) as «фирменные слова».

After:
    both sides go through the identical name-cleaning chain before the
    view / entities / empty-side detection.

R3/R4 compliance: the contract test feeds the REAL recorded v1 shape
(golden fixture scripts.rag_tools.compare_authors.json — nested
author1/author2.top_unique), not a flat-key mock.
R2/R5 compliance: positive (pollution dropped) + negative (genuine
stylistic words survive — the scrub is not a blanket nuke).
"""
from __future__ import annotations

import json
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

_FIXTURE = (_REPO / "scripts" / "v2" / "contracts" / "fixtures"
            / "scripts.rag_tools.compare_authors.json")


def _words(rows):
    return [(r.get("word") if isinstance(r, dict) else str(r)) for r in (rows or [])]


class E27CompareAuthorsGoldenFixture(unittest.TestCase):
    """Contract test against the real recorded v1 shape (Austen vs Wilde)."""

    def setUp(self):
        self.v1_raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        # Guard: the fixture really is the nested v1 shape we're contracting
        # against (NOT a pre-flattened mock). If the fixture is ever
        # re-recorded into a different shape this assertion fails loudly.
        self.assertIn("author1", self.v1_raw)
        self.assertIn("top_unique", self.v1_raw["author1"])
        # And it really does carry the pollution this test exists to kill.
        a1_words = _words(self.v1_raw["author1"]["top_unique"])
        a2_words = _words(self.v1_raw["author2"]["top_unique"])
        self.assertIn("austen", a1_words, "fixture should still show the leak")
        self.assertIn("wilde", a2_words, "fixture should still show the leak")
        self.assertIn("elinor", a1_words)
        self.assertIn("goring", a2_words)

    def _run(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                        return_value=json.loads(json.dumps(self.v1_raw))):
            return compare_authors("^Austen,", "^Wilde, Oscar$",
                                   min_corpus_count=500)

    def test_self_names_dropped_both_sides(self):
        r = self._run()
        self.assertTrue(r.ok)
        a = _words(r.data["top_unique_a"])
        b = _words(r.data["top_unique_b"])
        self.assertNotIn("austen", a, "author1 self-name must be dropped")
        self.assertNotIn("wilde", b, "author2 self-name must be dropped")

    def test_character_names_dropped_both_sides(self):
        r = self._run()
        a = _words(r.data["top_unique_a"])
        b = _words(r.data["top_unique_b"])
        # Austen characters (curated surname blocklist)
        for ch in ("elinor", "ferrars", "dashwood", "willoughby",
                   "collins", "crawford"):
            self.assertNotIn(ch, a, f"Austen character {ch!r} must be dropped")
        # Wilde characters (literary PROPN blacklist — NOT in surname list,
        # which is exactly why the two named filters alone are insufficient)
        for ch in ("goring", "worthing", "chasuble", "algernon",
                   "cecily", "cardew", "bunbury", "daubeny", "tetrarch"):
            self.assertNotIn(ch, b, f"Wilde character {ch!r} must be dropped")

    def test_genuine_signature_words_survive(self):
        """NEGATIVE half (NOT-X → NOT-Y): the scrub drops names, NOT real
        stylistic vocabulary. If this fails the filter is too aggressive."""
        r = self._run()
        a = _words(r.data["top_unique_a"])
        b = _words(r.data["top_unique_b"])
        for w in ("gentlemanlike", "curricle", "charade"):
            self.assertIn(w, a, f"genuine Austen word {w!r} must survive")
        for w in ("individualism", "modernity", "nihilists",
                  "handicraftsmen"):
            self.assertIn(w, b, f"genuine Wilde word {w!r} must survive")

    def test_nested_render_payload_also_scrubbed(self):
        """The LLM render payload exposes raw['author1'/'author2'] nested
        top_unique — those must be cleaned too, not just the flat aliases,
        else the renderer can still read the polluted nested list."""
        r = self._run()
        a_nested = _words(r.data["author1"]["top_unique"])
        b_nested = _words(r.data["author2"]["top_unique"])
        self.assertNotIn("austen", a_nested)
        self.assertNotIn("wilde", b_nested)
        self.assertNotIn("elinor", a_nested)
        self.assertNotIn("goring", b_nested)


class E27WodehouseWildeNegative(unittest.TestCase):
    """The brief's literal acceptance: «сравни Wodehouse и Wilde» → no
    wodehouse/wilde and no characters in the output. Synthetic but
    contract-shaped (nested author1/author2.top_unique like real v1)."""

    def _v1(self):
        return {
            "author1": {
                "regex": "^Wodehouse,", "slug": "wodehouse",
                "top_unique": [
                    {"word": "wodehouse", "affinity": 70.0,
                     "author_count": 200, "corpus_count": 5000},
                    {"word": "jeeves", "affinity": 300.0,
                     "author_count": 150, "corpus_count": 900},
                    {"word": "wooster", "affinity": 250.0,
                     "author_count": 140, "corpus_count": 850},
                    {"word": "psmith", "affinity": 200.0,
                     "author_count": 90, "corpus_count": 500},
                    {"word": "burbled", "affinity": 88.0,
                     "author_count": 40, "corpus_count": 600},  # real
                ],
            },
            "author2": {
                "regex": "^Wilde, Oscar$", "slug": "wilde",
                "top_unique": [
                    {"word": "wilde", "affinity": 64.0,
                     "author_count": 217, "corpus_count": 8806},
                    {"word": "goring", "affinity": 406.0,
                     "author_count": 445, "corpus_count": 2840},
                    {"word": "algernon", "affinity": 98.0,
                     "author_count": 275, "corpus_count": 7243},
                    {"word": "dorian", "affinity": 120.0,
                     "author_count": 100, "corpus_count": 1500},
                    {"word": "epigram", "affinity": 75.0,
                     "author_count": 30, "corpus_count": 700},  # real
                ],
            },
            "shared_high_affinity": [],
            "cosine_similarity": 0.0,
            "cosine_note": "low cosine",
            "min_corpus_count": 500,
        }

    def test_no_self_names_no_characters(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                        return_value=self._v1()):
            r = compare_authors("^Wodehouse,", "^Wilde, Oscar$",
                                min_corpus_count=500)
        self.assertTrue(r.ok)
        a = _words(r.data["top_unique_a"])
        b = _words(r.data["top_unique_b"])
        for bad in ("wodehouse", "jeeves", "wooster", "psmith"):
            self.assertNotIn(bad, a, f"{bad!r} (self/character) must be gone")
        for bad in ("wilde", "goring", "algernon", "dorian"):
            self.assertNotIn(bad, b, f"{bad!r} (self/character) must be gone")
        # Genuine words survive
        self.assertIn("burbled", a)
        self.assertIn("epigram", b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
