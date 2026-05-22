"""E22 (2026-05-22) — hybrid_search `want` extraction (twin of E21).

ROOT CAUSE (Stan prod 2026-05-22, final layer):
After E21 fixed _title_lookup to store ISO-639 codes ("en", "fr"
instead of "['e"), the «ajar в английской литературе» query STILL
returned empty. The bug had a TWIN on the query side:

  want = lang.lower().strip().split("-")[0][:3]

Entity extractor turned «английская литература» into
lang_hint="english" (7 chars). The [:3] truncation gave want="eng".
Book metadata (post-E21) was "en". So:

  "eng" in "en"   → False  ❌ drops every match

Stan's empirical sanity check confirmed:
  «примеры использования слова ajar»                       → 1541 chars ✓
  «примеры использования слова "ajar" в английской литературе» → 62 chars empty ❌

E22 applies the SAME robust extraction logic to the query side as E21
did to the book side: extract a 2-3 letter ISO-639 token via regex,
fallback to 2-char slice for full names («english» → «en», «russian»
→ «ru»).

After E22:
  "english"  → "en"   ✓
  "en"       → "en"   ✓
  "en-US"    → "en"   ✓
  "russian"  → "ru"   ✓
  "fr"       → "fr"   ✓

This file contracts the want-extraction so future refactors can't
re-introduce the [:3] truncation on the query side.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _want_extract(raw_lang: str) -> str:
    """Same logic as hybrid_search wrapper's `want` computation —
    isolated for direct testing."""
    import re
    raw = str(raw_lang).lower().strip()
    m = re.match(r"^([a-z]{2,3})\b", raw)
    if m:
        return m.group(1)
    return raw[:2]


class WantExtraction(unittest.TestCase):
    def test_iso_two_letter(self):
        self.assertEqual(_want_extract("en"), "en")

    def test_iso_three_letter(self):
        self.assertEqual(_want_extract("eng"), "eng")

    def test_iso_dashed_locale(self):
        self.assertEqual(_want_extract("en-US"), "en")

    def test_full_name_english(self):
        """Most important case — entity extractor produces 'english'
        from «английская литература». [:3] used to give 'eng' which
        didn't match 'en' book metadata."""
        self.assertEqual(_want_extract("english"), "en")

    def test_full_name_russian(self):
        self.assertEqual(_want_extract("russian"), "ru")

    def test_full_name_french(self):
        self.assertEqual(_want_extract("french"), "fr")

    def test_empty_string_returns_empty(self):
        self.assertEqual(_want_extract(""), "")

    def test_no_three_char_truncation_regression(self):
        """The pre-E22 bug signature was want='eng' from 'english'.
        Assert that never happens."""
        for full in ("english", "russian", "french", "spanish", "italian"):
            extracted = _want_extract(full)
            self.assertEqual(len(extracted), 2,
                             f"{full!r} must extract to 2-char code, "
                             f"got {extracted!r} ({len(extracted)} chars)")


class HybridSearchLangFilterEndToEnd(unittest.TestCase):
    """Integration: hybrid_search with lang='english' (full name) must
    keep English books, drop French ones — exactly the path Stan's
    «английская литература» query takes."""

    def test_english_full_name_keeps_en_books(self):
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage

        lex_matches = [
            {"pg_id": "PG1", "snippet": "door ajar",
             "title": "T1", "author": "A1"},
            {"pg_id": "PG2", "snippet": "porte entrouverte",
             "title": "Bovary", "author": "Flaubert"},
        ]
        meta = {
            "PG1": {"language": "en", "title": "T1", "author": "A1"},
            "PG2": {"language": "fr", "title": "Bovary",
                     "author": "Flaubert"},
        }

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=2, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta.get(pid, default)
            return _T()

        with mock.patch("scripts.v2.tools.search.hybrid.v2_dispatch",
                         return_value=lex_result), \
             mock.patch("scripts.v2.tools.search.hybrid.dispatch_any",
                         return_value=ToolResult.fail(
                             tool="semantic_search",
                             err_type="internal",
                             message="off",
                         )), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            # The critical case: full-name «english» passed as lang
            r = hybrid_search(query="ajar", k=12, lang="english")

        self.assertTrue(r.ok)
        # English book kept, French dropped
        matches = r.data.get("matches", [])
        pgs = {m["pg_id"] for m in matches}
        self.assertEqual(pgs, {"PG1"},
                          "lang='english' must keep English book and drop "
                          "French; got {!r}".format(pgs))

    def test_iso_two_letter_still_works(self):
        """Backwards compat: lang='en' (already extracted) still works."""
        from scripts.v2.tools.search.hybrid import hybrid_search
        from scripts.v2._types import ToolResult, Coverage

        lex_matches = [{"pg_id": "PG1", "snippet": "x",
                         "title": "T", "author": "A"}]
        meta = {"PG1": {"language": "en", "title": "T", "author": "A"}}

        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": lex_matches},
            coverage=Coverage(books_matched=1, books_total=-1),
            query={"query": "ajar"},
        )

        def fake_meta_lookup_loader():
            class _T:
                def get(self, pid, default=None):
                    return meta.get(pid, default)
            return _T()

        with mock.patch("scripts.v2.tools.search.hybrid.v2_dispatch",
                         return_value=lex_result), \
             mock.patch("scripts.v2.tools.search.hybrid.dispatch_any",
                         return_value=ToolResult.fail(
                             tool="semantic_search",
                             err_type="internal",
                             message="off",
                         )), \
             mock.patch("scripts.v2.tools.search.lexical._title_lookup",
                         side_effect=fake_meta_lookup_loader):
            r = hybrid_search(query="ajar", k=12, lang="en")

        self.assertEqual(len(r.data.get("matches", [])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
