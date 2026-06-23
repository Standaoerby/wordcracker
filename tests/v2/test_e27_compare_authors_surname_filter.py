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


# Self-/character names that must NEVER appear in an author's stylistic
# signature. As of 2.7.37 they are dropped at the v1 SOURCE by the author-level
# NER shield (RAG_TASK_author_keyness_propn / ADR 2.7.37): _author_propn_set
# unions whole-book NER over the author's books and drops every matched word
# before head(top). The v2 wrapper's name-cleaning chain (self-name drop →
# literary PROPN blacklist → curated surname blocklist) stays as a second,
# defense-in-depth layer. Either layer alone keeps this set out of the output,
# so the recorded golden is name-free by design — the test asserts ABSENCE of
# the whole set, not "scrub whatever names happen to be recorded". (The live
# wrapper-scrub regression — names present in v1 → dropped — is the synthetic,
# ranking-independent E27WodehouseWildeNegative class below.)
_SCRUBBED_NAMES = frozenset({
    "austen", "wilde",                                   # self-names
    "elinor", "ferrars", "dashwood", "willoughby",
    "collins", "crawford",                               # Austen characters
    "goring", "worthing", "chasuble", "algernon",
    "cecily", "cardew", "bunbury", "daubeny", "tetrarch",  # Wilde characters
})


class E27CompareAuthorsGoldenFixture(unittest.TestCase):
    """End-to-end check against the real recorded v1 shape (Austen vs Wilde).

    Until 2.7.36 the v1 raw still carried self/character names and this class
    verified the v2 wrapper scrubbed them. As of 2.7.37 the author-level NER
    shield drops those names at the v1 SOURCE, so the recorded fixture is
    name-free BY DESIGN (ADR 2.7.37: "golden fixtures WILL change — proper
    nouns drop from top — that is the intended outcome"). Requiring a name to
    still be present is therefore unsatisfiable; that guard is gone.

    What this class still guarantees on REAL data: the full pipeline
    (v1 shield + wrapper) yields name-free, non-empty signatures on both sides
    and never re-introduces a curated name — flat aliases and nested payload.
    The wrapper-scrub regression itself lives in E27WodehouseWildeNegative;
    the v1-source drop (the dunwich leak) lives in test_author_keyness_propn.
    """

    def setUp(self):
        self.v1_raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        # Guard: the fixture really is the nested v1 shape we contract against
        # (NOT a pre-flattened mock). No "names must be present" guard — the
        # 2.7.37 shield makes the recorded golden name-free by design.
        self.assertIn("author1", self.v1_raw)
        self.assertIn("top_unique", self.v1_raw["author1"])
        self.assertIn("author2", self.v1_raw)
        self.assertIn("top_unique", self.v1_raw["author2"])

    def _run(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                        return_value=json.loads(json.dumps(self.v1_raw))):
            return compare_authors("^Austen,", "^Wilde, Oscar$",
                                   min_corpus_count=500)

    def test_names_dropped_both_sides(self):
        """No curated self-/character name reaches the flat aliases the
        renderer reads — dropped at the v1 shield or the wrapper, either way."""
        r = self._run()
        self.assertTrue(r.ok)
        a = set(_words(r.data["top_unique_a"]))
        b = set(_words(r.data["top_unique_b"]))
        self.assertEqual(a & _SCRUBBED_NAMES, set(),
                         f"author1 carries names: {a & _SCRUBBED_NAMES}")
        self.assertEqual(b & _SCRUBBED_NAMES, set(),
                         f"author2 carries names: {b & _SCRUBBED_NAMES}")

    def test_scrub_is_not_a_blanket_nuke(self):
        """NEGATIVE half: the pipeline removes names, NOT all vocabulary — real
        signature words survive on BOTH sides, never the empty list the §8
        over-drop bug produced (shield had truncated the pool to nothing)."""
        r = self._run()
        a_nonname = [w for w in _words(r.data["top_unique_a"])
                     if w not in _SCRUBBED_NAMES]
        b_nonname = [w for w in _words(r.data["top_unique_b"])
                     if w not in _SCRUBBED_NAMES]
        self.assertTrue(a_nonname, "author1 signature empty after scrub")
        self.assertTrue(b_nonname, "author2 signature empty after scrub")

    def test_nested_render_payload_also_scrubbed(self):
        """The LLM render payload exposes raw['author1'/'author2'] nested
        top_unique — those must be name-free too, not just the flat aliases."""
        r = self._run()
        a_nested = set(_words(r.data["author1"]["top_unique"]))
        b_nested = set(_words(r.data["author2"]["top_unique"]))
        self.assertEqual(a_nested & _SCRUBBED_NAMES, set())
        self.assertEqual(b_nested & _SCRUBBED_NAMES, set())


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
