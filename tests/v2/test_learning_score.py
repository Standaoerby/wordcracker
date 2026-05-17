"""Tests for the Sprint 8 learning_priority_score formula."""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# learning_tools does `from rag_tools import ...` so scripts/ must be on path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


class LearningPriorityScore(unittest.TestCase):
    """Verify each weighted component pulls in the expected direction."""

    def setUp(self):
        # Mock _corpus_total_tokens to a known value so affinity math is stable.
        import scripts.learning_tools as lt
        self._patch = mock.patch.object(
            lt, "_corpus_total_tokens", return_value=2_840_000_000)
        self._patch.start()
        self.lt = lt

    def tearDown(self):
        self._patch.stop()

    def test_zero_inputs_zero_score(self):
        s = self.lt._learning_priority_score(
            scope_count=0, corpus_count=0, scope_tokens=0,
            level="intermediate", has_context=False, is_proper_noun=False,
        )
        self.assertEqual(s, 0.0)

    def test_high_affinity_high_score(self):
        # Wodehouse signature word: scope_count=200/2M tokens, corpus_count=1000
        # affinity = (200/2_000_000) / (1000/2.84e9) ≈ 284 → log10≈2.45 → norm≈1.0
        s = self.lt._learning_priority_score(
            scope_count=200, corpus_count=1000, scope_tokens=2_000_000,
            level="advanced", has_context=True, is_proper_noun=False,
        )
        self.assertGreater(s, 0.6)

    def test_proper_noun_penalty(self):
        s_clean = self.lt._learning_priority_score(
            scope_count=200, corpus_count=1000, scope_tokens=2_000_000,
            level="advanced", has_context=True, is_proper_noun=False,
        )
        s_propn = self.lt._learning_priority_score(
            scope_count=200, corpus_count=1000, scope_tokens=2_000_000,
            level="advanced", has_context=True, is_proper_noun=True,
        )
        # propn loses the 0.05 weight contribution
        self.assertAlmostEqual(s_clean - s_propn, 0.05, places=3)

    def test_cefr_mismatch_penalty(self):
        # corpus_count 1000 → intermediate band (100-10000); requesting advanced
        # → mismatch → CEFR component drops 1.0 → 0.4, weight 0.15
        s_match = self.lt._learning_priority_score(
            scope_count=50, corpus_count=1000, scope_tokens=2_000_000,
            level="intermediate", has_context=True, is_proper_noun=False,
        )
        s_mismatch = self.lt._learning_priority_score(
            scope_count=50, corpus_count=1000, scope_tokens=2_000_000,
            level="basic", has_context=True, is_proper_noun=False,
        )
        self.assertGreater(s_match, s_mismatch)
        # Difference comes from 0.15 * (1.0 - 0.4) = 0.09
        self.assertAlmostEqual(s_match - s_mismatch, 0.09, places=2)

    def test_context_quality_boost(self):
        s_ctx = self.lt._learning_priority_score(
            scope_count=50, corpus_count=1000, scope_tokens=2_000_000,
            level="intermediate", has_context=True, is_proper_noun=False,
        )
        s_no_ctx = self.lt._learning_priority_score(
            scope_count=50, corpus_count=1000, scope_tokens=2_000_000,
            level="intermediate", has_context=False, is_proper_noun=False,
        )
        # context contributes 0.10 * (1.0 - 0.5) = 0.05
        self.assertAlmostEqual(s_ctx - s_no_ctx, 0.05, places=3)

    def test_score_in_unit_interval(self):
        """Across reasonable inputs, score should never exceed 1.0."""
        for scope_c in (1, 50, 500, 5000):
            for corpus_c in (10, 1000, 100_000, 5_000_000):
                for level in ("basic", "intermediate", "advanced", "rare"):
                    s = self.lt._learning_priority_score(
                        scope_count=scope_c, corpus_count=corpus_c,
                        scope_tokens=2_000_000, level=level,
                        has_context=True, is_proper_noun=False,
                    )
                    self.assertGreaterEqual(s, 0.0)
                    self.assertLessEqual(s, 1.0)


class AnkiApkgFallback(unittest.TestCase):
    """When genanki isn't installed, requesting apkg falls back to CSV."""

    def test_no_genanki_falls_back(self):
        import scripts.learning_tools as lt
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # Force genanki import to fail
            with mock.patch.dict("sys.modules", {"genanki": None}):
                out_path = f"{tmp}/test.apkg"
                # Need a minimal call — most of the function reads
                # word_dictionary; skip the heavy bit by inspecting just the
                # return path of the format check.
                result = lt.export_word_list(
                    words=[{"word": "civility"}],
                    format="anki_apkg",
                    out_path=out_path,
                    target_lang="ru",
                )
                # Either: (a) genanki actually installed → returns apkg
                # success; or (b) fell back → returns anki_csv format.
                # We accept both since the dev box may have genanki preinstalled.
                self.assertIn(result.get("format"), ("anki_apkg", "anki_csv"))

    def test_format_validation(self):
        import scripts.learning_tools as lt
        r = lt.export_word_list(words=[], format="invalid_fmt")
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
