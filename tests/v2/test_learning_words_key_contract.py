"""B-R14-7 ROOT CAUSE FIX — v1 ↔ v2 key contract for learning_words.

Stage 3 prod 2026-05-21: every learning_words query returned «0 слов»
regardless of book/level. R17 acceptance flagged Q39/Q43/Q44/Q45 as ⛔.
Root cause: v1 `scripts/learning_tools.py::learning_words()` returns
`{"results": rows, ...}` (line 564), but v2 wrapper read
`raw.get("words")` — always None — empty rows — `looks_broken=True` —
LEARNING_WORDS view with `data_validity=BROKEN` shown to user.

The reason unit-tests didn't catch it:
  * `test_router.py:97` mock: `m.learning_words = lambda **kw:
    {"words": [...], "n_books": 1}` — uses WRONG key (matches wrapper)
  * Live behavioural golden `test_golden_v5.py::test_learning_words_
    b2_pride_and_prejudice_nonempty` IS gated behind
    `@unittest.skipUnless(_LIVE)` so never runs in CI

This file locks in the key contract — both keys are accepted on
read (results = canonical v1 shape, words = test-mock back-compat).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class LearningWordsKeyContract(unittest.TestCase):
    """v2 learning_words wrapper must read v1's `results` key correctly."""

    def setUp(self):
        # Save + reset registry so re-importing the tool re-registers.
        from scripts.v2.tool_registry import REGISTRY
        self._snap = dict(REGISTRY)

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear()
        REGISTRY.update(self._snap)

    def _call_wrapper(self, fake_v1_return):
        """Invoke the v2 learning_words wrapper with v1 stubbed."""
        from scripts.v2.tools.learning import learning_words as lw_module
        with mock.patch(
            "scripts.learning_tools.learning_words",
            return_value=fake_v1_return,
        ):
            return lw_module.learning_words(
                scope={"book": "PG1342"}, level="intermediate",
                top=10, lemmatize=False,
            )

    def test_v1_results_key_is_read_correctly(self):
        """B-R14-7 ROOT CAUSE: v1 returns under 'results'. Wrapper must
        read it. Previously it read 'words' → always empty → broken view."""
        fake = {
            "scope": "book:PG1342",
            "level": "intermediate",
            "scope_tokens": 119035,
            "results": [
                {"word": "civility", "lemma": "civility", "pos": "NOUN",
                 "scope_count": 12, "corpus_count": 580, "affinity": 5.7},
                {"word": "amiable", "lemma": "amiable", "pos": "ADJ",
                 "scope_count": 8, "corpus_count": 410, "affinity": 5.2},
            ],
            "top_requested": 10, "top_returned": 2,
        }
        result = self._call_wrapper(fake)
        self.assertTrue(result.ok)
        # The wrapper should have surfaced both words from v1's "results".
        rows = result.data.get("results") or result.data.get("words") or []
        self.assertEqual(len(rows), 2,
                          "B-R14-7 regression: v1 'results' key not read")
        # Validity should NOT be broken when rows are present.
        from scripts.v2.view_types import DataValidity
        self.assertEqual(result.data_validity, DataValidity.OK)
        # View should be present and non-broken.
        self.assertIsNotNone(result.view)

    def test_legacy_words_key_still_accepted(self):
        """Backwards-compat: if any legacy caller passes through under
        'words' key, the wrapper must still process correctly. This
        keeps test_router.py mock + any external consumer working."""
        fake = {
            "scope": "book:PG1342",
            "level": "intermediate",
            "words": [
                {"word": "civility", "lemma": "civility", "pos": "NOUN",
                 "scope_count": 12, "corpus_count": 580, "affinity": 5.7},
            ],
        }
        result = self._call_wrapper(fake)
        self.assertTrue(result.ok)
        rows = result.data.get("results") or result.data.get("words") or []
        self.assertEqual(len(rows), 1)

    def test_empty_results_triggers_broken_validity(self):
        """Counter-test: when v1 genuinely returns empty results at an
        intermediate level, data_validity should be BROKEN (B-R14-7
        gate). This locks in the v5 broken-state contract."""
        fake = {
            "scope": "book:PG1342",
            "level": "intermediate",
            "results": [],  # genuinely empty
            "top_requested": 30, "top_returned": 0,
        }
        result = self._call_wrapper(fake)
        self.assertTrue(result.ok)  # tool succeeded, just empty
        from scripts.v2.view_types import DataValidity
        self.assertEqual(
            result.data_validity, DataValidity.BROKEN,
            "B-R14-7: empty learning_words at intermediate must mark BROKEN",
        )
        # View carries the broken state
        self.assertIsNotNone(result.view)
        self.assertIsNotNone(result.view.empty_state)

    def test_v1_results_key_filters_apply(self):
        """Literary location blacklist + surname filter run on the
        rows read from v1's 'results' key (regression: previously
        these filters never fired because rows was always empty)."""
        fake = {
            "scope": "book:PG1342",
            "level": "intermediate",
            "results": [
                {"word": "lambton", "lemma": "lambton", "pos": "PROPN",
                 "scope_count": 5, "corpus_count": 80},
                {"word": "civility", "lemma": "civility", "pos": "NOUN",
                 "scope_count": 12, "corpus_count": 580},
            ],
        }
        result = self._call_wrapper(fake)
        self.assertTrue(result.ok)
        rows = result.data.get("results") or result.data.get("words") or []
        # "lambton" is in _LITERARY_LOCATION_BLACKLIST, must be dropped.
        lemmas = [r.get("lemma") for r in rows]
        self.assertNotIn("lambton", lemmas)
        self.assertIn("civility", lemmas)


if __name__ == "__main__":
    unittest.main(verbosity=2)
