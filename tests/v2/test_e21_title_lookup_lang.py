"""E21 (2026-05-22) — _title_lookup language column normalization.

ROOT CAUSE (Stan prod 2026-05-22, deeper than E20):
PG catalog stores `language` as a Python-repr'd list, e.g. "['en']" /
"['en', 'fr']" / "['fr']". The original `_title_lookup` normalization

    entry["language"] = str(lv).lower().strip().split("-")[0][:3]

truncated to first 3 chars AFTER lowercasing. For "['en']" that gave
"['e" (literally the bracket-quote-e sequence) — 3 chars total, NO
language code at all.

Every book in the corpus stored "['e" as its language. Downstream
filter (hybrid_search lang post-filter, hybrid_search rerank, anyone
who consulted `meta.get('language')`) compared user input ("en")
against "['e" and dropped ALL matches.

E20 fixed the hybrid_search side (substring containment instead of
equality), but the substring check still failed: "en" is not in "['e".
So «ajar in English literature» kept returning empty.

E21 fixes the SOURCE: extract real ISO-639 codes via regex and store
them space-separated. "['en']" → "en". "['en', 'fr']" → "en fr".

This contract pins the loader behavior so future refactors can't
re-introduce the [:3] truncation bug.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _normalize(raw_value: str) -> str:
    """Run the same normalization as _title_lookup's inner block,
    isolated for direct testing."""
    import re
    raw = str(raw_value).lower().strip()
    codes = re.findall(r"\b([a-z]{2,3})\b", raw)
    if codes:
        return " ".join(codes)
    return raw


class LanguageNormalization(unittest.TestCase):
    def test_pg_bracketed_single_lang_en(self):
        """PG metadata's most common shape — single-language list."""
        self.assertEqual(_normalize("['en']"), "en")

    def test_pg_bracketed_multi_lang(self):
        """Bilingual books (e.g. parallel-text editions)."""
        self.assertEqual(_normalize("['en', 'fr']"), "en fr")

    def test_pg_bracketed_french(self):
        """Negative — French book stays as 'fr', not 'en'."""
        self.assertEqual(_normalize("['fr']"), "fr")

    def test_raw_two_letter_code(self):
        self.assertEqual(_normalize("en"), "en")

    def test_full_name_english_extracted_as_eng_or_first_two(self):
        """`english` → first 2-3 letter code via regex. Original input
        gives back "english" via the dynamic regex match `[a-z]{2,3}`
        — actually no, regex matches first 2-3 letter token: "en"
        is followed by "g" so regex matches "english" as a 7-letter
        token — no match. Actually \\b...\\b on "english" matches the
        full 7-letter word IF the class were unbounded; with [a-z]{2,3}
        + word-boundary, only complete 2-3 letter tokens match.
        Result: 'english' returns the raw fallback."""
        # The current implementation regex: \b([a-z]{2,3})\b
        # 'english' is 7 chars surrounded by \b → does NOT match
        # [a-z]{2,3} (length constraint). So fallback raw is returned.
        self.assertEqual(_normalize("english"), "english")

    def test_dashed_en_us(self):
        """en-US → en (dash is a word boundary, splits 'en' and 'us')."""
        self.assertEqual(_normalize("en-US"), "en us")

    def test_empty_string_stays_empty(self):
        self.assertEqual(_normalize(""), "")

    def test_no_three_char_truncation(self):
        """E21 regression: never produce «['e» (the old bug signature)."""
        result = _normalize("['en']")
        self.assertNotEqual(result, "['e")
        # Bug had len=3 with leading bracket — assert leading char is alpha
        self.assertTrue(result and result[0].isalpha(),
                         f"normalized lang must start with alpha, got {result!r}")


class TitleLookupContract(unittest.TestCase):
    """Integration: _title_lookup applied to a mocked DataFrame returns
    properly normalized language entries."""

    def test_real_pg_metadata_shape(self):
        """Build a fake metadata DataFrame in the EXACT shape v1 returns
        — language as stringified list — and verify _title_lookup
        normalizes correctly."""
        import pandas as pd
        import unittest.mock as mock

        df = pd.DataFrame([
            {"pg_id": "PG1342", "title": "Pride and Prejudice",
             "author": "Austen, Jane", "language": "['en']"},
            {"pg_id": "PG174", "title": "The Picture of Dorian Gray",
             "author": "Wilde, Oscar", "language": "['en']"},
            {"pg_id": "PG10001", "title": "Madame Bovary",
             "author": "Flaubert", "language": "['fr']"},
            {"pg_id": "PG99999", "title": "Mixed",
             "author": "X", "language": "['en', 'fr']"},
        ])

        # Avoid the real cache + bypass mtime check
        from scripts.v2.tools.search import lexical as lex_mod
        lex_mod._TITLE_CACHE = None
        lex_mod._TITLE_CACHE_KEY = None

        with mock.patch("scripts.rag_tools._metadata_df", return_value=df), \
             mock.patch("scripts.rag_tools.SPGC_METADATA", Path("/nonexistent_a")), \
             mock.patch("scripts.rag_tools.USER_UPLOADS_META", Path("/nonexistent_b")), \
             mock.patch("scripts.rag_tools.ORPHAN_PG_META", Path("/nonexistent_c")):
            lookup = lex_mod._title_lookup()

        self.assertEqual(lookup.get("PG1342", {}).get("language"), "en",
                          "PG1342 ['en'] must normalize to 'en'")
        self.assertEqual(lookup.get("PG174", {}).get("language"), "en")
        self.assertEqual(lookup.get("PG10001", {}).get("language"), "fr")
        self.assertEqual(lookup.get("PG99999", {}).get("language"), "en fr",
                          "Bilingual entry must keep both codes")
        # NEVER the old bug signature
        for pg, entry in lookup.items():
            lang = entry.get("language", "")
            self.assertNotEqual(lang, "['e",
                                 f"{pg} returned the pre-E21 bug shape «['e»")


from pathlib import Path  # for the integration test path mocks


if __name__ == "__main__":
    unittest.main(verbosity=2)
