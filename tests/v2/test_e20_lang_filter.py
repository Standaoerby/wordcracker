"""E20 (2026-05-22) — hybrid_search lang filter must handle PG
metadata's stringified-list shape («['en']»), not just plain «en».

ROOT CAUSE (Stan prod 2026-05-22):
Query «примеры использования слова "ajar" в английской литературе»
returned «Слово «ajar» не встретилось в весь корпус» despite hybrid_search
finding 50 lexical + 50 semantic + 30 RRF-merged matches. The lang
post-filter dropped ALL 30 because:

  book_lang = meta.get('language')  # "['en']" (Python-repr'd list)
  want = 'en'
  book_lang == want  → False  ❌  always

PG catalog CSV stores `language` as a stringified Python list:
  - "['en']"        — most books
  - "['en', 'fr']"  — multi-lang
  - "['fr']"        — non-English

Other v1 callers (e.g. affinity_by_author at rag_tools.py:1296) work
around it with `df["language"].str.contains("'en'", regex=False)`.
The hybrid_search wrapper missed this idiom and used strict equality
→ 100% drop rate for any English query with lang_hint set.

FIX:
  if not book_lang or want in book_lang:    # substring containment
      filtered.append(m)
  else:
      lang_dropped += 1

Catches all three shapes (raw, single-list, multi-list) without false
positives (a book with language='french' won't match 'en' because
'en' is not a substring of 'french').
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class LangFilterSubstring(unittest.TestCase):
    """E20 — verify the substring-containment lang filter accepts the
    real shapes PG metadata produces."""

    def _build_inputs(self):
        """Helper to construct realistic matches + meta lookup."""
        lex_matches = [
            {"pg_id": "PG65232", "snippet": "The door is ajar",
             "title": "Adytum", "author": "X"},
            {"pg_id": "PG32439", "snippet": "left the front door ajar",
             "title": "Hasbrouck", "author": "Y"},
            {"pg_id": "PG10001", "snippet": "porte entrouverte",
             "title": "Madame Bovary", "author": "Flaubert"},
        ]
        meta_by_pg = {
            "PG65232": {"language": "['en']", "title": "Adytum",
                         "author": "X"},
            "PG32439": {"language": "['en']", "title": "Hasbrouck",
                         "author": "Y"},
            "PG10001": {"language": "['fr']", "title": "Madame Bovary",
                         "author": "Flaubert"},
        }
        return lex_matches, meta_by_pg

    def test_pg_list_shape_en_passes_filter(self):
        """«['en']» metadata + lang='en' query must keep the matches."""
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage
        lex_matches, meta_by_pg = self._build_inputs()

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=3, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta_by_pg.get(pid, default)
            return _T()

        def _dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            return ToolResult.fail(tool="semantic_search",
                                   err_type="internal", message="off")
        with mock.patch("scripts.v2.tools.search.hybrid.dispatch",
                         side_effect=_dispatch), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            r = hybrid_search(query="ajar", k=12, lang="en")

        self.assertTrue(r.ok)
        # 2 English books survived, 1 French dropped
        matches = r.data.get("matches", [])
        match_pgs = {m["pg_id"] for m in matches}
        self.assertEqual(match_pgs, {"PG65232", "PG32439"})
        # lang_filtered warning is emitted with correct count
        codes = {w.code for w in r.warnings}
        self.assertIn("lang_filtered", codes)
        for w in r.warnings:
            if w.code == "lang_filtered":
                self.assertIn("1", w.message)  # 1 non-en dropped

    def test_plain_lang_string_also_works(self):
        """If metadata is ever cleaned to «en» (no brackets), still OK."""
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage

        lex_matches = [{"pg_id": "PG1", "snippet": "x",
                         "title": "T", "author": "A"}]
        meta_by_pg = {"PG1": {"language": "en", "title": "T",
                                "author": "A"}}

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=1, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta_by_pg.get(pid, default)
            return _T()

        def _dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            return ToolResult.fail(tool="semantic_search",
                                   err_type="internal", message="off")
        with mock.patch("scripts.v2.tools.search.hybrid.dispatch",
                         side_effect=_dispatch), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            r = hybrid_search(query="ajar", k=12, lang="en")

        self.assertTrue(r.ok)
        self.assertEqual(len(r.data.get("matches", [])), 1)

    def test_multi_lang_list_keeps_if_contains_wanted(self):
        """«['en', 'fr']» bilingual book matches when lang='en'."""
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage

        lex_matches = [{"pg_id": "PG1", "snippet": "x",
                         "title": "T", "author": "A"}]
        meta_by_pg = {"PG1": {"language": "['en', 'fr']", "title": "T",
                                "author": "A"}}

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=1, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta_by_pg.get(pid, default)
            return _T()

        def _dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            return ToolResult.fail(tool="semantic_search",
                                   err_type="internal", message="off")
        with mock.patch("scripts.v2.tools.search.hybrid.dispatch",
                         side_effect=_dispatch), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            r = hybrid_search(query="ajar", k=12, lang="en")

        self.assertEqual(len(r.data.get("matches", [])), 1)

    def test_french_book_dropped_when_lang_en(self):
        """Negative case: non-English book with «['fr']» dropped."""
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage

        lex_matches = [{"pg_id": "PG1", "snippet": "x",
                         "title": "Bovary", "author": "Flaubert"}]
        meta_by_pg = {"PG1": {"language": "['fr']", "title": "Bovary",
                                "author": "Flaubert"}}

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=1, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta_by_pg.get(pid, default)
            return _T()

        def _dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            return ToolResult.fail(tool="semantic_search",
                                   err_type="internal", message="off")
        with mock.patch("scripts.v2.tools.search.hybrid.dispatch",
                         side_effect=_dispatch), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            r = hybrid_search(query="ajar", k=12, lang="en")

        # French book dropped
        self.assertEqual(len(r.data.get("matches", [])), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
