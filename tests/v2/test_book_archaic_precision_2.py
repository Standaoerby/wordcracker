"""2.7.36 — book_archaic_words precision #2 (RAG_TASK_book_archaic_precision_2).

Three precision fixes, all unit-tested without spaCy / raw_text / Ollama so they
run on any box (the recorded-golden re-record on SOW is a separate gate):

  * WP-B — the proper-noun NER gate no longer drops *seed* archaisms, so a word
    that collides with a one-book character name (`art` = Arthur Holmwood in
    Dracula) survives; only cache-sourced names (`galatz`) drop.
  * WP-C — `_book_propn_set` samples head/middle/tail windows instead of a
    single leading 400k slice, so a name confined to the final chapters is
    seen. The window math (`_propn_sample_windows`) and the versioned cache
    path (`_propn_cache_path`) are pure and directly testable.
  * WP-D — `migrate_archaic_cache.pass2_reenrich_violators` writes the
    re-enriched verdict back into the dict `main()` persists, so the final save
    no longer clobbers pass 2.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import learning_tools as lt


def _counts_file(pairs: dict[str, int]) -> str:
    fd, path = tempfile.mkstemp(suffix=".tsv")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for w, c in pairs.items():
            fh.write(f"{w}\t{c}\n")
    return path


class TestSeedSurvivesPropnCollision(unittest.TestCase):
    """WP-B — a seed archaism that surface-collides with a book character name
    survives the NER gate; only cache-sourced names drop."""

    def _run(self, counts, *, propn_set=frozenset(), cache=None):
        path = _counts_file(counts)
        try:
            with mock.patch.object(lt, "_counts_path",
                                   return_value=Path(path)), \
                 mock.patch.object(lt, "_load_word_dict",
                                   return_value=cache or {}), \
                 mock.patch.object(lt, "_book_propn_set",
                                   return_value=set(propn_set)):
                return lt.book_archaic_words("PG345", top=30)
        finally:
            os.unlink(path)

    def test_seed_art_survives_name_collision(self):
        # «Art» = Arthur Holmwood is NER-tagged PERSON → art ∈ propn_set, but
        # `art` is a curated seed archaism («thou art») → must survive. `galatz`
        # is a cache-only name → must still drop. dropped_propn counts the cache
        # name only, NOT the seed homograph (Q-B1).
        out = self._run(
            {"art": 25, "galatz": 18},
            propn_set={"art", "galatz"},
            cache={"galatz|ru": {"archaic": True, "proper_noun": False,
                                 "archaic_note": "old word"}},
        )
        words = [r["word"] for r in out["top"]]
        self.assertIn("art", words, "seed archaism must survive name collision")
        self.assertNotIn("galatz", words, "cache-only name must still drop")
        self.assertEqual(out["dropped_propn"], 1,
                         "only the cache name counts, not the seed homograph")
        art = next(r for r in out["top"] if r["word"] == "art")
        self.assertEqual(art["source"], "seed")
        self.assertIn("современное значение", art["note"],
                      "art keeps the homograph overcount caveat (WP-F)")

    def test_cache_name_still_drops_without_seed(self):
        # Regression guard: a pure cache name with no seed status drops exactly
        # as in 2.7.35 — WP-B narrows the gate, it does not disable it.
        out = self._run(
            {"nay": 30, "galatz": 18},
            propn_set={"galatz"},
            cache={"galatz|ru": {"archaic": True, "proper_noun": False}},
        )
        words = [r["word"] for r in out["top"]]
        self.assertIn("nay", words)
        self.assertNotIn("galatz", words)
        self.assertEqual(out["dropped_propn"], 1)


class TestPropnSampleWindows(unittest.TestCase):
    """WP-C — sampling windows cover the whole book, tail included."""

    def test_small_book_single_whole_window(self):
        # Fits the budget → one window spanning the entire text. The old code
        # would have truncated a 500k book at 400k.
        self.assertEqual(lt._propn_sample_windows(500_000), [(0, 500_000)])

    def test_boundary_is_whole_window(self):
        budget = lt._PROPN_N_WINDOWS * lt._PROPN_WINDOW_CHARS
        self.assertEqual(lt._propn_sample_windows(budget), [(0, budget)])

    def test_large_book_tail_covered(self):
        # Dracula-sized: `galatz` lives past the old 400k cut. The last window
        # must reach the end of the text.
        wins = lt._propn_sample_windows(900_000)
        self.assertTrue(wins, "expected sampling windows")
        self.assertEqual(wins[0][0], 0, "first window starts at 0")
        self.assertEqual(wins[-1][1], 900_000, "last window ends at text end")
        self.assertTrue(any(s <= 850_000 < e for s, e in wins),
                        "a late-book offset (galatz) must fall inside a window")

    def test_no_overlap_in_strided_branch(self):
        wins = lt._propn_sample_windows(900_000)
        for (s1, e1), (s2, e2) in zip(wins, wins[1:]):
            self.assertLessEqual(e1, s2, "strided windows must not overlap")

    def test_cost_bounded_for_huge_book(self):
        # A 3M-char tome samples no more than the budget — cost is independent
        # of book size.
        wins = lt._propn_sample_windows(3_000_000)
        sampled = sum(e - s for s, e in wins)
        self.assertLessEqual(
            sampled, lt._PROPN_N_WINDOWS * lt._PROPN_WINDOW_CHARS)

    def test_empty_text(self):
        self.assertEqual(lt._propn_sample_windows(0), [])


class TestPropnCachePath(unittest.TestCase):
    """WP-C — the per-book cache key is versioned so stale 400k caches are
    ignored rather than silently reused."""

    def test_versioned_filename(self):
        p = lt._propn_cache_path("pg345")
        self.assertEqual(p.name, f"PG345.{lt._PROPN_CACHE_VERSION}.json")
        self.assertEqual(p.name, "PG345.v2.json")
        self.assertEqual(p.parent, lt._BOOK_PROPN_DIR)


class TestPass2MergeBack(unittest.TestCase):
    """WP-D — pass 2 writes the re-enriched verdict back into the dict main()
    persists, so the final save doesn't clobber it."""

    def test_merge_back_persists_and_strips_internal_keys(self):
        from scripts import migrate_archaic_cache as migrate

        cache = {"galatz|ru": {"archaic": True, "proper_noun": False,
                               "archaic_note": "old word"}}
        corrected = {"archaic": False, "proper_noun": True, "archaic_note": "",
                     "lemma": "galatz", "_elapsed_s": 0.1}
        with mock.patch.object(migrate.lt, "enrich_word",
                               return_value=corrected) as m:
            touched = migrate.pass2_reenrich_violators(cache, apply=True)

        m.assert_called_once()
        self.assertEqual(touched, ["galatz|ru"])
        self.assertFalse(cache["galatz|ru"]["archaic"],
                         "merge-back must land in the dict main() saves")
        self.assertTrue(cache["galatz|ru"]["proper_noun"])
        self.assertNotIn("_elapsed_s", cache["galatz|ru"],
                         "enrich_word's internal `_`-keys must be stripped")
        self.assertEqual(len(cache), 1, "must correct in place, not duplicate")

    def test_dry_run_does_not_enrich_or_mutate(self):
        from scripts import migrate_archaic_cache as migrate

        cache = {"galatz|ru": {"archaic": True, "proper_noun": False}}
        with mock.patch.object(migrate.lt, "enrich_word") as m:
            touched = migrate.pass2_reenrich_violators(cache, apply=False)

        m.assert_not_called()
        self.assertTrue(cache["galatz|ru"]["archaic"],
                        "dry-run must not mutate the cache")
        self.assertEqual(touched, ["galatz|ru"])


if __name__ == "__main__":
    unittest.main()
