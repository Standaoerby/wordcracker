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


# Self-/character names the scrub chain (self-name drop → literary PROPN
# blacklist → curated surname blocklist) is known to remove. Asserting against
# THIS set instead of hard-coded ranking positions makes the contract test
# robust to keyness re-ranking: the keyness upgrade (G² + stopword exclusion)
# changes WHICH names land in the recorded top-N, but the invariant — every
# name that IS present gets scrubbed, and non-name vocabulary survives — holds
# regardless. (The strict word-level guarantees live in the synthetic
# E27WodehouseWildeNegative class below, which is ranking-independent.)
_SCRUBBED_NAMES = frozenset({
    "austen", "wilde",                                   # self-names
    "elinor", "ferrars", "dashwood", "willoughby",
    "collins", "crawford",                               # Austen characters
    "goring", "worthing", "chasuble", "algernon",
    "cecily", "cardew", "bunbury", "daubeny", "tetrarch",  # Wilde characters
})


class E27CompareAuthorsGoldenFixture(unittest.TestCase):
    """Contract test against the real recorded v1 shape (Austen vs Wilde).

    Ranking-robust: it derives the pollution to scrub from whatever the
    fixture currently carries, so a keyness re-record doesn't make it brittle.
    """

    def setUp(self):
        self.v1_raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        # Guard: the fixture really is the nested v1 shape we're contracting
        # against (NOT a pre-flattened mock).
        self.assertIn("author1", self.v1_raw)
        self.assertIn("top_unique", self.v1_raw["author1"])
        a1 = set(_words(self.v1_raw["author1"]["top_unique"]))
        a2 = set(_words(self.v1_raw["author2"]["top_unique"]))
        # The names actually present in this recording that the scrub must kill.
        self._a1_names = a1 & _SCRUBBED_NAMES
        self._a2_names = a2 & _SCRUBBED_NAMES
        # The fixture must still carry SOME known name on at least one side,
        # else this contract test has no pollution to assert against (would be
        # vacuously green — a silent loss of coverage).
        self.assertTrue(
            self._a1_names or self._a2_names,
            "fixture carries no known character/self name to scrub — re-record "
            "may have changed the corpus or the curated name set drifted.")

    def _run(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                        return_value=json.loads(json.dumps(self.v1_raw))):
            return compare_authors("^Austen,", "^Wilde, Oscar$",
                                   min_corpus_count=500)

    def test_names_dropped_both_sides(self):
        """Every self-/character name present in the fixture is scrubbed from
        both the flat aliases the renderer reads."""
        r = self._run()
        self.assertTrue(r.ok)
        a = set(_words(r.data["top_unique_a"]))
        b = set(_words(r.data["top_unique_b"]))
        self.assertEqual(a & self._a1_names, set(),
                         f"author1 names not scrubbed: {a & self._a1_names}")
        self.assertEqual(b & self._a2_names, set(),
                         f"author2 names not scrubbed: {b & self._a2_names}")

    def test_scrub_is_not_a_blanket_nuke(self):
        """NEGATIVE half: the scrub removes names, NOT all vocabulary. At least
        one side must retain non-name signature words after scrubbing."""
        r = self._run()
        a_nonname = [w for w in _words(r.data["top_unique_a"])
                     if w not in _SCRUBBED_NAMES]
        b_nonname = [w for w in _words(r.data["top_unique_b"])
                     if w not in _SCRUBBED_NAMES]
        self.assertTrue(
            a_nonname or b_nonname,
            "scrub left no non-name vocabulary on either side — too aggressive")

    def test_nested_render_payload_also_scrubbed(self):
        """The LLM render payload exposes raw['author1'/'author2'] nested
        top_unique — those must be cleaned too, not just the flat aliases."""
        r = self._run()
        a_nested = set(_words(r.data["author1"]["top_unique"]))
        b_nested = set(_words(r.data["author2"]["top_unique"]))
        self.assertEqual(a_nested & self._a1_names, set())
        self.assertEqual(b_nested & self._a2_names, set())


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
