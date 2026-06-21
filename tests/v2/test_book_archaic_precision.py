"""2.7.35 — book_archaic_words precision sprint (RAG_TASK_book_archaic_precision).

Covers WP-A (seed prune), WP-B (proper-noun gate), WP-F (`art` homograph
disclosure) and WP-D (honest coverage caveat in the view). No corpus files
needed — the counts file and the NER/cache lookups are mocked, so this runs
on any box (the recorded golden fixture re-record on SOW is a separate gate).
"""
import os
import tempfile
import unittest
from unittest import mock

from scripts import learning_tools as lt


class TestSeedPrune(unittest.TestCase):
    """WP-A / Q2 — the seed reads as a list of archaisms from the first row."""

    REMOVED = ["amongst", "amidst", "ought", "albeit", "hence", "ado",
               "bespoke", "fortnight", "clad", "smitten", "wrought", "aye"]
    KEPT = ["ye", "nay", "ere", "whither", "whence", "thence", "thither",
            "bade", "aught", "anent", "mayhap", "hath", "doth", "art",
            "behold", "tarry", "oft", "alas"]

    def test_removed_forms_gone(self):
        for w in self.REMOVED:
            self.assertNotIn(w, lt._KNOWN_ARCHAISMS,
                             f"{w!r} should have been pruned from the seed")

    def test_kept_forms_present(self):
        for w in self.KEPT:
            self.assertIn(w, lt._KNOWN_ARCHAISMS,
                          f"{w!r} is a defensible archaism and must stay")


def _counts_file(pairs: dict[str, int]) -> str:
    fd, path = tempfile.mkstemp(suffix=".tsv")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for w, c in pairs.items():
            fh.write(f"{w}\t{c}\n")
    return path


class TestProperNounGate(unittest.TestCase):
    """WP-B — names never appear as archaisms."""

    def _run(self, counts, *, propn_set=frozenset(), cache=None):
        path = _counts_file(counts)
        try:
            with mock.patch.object(lt, "_counts_path",
                                   return_value=__import__("pathlib").Path(path)), \
                 mock.patch.object(lt, "_load_word_dict",
                                   return_value=cache or {}), \
                 mock.patch.object(lt, "_book_propn_set",
                                   return_value=set(propn_set)):
                return lt.book_archaic_words("PG345", top=30)
        finally:
            os.unlink(path)

    def test_ner_name_dropped(self):
        # `nay` is a real seed archaism; `galatz` is a NER place name that
        # leaked into the archaic cache (proper_noun=false there).
        out = self._run(
            {"nay": 30, "galatz": 18},
            propn_set={"galatz"},
            cache={"galatz|ru": {"archaic": True, "proper_noun": False,
                                 "archaic_note": "old word"}},
        )
        words = [r["word"] for r in out["top"]]
        self.assertIn("nay", words)
        self.assertNotIn("galatz", words, "NER place name must be dropped")
        self.assertEqual(out["dropped_propn"], 1)

    def test_cache_propn_flag_dropped(self):
        # When the LLM correctly tagged proper_noun=True, the cache flag alone
        # (no NER set) is enough to drop it.
        out = self._run(
            {"nay": 30, "lucy": 40},
            propn_set=frozenset(),
            cache={"lucy|ru": {"archaic": True, "proper_noun": True}},
        )
        words = [r["word"] for r in out["top"]]
        self.assertIn("nay", words)
        self.assertNotIn("lucy", words)
        self.assertEqual(out["dropped_propn"], 1)

    def test_real_archaisms_survive_empty_ner(self):
        # No raw text → empty NER set → gate is a no-op, nothing lost.
        out = self._run({"ye": 31, "nay": 30, "ere": 8}, propn_set=frozenset())
        words = {r["word"] for r in out["top"]}
        self.assertEqual(words, {"ye", "nay", "ere"})
        self.assertEqual(out["dropped_propn"], 0)

    def test_art_homograph_note(self):
        out = self._run({"art": 25})
        art = next(r for r in out["top"] if r["word"] == "art")
        self.assertIn("современное значение", art["note"],
                      "art must disclose the homograph overcount")


class TestCoverageCaveat(unittest.TestCase):
    """WP-D — view carries an honest coverage caption (not «N слов» = text)."""

    def test_view_caveat_present(self):
        from scripts.v2.tools.books.readability import book_archaic_words

        v1_raw = {
            "id": "PG345",
            "checked_book_vocab": 3993,
            "seed_or_cache_hits": 21,
            "dropped_propn": 1,
            "top": [{"word": "ye", "book_count": 31, "source": "seed",
                     "note": ""}],
            "_elapsed_s": 0.1,
        }
        with mock.patch("scripts.learning_tools.book_archaic_words",
                        return_value=v1_raw):
            r = book_archaic_words(pg_id="PG345")
        self.assertTrue(r.ok)
        caveats = " ".join(r.view.caveats)
        self.assertIn("3993", caveats)
        self.assertIn("словоформ", caveats)
        self.assertIn("1", caveats)  # dropped_propn surfaced


if __name__ == "__main__":
    unittest.main()
