"""Sprint 22+ alpha6 — Round 13 follow-on fixes.

External Claude Round 13 (vs Round 12 regression) showed:
  - Q11/Q12 alpha3 v4 LLM-planner works (deployed)
  - Q17 Hemingway copyright OOS works (deployed via v4 path)
  - Q14 number format works (closed)
  - Strict pass: 60% → 70%, loose 75% → 85%

Remaining issues from R13:
  - Q13 Marlowe non-determinism: R12 showed 12 books in table,
    R13 wrote «не указано напрямую» as prose. Tool data identical.
  - 4/20 queries had renderer variance on tabular data.
  - B1 stream timeout (Q3, Q15): ~120s tool calls drop SSE.

Alpha6 fixes:
  1. RENDER_PROMPT rule 18 — array → MUST table
  2. _LOW_TEMP_INTENTS — temp 0.1 for table-heavy intents
  3. SSE keep-alive heartbeat in chat_server
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TableMustRule(unittest.TestCase):
    """RENDER_PROMPT rule 18 explicitly forbids losing tabular data
    to prose. Stan R13 Q13."""

    def test_rule_18_present(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Tabular data", RENDER_PROMPT)
        self.assertIn("ОБЯЗАТЕЛЬНО markdown table", RENDER_PROMPT)
        # Concrete user-pain example
        self.assertIn("Marlowe", RENDER_PROMPT)


class LowTempIntents(unittest.TestCase):
    """Table-heavy intents render with temp=0.1 instead of 0.3."""

    def test_known_low_temp_intents_present(self):
        from scripts.v2.rag_v2 import _LOW_TEMP_INTENTS
        # All Marlowe-affected intents
        self.assertIn("author_lookup", _LOW_TEMP_INTENTS)
        # All table-heavy author intents
        self.assertIn("author_vocab", _LOW_TEMP_INTENTS)
        self.assertIn("author_compare", _LOW_TEMP_INTENTS)
        self.assertIn("top_authors_books", _LOW_TEMP_INTENTS)
        # Book intents
        self.assertIn("book_compare", _LOW_TEMP_INTENTS)
        self.assertIn("book_readability_compare", _LOW_TEMP_INTENTS)
        # Word intents
        self.assertIn("word_etymology", _LOW_TEMP_INTENTS)
        self.assertIn("word_collocates", _LOW_TEMP_INTENTS)

    def test_prose_intents_not_in_low_temp(self):
        from scripts.v2.rag_v2 import _LOW_TEMP_INTENTS
        # Intro/clarify keep natural variation
        self.assertNotIn("introduction", _LOW_TEMP_INTENTS)
        self.assertNotIn("clarify", _LOW_TEMP_INTENTS)
        self.assertNotIn("out_of_scope", _LOW_TEMP_INTENTS)

    def test_temp_lowered_for_author_vocab(self):
        """Render call should pass temperature=0.1 for author_vocab."""
        from scripts.v2 import rag_v2
        from scripts.v2._types import ToolResult, Coverage
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities
        plan = QueryPlan(intent="author_vocab",
                         entities=Entities(author_regex="^Marlowe,"),
                         steps=[])
        result = ToolResult.success(
            tool="author_metadata", data={"top_words": [{"word": "x"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        captured = {}
        def fake_post(url, json=None, **kw):
            captured["payload"] = json
            class FakeResp:
                def raise_for_status(self): pass
                def iter_lines(self):
                    import json as _j
                    return [_j.dumps(self.json()).encode()]
                def close(self): pass
                def json(self):
                    return {"message": {"content": "ok"},
                            "prompt_eval_count": 100}
            return FakeResp()
        with mock.patch("scripts.v2.rag_v2.requests.post",
                         side_effect=fake_post):
            rag_v2._llm_render("q", plan, [result],
                                model="qwen3:14b", ollama_host="http://x")
        self.assertEqual(captured["payload"]["options"]["temperature"], 0.1)

    def test_temp_default_for_clarify(self):
        """Render call should pass temperature=0.3 for prose intents."""
        from scripts.v2 import rag_v2
        from scripts.v2._types import ToolResult, Coverage
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.entities import Entities
        plan = QueryPlan(intent="clarify",
                         entities=Entities(),
                         steps=[])
        result = ToolResult.success(
            tool="x", data={"y": 1},
            coverage=Coverage(books_matched=0, books_total=0),
        )
        captured = {}
        def fake_post(url, json=None, **kw):
            captured["payload"] = json
            class FakeResp:
                def raise_for_status(self): pass
                def iter_lines(self):
                    import json as _j
                    return [_j.dumps(self.json()).encode()]
                def close(self): pass
                def json(self):
                    return {"message": {"content": "ok"},
                            "prompt_eval_count": 100}
            return FakeResp()
        with mock.patch("scripts.v2.rag_v2.requests.post",
                         side_effect=fake_post):
            rag_v2._llm_render("q", plan, [result],
                                model="qwen3:14b", ollama_host="http://x")
        self.assertEqual(captured["payload"]["options"]["temperature"], 0.3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
