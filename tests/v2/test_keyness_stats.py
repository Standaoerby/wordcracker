"""Keyness statistics (RAG_TASK keyness_word_selection) — WP-F.

Validates the corpus-linguistics upgrade that replaces the naive
frequency-ratio "affinity" with log-likelihood G² (Dunning, significance)
+ LogRatio (Hardie, effect size). Corpus-free — runs on CI without the
SPGC mount.

Coverage:
  * Hand-worked 2x2 — G² and LogRatio match arithmetic a linguist can
    check by hand (the spec's hard requirement).
  * LogRatio +0.5 smoothing — a unique-to-target word gets a finite,
    bounded effect size instead of +inf.
  * Reference geometry — keyness() derives the corpus-minus-target
    reference (o2 = corpus_count - target_count) verified on live data.
  * NEGATIVE / regression (R2): keyness ranking differs from the ratio
    ranking, and a rare unique word no longer tops the list under G²
    (the exact bug this upgrade fixes). Asserted both at the pure-stats
    layer and end-to-end through v1 affinity_by_author on a synthetic CSV.
"""
from __future__ import annotations

import csv as _csv
import math
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.keyness import (  # noqa: E402
    AFFINITY_SCHEMA_VERSION,
    MIN_LL_DEFAULT,
    keyness,
    log_likelihood_g2,
    log_ratio,
    rel_freq_ratio,
    sort_key_for,
)


class HandWorked2x2(unittest.TestCase):
    """A linguist will check this arithmetic — keep it transparent."""

    def test_g2_matches_hand_calc(self):
        # target: o1=10 in n1=1000 ; reference: o2=100 in n2=1_000_000
        #   E1 = 1000 * 110 / 1_001_000 = 0.109890109...
        #   E2 = 1_000_000 * 110 / 1_001_000 = 109.890109...
        #   G2 = 2 * (10*ln(10/E1) + 100*ln(100/E2))
        #      = 2 * (10*ln(91.0)  + 100*ln(0.91))
        #      = 2 * (45.1085951   - 9.4310679)
        #      = 71.3550543
        g2 = log_likelihood_g2(10, 1000, 100, 1_000_000)
        self.assertAlmostEqual(g2, 71.3550543, places=4)

    def test_log_ratio_matches_hand_calc(self):
        # LogRatio = log2((10/1000) / (100/1_000_000))
        #          = log2(0.01 / 0.0001) = log2(100) = 6.6438562
        lr = log_ratio(10, 1000, 100, 1_000_000)
        self.assertAlmostEqual(lr, math.log2(100), places=6)
        self.assertAlmostEqual(lr, 6.6438562, places=4)

    def test_g2_symmetric_independent_recompute(self):
        # Independent re-derivation of the formula — guards against a
        # transcription slip in the implementation.
        o1, n1, o2, n2 = 37, 4200, 581, 9_500_000
        total_obs, total_n = o1 + o2, n1 + n2
        e1 = n1 * total_obs / total_n
        e2 = n2 * total_obs / total_n
        expect = 2 * (o1 * math.log(o1 / e1) + o2 * math.log(o2 / e2))
        self.assertAlmostEqual(log_likelihood_g2(o1, n1, o2, n2), expect,
                               places=9)


class LogRatioSmoothing(unittest.TestCase):
    """o2 == 0 (unique to target) must yield a finite, bounded LogRatio."""

    def test_zero_reference_is_finite(self):
        # LogRatio = log2((5/1000) / (0.5/1_000_000)) = log2(10000) = 13.2877
        lr = log_ratio(5, 1000, 0, 1_000_000)
        self.assertTrue(math.isfinite(lr))
        self.assertAlmostEqual(lr, math.log2(10000), places=6)

    def test_zero_reference_not_dumped_at_infinity(self):
        # The whole point: a unique word's effect size is large but bounded,
        # so it does not get an artificially infinite score that floats it
        # to the top (the legacy ratio's failure mode).
        lr = log_ratio(5, 1000, 0, 1_000_000)
        self.assertLess(lr, 100.0)

    def test_g2_of_unique_low_count_is_modest(self):
        # A 5x unique word in a realistically-sized target: G² is small
        # because the count is small — significance, not mere uniqueness.
        g2 = log_likelihood_g2(5, 500_000, 0, 2_800_000_000)
        self.assertLess(g2, 200.0)


