"""Sprint 20 — corpus markup artifact filter.

Stan 2026-05-19: «xvth» showed up in Christie's top affinity list with
affinity=138 — it's `XV-th` (XV century) from broken text markup, not
vocabulary. v3.1.1 surname filter doesn't catch (not a name). spaCy
PROPN unreliable on Roman-numeral-like tokens. New filter targets
known markup classes.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401
from scripts.v2._types import Coverage, ToolResult


class IsCorpusArtifact(unittest.TestCase):

    def test_roman_numeral_ordinals_caught(self):
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        for w in ["xvth", "iith", "iiith", "ivth", "vth", "vith",
                   "viith", "viiith", "ixth", "xth", "xith", "xiith",
                   "xviith", "xviiith", "xixth", "xxth", "iind", "iiird"]:
            self.assertTrue(is_corpus_artifact(w), w)

    def test_bare_roman_numerals_caught(self):
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        for w in ["ii", "iii", "iv", "vi", "vii", "viii", "ix",
                   "xi", "xii", "xiii", "xiv", "xv", "xvi", "xvii"]:
            self.assertTrue(is_corpus_artifact(w), w)

    def test_real_words_passed(self):
        """Sanity: actual English vocab is NOT flagged."""
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        for w in ["tuppence", "strychnine", "stitch", "embroidery",
                   "couching", "vavasour", "darning", "weft",
                   "the", "and", "of", "blighter", "dashed"]:
            self.assertFalse(is_corpus_artifact(w), w)

    def test_consonant_only_caught(self):
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        for w in ["pqr", "wxyz", "bcd", "lll"]:
            self.assertTrue(is_corpus_artifact(w), w)

    def test_single_char_caught(self):
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        for w in ["a", "b", "c", "z"]:
            self.assertTrue(is_corpus_artifact(w), w)

    def test_empty_and_garbage_safe(self):
        from scripts.v2.tools.authors._corpus_artifacts import is_corpus_artifact
        self.assertFalse(is_corpus_artifact(""))
        self.assertFalse(is_corpus_artifact(None))  # type: ignore[arg-type]
        self.assertFalse(is_corpus_artifact("   "))


class FilterCorpusArtifacts(unittest.TestCase):

    def test_drops_xvth_keeps_real_words(self):
        from scripts.v2.tools.authors._corpus_artifacts import filter_corpus_artifacts
        rows = [
            {"word": "tuppence", "affinity": 5230.24},
            {"word": "xvth", "affinity": 138.77},  # Stan's example
            {"word": "stitching", "affinity": 179.85},
            {"word": "iii", "affinity": 100.0},
            {"word": "embroidery", "affinity": 112.61},
        ]
        kept, dropped = filter_corpus_artifacts(rows)
        self.assertEqual(dropped, 2)  # xvth + iii
        words = [r["word"] for r in kept]
        self.assertNotIn("xvth", words)
        self.assertNotIn("iii", words)
        self.assertIn("tuppence", words)
        self.assertIn("stitching", words)


class AffinityWrapperIntegration(unittest.TestCase):

    def _v1_with_artifacts(self):
        """v1 returns Christie's affinity list including xvth markup."""
        return {
            "author_regex": "^Christie,",
            "slug": "christie",
            "pos_filter": None,
            "effective_min_corpus_count": 500,
            "total_unique_words": 12000,
            # Phase 2 — V1AffinityByAuthor canonical key is `top`.
            "top": [
                {"word": "tuppence", "author_count": 613,
                  "corpus_count": 1230, "affinity": 5230.24},
                {"word": "couching", "author_count": 50,
                  "corpus_count": 697, "affinity": 752.84},
                {"word": "xvth", "author_count": 8,  # ← MARKUP
                  "corpus_count": 605, "affinity": 138.77},
                {"word": "esthonia", "author_count": 8,
                  "corpus_count": 612, "affinity": 137.18},
                {"word": "iii", "author_count": 3,  # ← MARKUP
                  "corpus_count": 200, "affinity": 75.0},
                {"word": "stitching", "author_count": 51,
                  "corpus_count": 2976, "affinity": 179.85},
            ],
            "cached": True,
            "proper_noun_filter": "corpus-diff heuristic dropped 0",
        }

    def test_affinity_by_author_drops_xvth_and_iii(self):
        from scripts.v2.tools.authors.affinity import affinity_by_author
        with mock.patch("scripts.rag_tools.affinity_by_author",
                          return_value=self._v1_with_artifacts()):
            r = affinity_by_author("^Christie,", top=10)
        self.assertTrue(r.ok)
        words = [t["word"] for t in r.data["top"]]
        self.assertNotIn("xvth", words)
        self.assertNotIn("iii", words)
        # Real words preserved
        self.assertIn("tuppence", words)
        self.assertIn("stitching", words)
        # Filter note mentions corpus-artifact drops
        self.assertIn("corpus-artifact", r.data["proper_noun_filter"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
