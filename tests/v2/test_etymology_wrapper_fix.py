"""Sprint 18+ — find_words_by_etymology wrapper typo fix.

Stan 2026-05-19 prod: «германские слова Толкина» showed 15 valid
results in the table AND a confusing «no_matches» warning. Root
cause: wrapper read `raw.get("matches")` but v1 tool returns
`matched`. Always-empty `rows` → false warning."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import ToolResult


class EtymologyWrapperKeyFix(unittest.TestCase):

    def _v1_response(self, n: int):
        """Build a v1 find_words_by_etymology response with n matched
        words. Uses the actual v1 key («matched», not «matches»)."""
        return {
            "scope": {"author": "^Tolkien,"},
            "family": "germanic",
            "candidates_examined": 200,
            "cold_wiktionary_lookups": 0,
            "matched": [
                {"word": f"word{i}", "affinity": 100 - i,
                 "occurrences": 10, "corpus_count": 1000,
                 "family_chain": ["middle_english", "old_english",
                                   "proto_germanic"],
                 "raw_codes": ["enm", "ang", "gem-pro"]}
                for i in range(n)
            ],
        }

    def test_warning_does_not_fire_when_words_found(self):
        from scripts.v2.tools.words.etymology import find_words_by_etymology
        with mock.patch("scripts.rag_tools.find_words_by_etymology",
                         return_value=self._v1_response(15)):
            r = find_words_by_etymology({"author": "^Tolkien,"},
                                         family="germanic")
        codes = [w.code for w in r.warnings]
        self.assertNotIn("no_matches", codes,
            msg="false no_matches warning fired when v1 found 15 words")

    def test_warning_fires_when_empty(self):
        from scripts.v2.tools.words.etymology import find_words_by_etymology
        with mock.patch("scripts.rag_tools.find_words_by_etymology",
                         return_value=self._v1_response(0)):
            r = find_words_by_etymology({"author": "^Tolkien,"},
                                         family="germanic")
        codes = [w.code for w in r.warnings]
        self.assertIn("no_matches", codes)

    def test_render_note_set_on_germanic_results(self):
        """When germanic family returns ME function words, the render
        hint must tell the LLM to set expectations correctly."""
        from scripts.v2.tools.words.etymology import find_words_by_etymology
        with mock.patch("scripts.rag_tools.find_words_by_etymology",
                         return_value=self._v1_response(15)):
            r = find_words_by_etymology({"author": "^Tolkien,"},
                                         family="germanic")
        self.assertIn("_render_note", r.data)
        note = r.data["_render_note"]
        self.assertIn("germanic", note)
        # Mentions function words explicitly
        self.assertTrue(any(w in note for w in ("функциональные", "wite", "ich")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