class ReferenceGeometry(unittest.TestCase):
    """keyness() derives a corpus-minus-target reference (verified live)."""

    def test_corpus_minus_target_subtraction(self):
        # corpus_count INCLUDES the target's own books, so o2 = cc - ac.
        # keyness(10, 1000, cc=110, CT=1_001_000) ⇒ o2=100, n2=1_000_000,
        # i.e. identical to the hand-worked 2x2 above.
        k = keyness(10, 1000, 110, 1_001_000)
        self.assertAlmostEqual(k["g2"], 71.3550543, places=4)
        self.assertAlmostEqual(k["log_ratio"], math.log2(100), places=6)

    def test_overused_flag_direction(self):
        over = keyness(2000, 500_000, 10_000, 2_800_000_000)
        self.assertTrue(over["overused"])
        self.assertGreater(over["log_ratio"], 0)
        # A word the target UNDERuses: high corpus rate, low target rate.
        under = keyness(5, 500_000, 5_000_000, 2_800_000_000)
        self.assertFalse(under["overused"])
        self.assertLess(under["log_ratio"], 0)

    def test_rel_freq_preserves_legacy_affinity(self):
        # rel_freq must equal the old affinity formula exactly (continuity).
        k = keyness(2000, 500_000, 10_000, 2_800_000_000)
        legacy = (2000 / 500_000) / (10_000 / 2_800_000_000)
        self.assertAlmostEqual(k["rel_freq"], legacy, places=6)

    def test_rel_freq_none_when_absent_from_corpus(self):
        self.assertIsNone(rel_freq_ratio(5, 1000, 0, 1_000_000))
        self.assertIsNone(keyness(5, 1000, 0, 1_000_000)["rel_freq"])


class KeynessOutranksRatio(unittest.TestCase):
    """R2 NEGATIVE TEST — the core product fix.

    Compares a rare-but-high-ratio word against a frequent-and-truly-
    characteristic word. The legacy ratio ranks the rare word first
    (the bug); G² ranks the frequent characteristic word first (the fix).
    """

    # author = 500k tokens; whole corpus (incl. author) = 2.8B tokens.
    N1 = 500_000
    CT = 2_800_000_000

    def _k(self, word, ac, cc):
        k = keyness(ac, self.N1, cc, self.CT)
        return {"word": word, "author_count": ac, "corpus_count": cc, **k}

    def setUp(self):
        # rareish: 8x in author, 20x corpus-wide — huge ratio, tiny count.
        # char:    2000x in author, 10000x corpus-wide — the real signature.
        self.rows = [self._k("rareish", 8, 20), self._k("char", 2000, 10_000)]

    def test_ratio_ranks_rare_word_first_the_bug(self):
        ranked = sorted(self.rows, key=sort_key_for("freq"), reverse=True)
        self.assertEqual(ranked[0]["word"], "rareish")

    def test_keyness_ranks_characteristic_word_first_the_fix(self):
        ranked = sorted(self.rows, key=sort_key_for("keyness"), reverse=True)
        self.assertEqual(ranked[0]["word"], "char")
        # The rare word is NOT top-ranked under G² — old bug gone.
        self.assertNotEqual(ranked[0]["word"], "rareish")

    def test_rankings_actually_differ(self):
        by_freq = [r["word"] for r in
                   sorted(self.rows, key=sort_key_for("freq"), reverse=True)]
        by_key = [r["word"] for r in
                  sorted(self.rows, key=sort_key_for("keyness"), reverse=True)]
        self.assertNotEqual(by_freq, by_key)

    def test_logratio_sort_also_available(self):
        ranked = sorted(self.rows, key=sort_key_for("logratio"), reverse=True)
        self.assertEqual(len(ranked), 2)


class V1AffinityByAuthorRanking(unittest.TestCase):
    """End-to-end: v1 affinity_by_author honours sort_by on a synthetic CSV.

    Proves the engine wiring (not just the pure helper) ranks by keyness by
    default and by the legacy ratio under sort_by='freq'. Corpus-free — the
    CSV is hand-written with the v2 keyness schema; the subprocess path is
    never hit because the CSV is already current-schema.
    """

    N1 = 500_000
    CT = 2_800_000_000

    def _write_csv(self, path: Path, specs):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["word", "author_count", "corpus_count",
                        "rel_freq", "g2", "log_ratio"])
            for word, ac, cc in specs:
                k = keyness(ac, self.N1, cc, self.CT)
                w.writerow([word, ac, cc,
                            f"{k['rel_freq']:.6f}" if k["rel_freq"] else "",
                            f"{k['g2']:.6f}", f"{k['log_ratio']:.6f}"])

    def _run(self, sort_by):
        from scripts import rag_tools
        specs = [("rareish", 8, 20), ("char", 2000, 10_000)]
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._write_csv(dd / "x_affinity.csv", specs)
            with mock.patch.object(rag_tools, "DERIVED_DIR", dd), \
                 mock.patch.object(rag_tools, "_slug", return_value="x"), \
                 mock.patch.object(rag_tools, "_spacy_pos_tags",
                                   return_value={}):
                out = rag_tools.affinity_by_author(
                    "^X,", top=10, min_author_count=1,
                    sort_by=sort_by, min_ll=0.0)
        self.assertNotIn("error", out, out)
        return [r["word"] for r in out["top"]]

    def test_default_keyness_puts_characteristic_first(self):
        words = self._run("keyness")
        self.assertEqual(words[0], "char")

    def test_freq_sort_puts_rare_high_ratio_first(self):
        words = self._run("freq")
        self.assertEqual(words[0], "rareish")

    def test_rows_carry_keyness_and_compat_alias(self):
        from scripts import rag_tools
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._write_csv(dd / "x_affinity.csv", [("char", 2000, 10_000)])
            with mock.patch.object(rag_tools, "DERIVED_DIR", dd), \
                 mock.patch.object(rag_tools, "_slug", return_value="x"), \
                 mock.patch.object(rag_tools, "_spacy_pos_tags",
                                   return_value={}):
                out = rag_tools.affinity_by_author(
                    "^X,", top=10, min_author_count=1, min_ll=0.0)
        row = out["top"][0]
        for key in ("g2", "log_ratio", "rel_freq", "affinity"):
            self.assertIn(key, row)
        # affinity is the back-compat alias = rel_freq value.
        self.assertEqual(row["affinity"], row["rel_freq"])


