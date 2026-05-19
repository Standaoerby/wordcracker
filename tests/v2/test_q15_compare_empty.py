"""Round 10 Q15 hotfix — compare_authors empty-side render guard.

When one side of a compare returns 0 signature words, the renderer
historically invented words to «balance» the answer. 6-round
persistent bug. Acceptance report flagged as P0 blocker for v3.0
release.

Fix: tool wrapper attaches a forceful _render_note that explicitly
forbids inventing words for the empty side."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class CompareAuthorsEmptySide(unittest.TestCase):

    def _v1_response(self, has_a: bool, has_b: bool):
        """Build a v1 compare_authors response with controllable empty
        sides. Real v1 returns either a populated list or empty list."""
        return {
            "author1_regex": "^Wilde,",
            "author2_regex": "^Lovecraft,",
            "books_a": 12,
            "books_b": 0,
            "top_unique_a": ([{"word": "ernest", "affinity": 100},
                               {"word": "dandy",  "affinity": 80}]
                              if has_a else []),
            "top_unique_b": ([{"word": "cyclopean", "affinity": 90},
                               {"word": "eldritch",  "affinity": 70}]
                              if has_b else []),
            "shared_high_affinity": [],
            "cosine_similarity": 0.0,
        }

    def test_empty_side_b_gets_forceful_render_note(self):
        """Q15 exact scenario — Lovecraft (B) returns empty, Wilde (A) ok."""
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=self._v1_response(has_a=True, has_b=False)):
            r = compare_authors("^Wilde,", "^Lovecraft,")
        self.assertTrue(r.ok)
        note = r.data.get("_render_note", "")
        self.assertIn("ЗАПРЕЩЕНО", note)
        self.assertIn("author2", note)
        self.assertIn("Lovecraft", note)
        # Empty-side warning code present
        codes = [w.code for w in r.warnings]
        self.assertIn("author2_empty", codes)

    def test_empty_sides_structured_field(self):
        """data.empty_sides surfaces which sides are empty for the renderer."""
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=self._v1_response(has_a=True, has_b=False)):
            r = compare_authors("^Wilde,", "^Lovecraft,")
        empty = r.data.get("empty_sides", [])
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["label"], "author2")
        self.assertEqual(empty[0]["regex"], "^Lovecraft,")

    def test_both_sides_empty_gets_dual_clarify(self):
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=self._v1_response(has_a=False, has_b=False)):
            r = compare_authors("^X,", "^Y,")
        note = r.data.get("_render_note", "")
        self.assertIn("ОБЕ стороны пусты", note)
        codes = [w.code for w in r.warnings]
        self.assertIn("author1_empty", codes)
        self.assertIn("author2_empty", codes)

    def test_both_sides_present_no_warning(self):
        """Happy path — both authors have signature → no empty warning,
        no render_note about emptiness."""
        from scripts.v2.tools.authors.affinity import compare_authors
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=self._v1_response(has_a=True, has_b=True)):
            r = compare_authors("^Wilde,", "^Doyle,")
        codes = [w.code for w in r.warnings]
        self.assertNotIn("author1_empty", codes)
        self.assertNotIn("author2_empty", codes)
        # No empty_sides field
        self.assertNotIn("empty_sides", r.data)

    def test_render_note_threads_to_pipeline(self):
        """_render_note must be picked up by _collect_render_instructions
        in rag_v2 — it surfaces as render_instructions array to the LLM."""
        from scripts.v2.tools.authors.affinity import compare_authors
        from scripts.v2.rag_v2 import _collect_render_instructions
        with mock.patch("scripts.rag_tools.compare_authors",
                         return_value=self._v1_response(has_a=True, has_b=False)):
            r = compare_authors("^Wilde,", "^Lovecraft,")
        instructions = _collect_render_instructions([r])
        # At least one instruction string mentions the empty-side ban
        self.assertTrue(any("ЗАПРЕЩЕНО" in s for s in instructions),
                        msg=f"render_instructions did not carry the empty-side guard: {instructions}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
