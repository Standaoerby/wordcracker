"""WP-A 2.7.37 — author-level NER shield against proper-noun leaks.

Spec: RAG_TASK_author_keyness_propn.md (the `dunwich` leak). `dunwich` is a
real Suffolk village (corpus 528 ≫ author 38), so it survives every existing
proper-noun defence in `affinity_by_author`:

  * corpus-diff heuristic: 528 - 38 = 490 ≥ max(10, 19) → passes;
  * isolated-token spaCy POS: unreliable on a lowercased single token;
  * word_dict cache: `dunwich` was never flagged.

WP-A adds a structural shield: the union of `_book_propn_set(pg)` over the
author's books (the same whole-book NER that killed the `galatz` leak in
book_archaic WP-C) drops any candidate spaCy NER tagged as PERSON/GPE/LOC/...

This suite has three parts:
  * R3 contract — exercises the REAL `learning_tools._book_propn_set` and
    asserts its return *shape* (a `set`), the contract `_author_propn_set`
    depends on. No mock fitted to the wrapper.
  * union logic — `_author_propn_set` unions per-book sets and caches per slug.
  * R2 negative — `dunwich` survives every existing filter and is dropped ONLY
    by the new author-NER shield. This assertion FAILS on pre-fix code (no
    shield) and PASSES post-fix; common-noun signatures (`horror`/`soul`)
    survive (no mass over-drop).
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

import pandas as pd  # noqa: E402

from scripts import rag_tools  # noqa: E402


class BookPropnSetContract(unittest.TestCase):
    """R3 — contract against the REAL v1 `_book_propn_set`."""

    def test_returns_a_set_of_strings(self):
        # Bare-name import mirrors the in-function import in rag_tools; ensure
        # scripts/ is importable so we hit the REAL callee, not a stub.
        sys.path.insert(0, str(_REPO / "scripts"))
        try:
            from learning_tools import _book_propn_set
        finally:
            sys.path.pop(0)
        # No raw text for a bogus id → the real function returns an empty set
        # (not None, not a dict). That is the shape the shield relies on.
        result = _book_propn_set("PG_DOES_NOT_EXIST_999999")
        self.assertIsInstance(result, set)
        self.assertEqual(result, set())


class AuthorPropnSetUnion(unittest.TestCase):
    """`_author_propn_set` unions per-book NER sets and caches per slug."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wc-author-propn-")
        self._dir_patch = mock.patch.object(
            rag_tools, "AUTHOR_PROPN_DIR", Path(self._tmp))
        self._dir_patch.start()
        # Fake learning_tools so the in-function `from learning_tools import
        # _book_propn_set` resolves to our stub (real per-book NER needs the
        # corpus on disk; the SHAPE — set per pg — matches v1, R4).
        self._fake_lt = types.ModuleType("learning_tools")
        self._book_sets = {
            "PG1": {"dunwich", "arkham"},
            "PG2": {"innsmouth", "arkham"},
        }
        self._fake_lt._book_propn_set = lambda pg: set(self._book_sets.get(pg, set()))
        self._saved_lt = sys.modules.get("learning_tools")
        sys.modules["learning_tools"] = self._fake_lt

    def tearDown(self):
        self._dir_patch.stop()
        if self._saved_lt is not None:
            sys.modules["learning_tools"] = self._saved_lt
        else:
            sys.modules.pop("learning_tools", None)

    def _sel(self):
        return pd.DataFrame({"id": ["PG1", "PG2"]})

    def test_union_over_books(self):
        with mock.patch.object(rag_tools, "_select_books",
                               return_value=self._sel()):
            got = rag_tools._author_propn_set("^Lovecraft, H. P.", "lovecraft")
        self.assertEqual(got, {"dunwich", "arkham", "innsmouth"})

    def test_writes_and_reuses_per_slug_cache(self):
        cache = Path(self._tmp) / "lovecraft.v1.json"
        with mock.patch.object(rag_tools, "_select_books",
                               return_value=self._sel()):
            rag_tools._author_propn_set("^Lovecraft, H. P.", "lovecraft")
        self.assertTrue(cache.exists())
        self.assertEqual(set(json.loads(cache.read_text(encoding="utf-8"))),
                         {"dunwich", "arkham", "innsmouth"})
        # Second call must hit the cache, not the (now exploding) per-book NER.
        self._fake_lt._book_propn_set = lambda pg: (_ for _ in ()).throw(
            AssertionError("per-book NER must not run on a warm cache"))
        with mock.patch.object(rag_tools, "_select_books",
                               return_value=self._sel()):
            again = rag_tools._author_propn_set("^Lovecraft, H. P.", "lovecraft")
        self.assertEqual(again, {"dunwich", "arkham", "innsmouth"})