class StopwordExclusion(unittest.TestCase):
    """exclude_stopwords (default True) drops closed-class function words so
    the keyness top is distinctive CONTENT vocabulary, not her/she/very —
    while genuine content words an author overuses (sister) survive."""

    N1 = 500_000
    CT = 2_800_000_000

    def _csv(self, path: Path, specs):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = _csv.writer(fh)
            w.writerow(["word", "author_count", "corpus_count",
                        "rel_freq", "g2", "log_ratio"])
            for word, ac, cc in specs:
                k = keyness(ac, self.N1, cc, self.CT)
                w.writerow([word, ac, cc,
                            f"{k['rel_freq']:.6f}" if k["rel_freq"] else "",
                            f"{k['g2']:.6f}", f"{k['log_ratio']:.6f}"])

    def _run(self, exclude_stopwords):
        from scripts import rag_tools
        specs = [
            ("her", 19534, 14285151),    # function word, huge G²
            ("she", 14611, 11875554),    # function word
            ("very", 3000, 900000),      # function word (SIGNATURE_STOPWORDS only)
            ("sister", 1500, 120000),    # CONTENT word the author overuses
            ("curricle", 60, 800),       # CONTENT signature word
        ]
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._csv(dd / "x_affinity.csv", specs)
            with mock.patch.object(rag_tools, "DERIVED_DIR", dd), \
                 mock.patch.object(rag_tools, "_slug", return_value="x"), \
                 mock.patch.object(rag_tools, "_spacy_pos_tags",
                                   return_value={}):
                out = rag_tools.affinity_by_author(
                    "^X,", top=10, min_author_count=1, min_ll=0.0,
                    exclude_stopwords=exclude_stopwords)
        return [r["word"] for r in out["top"]]

    def test_default_excludes_function_words_keeps_content(self):
        words = self._run(exclude_stopwords=True)
        for fw in ("her", "she", "very"):
            self.assertNotIn(fw, words, f"function word {fw!r} must be excluded")
        # genuine content the author overuses survives — NOT a blanket nuke
        self.assertIn("sister", words)
        self.assertIn("curricle", words)

    def test_off_keeps_function_words_raw_keyness(self):
        words = self._run(exclude_stopwords=False)
        # raw keyness (AntConc-style): function words are legitimate keywords
        self.assertIn("her", words)
        self.assertIn("she", words)
        # and G² ranks them at the top (highest counts → highest G²)
        self.assertEqual(words[0], "her")

    def test_signature_stopwords_superset_of_stopwords(self):
        from scripts.rag_tools import STOPWORDS, SIGNATURE_STOPWORDS
        self.assertTrue(STOPWORDS.issubset(SIGNATURE_STOPWORDS))
        # covers the gaps the small list missed
        for fw in ("very", "am", "herself", "myself", "thou", "unto"):
            self.assertIn(fw, SIGNATURE_STOPWORDS)
        # but NOT content words an author can legitimately overuse
        for content in ("sister", "money", "sea", "love", "miss"):
            self.assertNotIn(content, SIGNATURE_STOPWORDS)


class SchemaConstants(unittest.TestCase):

    def test_schema_version_bumped(self):
        # The CSV-cache invalidation gate keys off this — must be >= 2.
        self.assertGreaterEqual(AFFINITY_SCHEMA_VERSION, 2)

    def test_default_threshold_is_p0001(self):
        self.assertAlmostEqual(MIN_LL_DEFAULT, 15.13, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
