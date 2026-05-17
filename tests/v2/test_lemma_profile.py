"""Tests for the LemmaProfile lightweight builder."""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class LemmaProfileBuild(unittest.TestCase):
    def setUp(self):
        # Fake corpus_counts.csv with known frequencies.
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp.name) / "corpus_counts.csv"
        with open(self.csv_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["word", "count"])
            w.writerow(["the", 50_000_000])
            w.writerow(["civility", 13060])
            w.writerow(["niblick", 215])
            w.writerow(["onomatopoeia", 42])
            w.writerow(["xyzlemma", 3])
        os.environ["WC_CORPUS_COUNTS"] = str(self.csv_path)

        # Profile store on temp DB
        self.db_path = Path(self.tmp.name) / "profiles.sqlite"
        os.environ["WC_V2_PROFILES_DB"] = str(self.db_path)

        from scripts.v2 import corpus_version
        from scripts.v2.profiles import store as store_mod
        from scripts.v2.profiles import lemma as lemma_mod
        corpus_version._reset()
        # Override module-level paths without reloading (reload poisons other
        # test files that import these modules).
        self._orig_db = store_mod.DB_PATH
        self._orig_conn = store_mod._conn
        self._orig_csv = lemma_mod.CORPUS_COUNTS
        self._orig_cache = lemma_mod._corpus_counts_cache
        store_mod.DB_PATH = self.db_path
        store_mod._conn = None
        lemma_mod.CORPUS_COUNTS = self.csv_path
        lemma_mod._corpus_counts_cache = None
        self.lemma_mod = lemma_mod
        self.store_mod = store_mod

    def tearDown(self):
        if self.store_mod._conn is not None:
            self.store_mod._conn.close()
        self.store_mod._conn = self._orig_conn
        self.store_mod.DB_PATH = self._orig_db
        self.lemma_mod.CORPUS_COUNTS = self._orig_csv
        self.lemma_mod._corpus_counts_cache = self._orig_cache
        self.tmp.cleanup()
        os.environ.pop("WC_CORPUS_COUNTS", None)
        os.environ.pop("WC_V2_PROFILES_DB", None)

    def test_difficulty_basic_for_the(self):
        p = self.lemma_mod.get_or_build("the")
        self.assertEqual(p["difficulty"], "basic")
        self.assertLess(p["rarity"], 0.1)

    def test_intermediate_for_civility(self):
        p = self.lemma_mod.get_or_build("civility")
        self.assertEqual(p["difficulty"], "intermediate")

    def test_advanced_for_niblick(self):
        p = self.lemma_mod.get_or_build("niblick")
        self.assertEqual(p["difficulty"], "advanced")

    def test_rare_for_onomatopoeia(self):
        p = self.lemma_mod.get_or_build("onomatopoeia")
        self.assertEqual(p["difficulty"], "rare")

    def test_ultra_rare(self):
        p = self.lemma_mod.get_or_build("xyzlemma")
        self.assertEqual(p["difficulty"], "ultra_rare")

    def test_zero_count_returns_none(self):
        self.assertIsNone(self.lemma_mod.get_or_build("nonexistentword"))

    def test_empty_input(self):
        self.assertIsNone(self.lemma_mod.get_or_build(""))
        self.assertIsNone(self.lemma_mod.get_or_build("   "))

    def test_lowercase_normalized(self):
        p1 = self.lemma_mod.get_or_build("Civility")
        p2 = self.lemma_mod.get_or_build("civility")
        self.assertEqual(p1["global_count"], p2["global_count"])

    def test_cache_hit_on_second_call(self):
        p1 = self.lemma_mod.get_or_build("niblick")
        p2 = self.lemma_mod.get_or_build("niblick")
        self.assertEqual(p1["global_count"], p2["global_count"])
        # second call should have _cached_at metadata from store
        self.assertIn("_cached_at", p2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
