"""S-T2 Group C — admin EPUB language hygiene + _clean_country ISO table.

Three concerns:
  * _clean_country: name/alias → ISO-3166-1 (no more .upper()[:2] that made
    "britain"->"BR" (Brazil) and dropped UK queries).
  * admin _normalize_epub_lang + _extract_epub_metadata: 639-2/B → 639-1,
    reject unknown, and NO ['en'] default when the EPUB has no DC language
    tag (a Russian EPUB without a tag must not become English).
  * migrate_user_meta_lang.normalize_lang_field + migrate(): repair the
    already-corrupted CSV (['eng'] -> ['en']).
"""
from __future__ import annotations

import csv
import sys
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# admin_server imports the stdlib `cgi` module (used only at request time,
# ~line 1487). `cgi` was removed in Python 3.13; CI/prod run 3.11 where it
# still exists. Stub it so these tests can import admin_server on any
# interpreter — setdefault leaves the real module untouched on 3.11.
sys.modules.setdefault("cgi", types.ModuleType("cgi"))


class TestCleanCountry(unittest.TestCase):
    def test_aliases_to_iso(self):
        from scripts.v2.planner.llm_intent import _clean_country
        cases = {
            "england": "GB", "Britain": "GB", "uk": "GB", "U.K.": "GB",
            "Great Britain": "GB", "scotland": "GB",
            "russia": "RU", "Russian Federation": "RU",
            "america": "US", "USA": "US", "germany": "DE",
        }
        for raw, expected in cases.items():
            self.assertEqual(_clean_country(raw), expected, f"_clean_country({raw!r})")

    def test_valid_two_letter_passthrough(self):
        from scripts.v2.planner.llm_intent import _clean_country
        self.assertEqual(_clean_country("GB"), "GB")
        self.assertEqual(_clean_country("fr"), "FR")

    def test_unknown_rejected_not_truncated(self):
        from scripts.v2.planner.llm_intent import _clean_country
        # Old bug: "britain"->"BR"(Brazil), "england"->"EN", "uk"->"UK".
        self.assertNotEqual(_clean_country("britain"), "BR")
        self.assertIsNone(_clean_country("atlantis"))
        self.assertIsNone(_clean_country("zz"))
        self.assertIsNone(_clean_country(None))


class TestEpubLang(unittest.TestCase):
    def test_normalize_epub_lang(self):
        from scripts.admin_server import _normalize_epub_lang
        cases = {
            "en": "en", "EN": "en", "en-US": "en", "en_us": "en",
            "eng": "en", "rus": "ru", "fre": "fr", "fra": "fr",
            "xx": "xx",            # any 2-letter code passes through (B/C-consistent)
            "": "", "zzz": "", "garbage": "",   # unknown 3+-letter / garbage rejected
        }
        for raw, expected in cases.items():
            self.assertEqual(_normalize_epub_lang(raw), expected, f"({raw!r})")

    def _patched_ebooklib(self, metadata: dict):
        """Inject a fake ebooklib whose book.get_metadata(ns, name) returns
        metadata[name] (list of (value, attrs) tuples)."""
        book = mock.Mock()
        book.get_metadata.side_effect = lambda ns, name: metadata.get(name, [])
        epub_mod = types.ModuleType("ebooklib.epub")
        epub_mod.read_epub = lambda *a, **k: book
        eb = types.ModuleType("ebooklib")
        eb.epub = epub_mod
        return mock.patch.dict(sys.modules, {"ebooklib": eb,
                                             "ebooklib.epub": epub_mod})

    def test_no_dc_tag_does_not_default_to_english(self):
        """A Russian EPUB with no <dc:language> must NOT become ['en']."""
        from scripts.admin_server import _extract_epub_metadata
        meta = {"title": [("Война и мир", {})]}  # no 'language' key
        with self._patched_ebooklib(meta):
            out = _extract_epub_metadata(Path("ru_book.epub"))
        self.assertEqual(out["language"], "")

    def test_dc_639_2_mapped_down(self):
        from scripts.admin_server import _extract_epub_metadata
        with self._patched_ebooklib({"language": [("eng", {})]}):
            self.assertEqual(
                _extract_epub_metadata(Path("x.epub"))["language"], "['en']")
        with self._patched_ebooklib({"language": [("rus", {})]}):
            self.assertEqual(
                _extract_epub_metadata(Path("x.epub"))["language"], "['ru']")

    def test_dc_unknown_rejected(self):
        from scripts.admin_server import _extract_epub_metadata
        with self._patched_ebooklib({"language": [("zzz", {})]}):
            self.assertEqual(
                _extract_epub_metadata(Path("x.epub"))["language"], "")


class TestMigration(unittest.TestCase):
    def test_normalize_lang_field(self):
        from scripts.migrations.migrate_user_meta_lang import normalize_lang_field
        cases = {
            "['eng']": "['en']",
            "['en', 'fre']": "['en', 'fr']",
            "['en', 'eng']": "['en']",        # dedup after map
            "['en']": "['en']",               # already good
            "['zzz']": "",                    # unknown dropped
            "": "", "nan": "",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_lang_field(raw), expected, f"({raw!r})")

    def test_migrate_dry_run_then_apply(self):
        from scripts.migrations.migrate_user_meta_lang import migrate

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "user_uploads_metadata.csv"
            with open(p, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=["id", "title", "language"])
                w.writeheader()
                w.writerow({"id": "U1", "title": "A", "language": "['eng']"})
                w.writerow({"id": "U2", "title": "B", "language": "['en']"})
                w.writerow({"id": "U3", "title": "C", "language": "['en', 'fre']"})

            dry = migrate(p, apply=False)
            self.assertEqual(dry["changed"], 2)        # U1, U3
            self.assertFalse(dry["applied"])
            # File untouched on dry-run.
            with open(p, encoding="utf-8") as fh:
                self.assertIn("['eng']", fh.read())

            applied = migrate(p, apply=True)
            self.assertEqual(applied["changed"], 2)
            self.assertTrue(applied["applied"])
            self.assertTrue(p.with_suffix(".csv.bak").exists())
            with open(p, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            langs = {r["id"]: r["language"] for r in rows}
            self.assertEqual(langs, {"U1": "['en']", "U2": "['en']",
                                     "U3": "['en', 'fr']"})

    def test_migrate_missing_file(self):
        from scripts.migrations.migrate_user_meta_lang import migrate
        self.assertEqual(migrate(Path("/no/such.csv"))["status"], "missing")


if __name__ == "__main__":
    unittest.main()
