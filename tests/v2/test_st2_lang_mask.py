"""S-T2 Group B — `_lang_codes` / `_lang_mask` normalizer.

ROOT CAUSE: 9 sites in rag_tools.py filtered language with
    df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
which only matches the Python-list-repr shape ['en']. It silently DROPPED
any book whose `language` was plain `en`, ISO-639-2 `['eng']` (the admin
upload corruption — see Group C), multi-language, or oddly quoted.

FIX: `_lang_codes(raw)` normalizes any shape to a set of ISO-639-1 codes
(639-2/B mapped down, garbage dropped); `_lang_mask(df, lang)` keeps rows
whose code set contains the normalized requested lang.

E20-style matrix: raw / bracketed / multi-lang / full(639-2) / garbage /
empty — each asserted, plus a direct contrast against the old brittle
behavior to prove the fix admits previously-dropped books.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.rag_tools import _lang_codes, _lang_mask


class TestLangCodes(unittest.TestCase):
    def test_e20_matrix(self):
        cases = {
            "en": {"en"},                       # raw
            "['en']": {"en"},                   # bracketed list-repr
            "['en', 'fr']": {"en", "fr"},       # multi-lang
            "['eng']": {"en"},                   # full ISO-639-2/B
            "eng": {"en"},                       # bare 639-2
            "['en', 'eng']": {"en"},             # dedup after 639-2 map
            '["de"]': {"de"},                    # double-quoted
            "": set(),                           # empty
            "nan": set(),                        # pandas NaN repr
            "none": set(),
            "['zzz']": set(),                    # unknown 3-letter → dropped
            "garbage!!": set(),                  # garbage → dropped
        }
        for raw, expected in cases.items():
            self.assertEqual(_lang_codes(raw), expected, f"_lang_codes({raw!r})")

    def test_none_and_nonstring(self):
        self.assertEqual(_lang_codes(None), set())
        self.assertEqual(_lang_codes(float("nan")), set())


class TestLangMask(unittest.TestCase):
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame({"language": [
            "['en']",        # 0 bracketed en
            "en",            # 1 plain en  (OLD filter WRONGLY dropped)
            "['eng']",       # 2 639-2 en  (OLD filter WRONGLY dropped)
            "['fr']",        # 3 fr only
            "['en', 'fr']",  # 4 multi incl en
            "",              # 5 empty
            "nan",           # 6 nan
            "garbage",       # 7 garbage
        ]})

    def test_mask_en_includes_all_english_shapes(self):
        df = self._df()
        mask = _lang_mask(df, "en")
        self.assertEqual(list(df.index[mask]), [0, 1, 2, 4])

    def test_mask_matches_639_2_request(self):
        # Requesting 'eng' normalizes to 'en' and matches the same rows.
        df = self._df()
        self.assertEqual(list(self._df().index[_lang_mask(df, "eng")]), [0, 1, 2, 4])

    def test_mask_fr(self):
        df = self._df()
        self.assertEqual(list(df.index[_lang_mask(df, "fr")]), [3, 4])

    def test_empty_lang_disables_filter(self):
        df = self._df()
        self.assertTrue(_lang_mask(df, None).all())
        self.assertTrue(_lang_mask(df, "").all())

    def test_fix_admits_rows_old_brittle_filter_dropped(self):
        """Direct contrast: the old str.contains("'en'") matched only the
        list-repr rows (0, 4); the fix additionally admits plain `en` (1)
        and 639-2 `['eng']` (2)."""
        df = self._df()
        old = df["language"].fillna("").str.contains("'en'", regex=False)
        new = _lang_mask(df, "en")
        self.assertEqual(list(df.index[old]), [0, 4])           # brittle
        self.assertEqual(list(df.index[new]), [0, 1, 2, 4])     # fixed
        # The fix is a strict superset here (no previously-kept row dropped).
        self.assertTrue((new | old).equals(new))


if __name__ == "__main__":
    unittest.main()