class AffinityByAuthorNERShield(unittest.TestCase):
    """R2 — `dunwich` dropped only by the author-NER shield; signatures stay."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="wc-affinity-")
        # A base CSV that carries the keyness columns (current schema), so
        # affinity_by_author serves it without a regen subprocess. `dunwich`
        # is built to survive every pre-existing filter:
        #   - corpus-diff: 528-38=490 ≥ max(10,19);
        #   - positive_only: log_ratio > 0;
        #   - min_ll: g2 ≥ 15.13.
        df = pd.DataFrame([
            {"word": "dunwich", "author_count": 38, "corpus_count": 528,
             "g2": 491.0, "log_ratio": 4.2, "rel_freq": 12.0},
            {"word": "horror", "author_count": 300, "corpus_count": 6000,
             "g2": 220.0, "log_ratio": 1.8, "rel_freq": 3.0},
            {"word": "soul", "author_count": 250, "corpus_count": 5000,
             "g2": 150.0, "log_ratio": 1.5, "rel_freq": 2.5},
        ])
        self._csv = Path(self._tmp) / "lovecraft_affinity.csv"
        df.to_csv(self._csv, index=False)
        # Drive the REAL shield end-to-end (not a mock of _author_propn_set):
        # fake the per-book NER (whole-book NER needs the corpus on disk) and
        # the book-selection, but exercise the real union + drop wiring. So the
        # negative test fails on pre-fix code because `dunwich` SURVIVES — the
        # right reason — not merely because a symbol is missing.
        self._fake_lt = types.ModuleType("learning_tools")
        self._fake_lt._book_propn_set = lambda pg: {"dunwich"}
        self._saved_lt = sys.modules.get("learning_tools")
        sys.modules["learning_tools"] = self._fake_lt
        self._patches = [
            mock.patch.object(rag_tools, "DERIVED_DIR", Path(self._tmp)),
            mock.patch.object(rag_tools, "AUTHOR_PROPN_DIR", Path(self._tmp)),
            # Keep all candidates through the spaCy POS pass (deterministic in
            # CI where the model may be absent): empty tag map ⇒ nothing PROPN.
            mock.patch.object(rag_tools, "_spacy_pos_tags", return_value={}),
            mock.patch.object(rag_tools, "_select_books",
                              return_value=pd.DataFrame({"id": ["PG1"]})),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self._saved_lt is not None:
            sys.modules["learning_tools"] = self._saved_lt
        else:
            sys.modules.pop("learning_tools", None)

    def test_dunwich_dropped_signatures_survive(self):
        out = rag_tools.affinity_by_author(
            "^Lovecraft, H. P.", top=10, min_corpus_count=0)
        self.assertNotIn("error", out, out)
        words = [r["word"] for r in out["top"]]
        # The leak is gone — this is the line that FAILS on pre-fix code
        # (no shield ⇒ dunwich survives every other filter).
        self.assertNotIn("dunwich", words)
        # No mass over-drop: genuine common-noun signatures remain.
        self.assertIn("horror", words)
        self.assertIn("soul", words)
        self.assertIn("author-NER dropped 1", out["proper_noun_filter"])

    def test_no_drop_when_shield_empty(self):
        # Empty union (e.g. raw text absent in CI) ⇒ no over-drop; dunwich
        # stays exactly as the pre-existing filters left it.
        self._fake_lt._book_propn_set = lambda pg: set()
        out = rag_tools.affinity_by_author(
            "^Lovecraft, H. P.", top=10, min_corpus_count=0)
        words = [r["word"] for r in out["top"]]
        self.assertIn("dunwich", words)
        self.assertIn("author-NER dropped 0", out["proper_noun_filter"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
