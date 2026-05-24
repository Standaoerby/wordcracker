"""W-6 (2026-05-23) — word_contexts: snippet normalization + dedup at
wrapper layer (not just view layer).

Prod bug «примеры heart у Дойла → 5 контекстов, текст каждого None»:
v1 `rag_tools.word_contexts` returns samples with `context` field. The
v2 wrapper used to surface raw v1 samples in `r.data` while the view
helper normalized only `r.view`. The LLM renderer reads `r.data.samples`
directly — saw `snippet=None` → rendered «None».

This test file pins:
  1. Wrapper-level normalization populates `snippet` from
     `context`/`text` BEFORE shipping data to the renderer.
  2. Blank/whitespace snippets are dropped (not surfaced as None).
  3. dedup_by_key actually fires now that `snippet` is populated
     (previously every row was «missing key → keep» = no-op).
  4. `word_contexts_global` gets the same fix via _normalize_and_dedup_samples.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class WordContextsWrapperPopulatesSnippet(unittest.TestCase):
    """The fix moved snippet normalization from view-only into the
    wrapper. `r.data.samples` now has `snippet` populated for the LLM."""

    def test_data_samples_carry_snippet_text(self):
        """r.data.samples[i]['snippet'] must be the real text, not None.
        This is what the LLM renderer reads."""
        from scripts.v2.tools.words.contexts import word_contexts

        v1_real_shape = {
            "author_regex": "^Doyle,", "word": "heart",
            "total_occurrences": 5,
            "samples": [
                # v1 puts the text under `context`, NOT `snippet`
                {"pg_id": "PG108", "title": "Return of Sherlock Holmes",
                 "context": "the heart of darkness lay before him"},
                {"pg_id": "PG244", "title": "A Study in Scarlet",
                 "context": "with a faint heart she advanced"},
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts",
                         return_value=v1_real_shape):
            r = word_contexts(author_regex="^Doyle,", word="heart")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        self.assertEqual(len(samples), 2)
        for s in samples:
            self.assertIn("snippet", s)
            self.assertTrue(s["snippet"],
                            "snippet must carry the actual text, not None")
            self.assertNotEqual(s["snippet"].lower(), "none")

    def test_blank_or_none_samples_dropped_from_data(self):
        """If v1 returned a sample with no usable text, drop it — don't
        ship `snippet=None` to the renderer."""
        from scripts.v2.tools.words.contexts import word_contexts

        v1_partial = {
            "author_regex": "^Doyle,", "word": "heart",
            "samples": [
                {"pg_id": "PG1", "title": "X", "context": None},
                {"pg_id": "PG2", "title": "Y", "context": "valid heart text"},
                {"pg_id": "PG3", "title": "Z", "context": ""},
                {"pg_id": "PG4", "title": "W", "context": "   "},
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts",
                         return_value=v1_partial):
            r = word_contexts(author_regex="^Doyle,", word="heart")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["snippet"], "valid heart text")

    def test_dedup_by_snippet_fires_on_overlapping_windows(self):
        """Stan Round 11 Q30: Doyle ctx1=ctx3, ctx2=ctx4 (same passage
        indexed under overlapping ±10-token windows). dedup_by_key was
        a no-op before W-6 because `snippet` was None → «missing key →
        keep». Now snippet is populated → dedup fires."""
        from scripts.v2.tools.words.contexts import word_contexts

        v1_dup = {
            "author_regex": "^Doyle,", "word": "heart",
            "samples": [
                {"pg_id": "PG108", "title": "Sherlock",
                 "context": "the heart of darkness lay before him"},
                {"pg_id": "PG108", "title": "Sherlock",
                 "context": "  the heart of darkness lay before him "},  # whitespace dup
                {"pg_id": "PG244", "title": "Study",
                 "context": "with a faint heart she advanced"},
                {"pg_id": "PG244", "title": "Study",
                 "context": "THE HEART OF DARKNESS LAY BEFORE HIM"},  # case dup
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts",
                         return_value=v1_dup):
            r = word_contexts(author_regex="^Doyle,", word="heart")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        # 4 inputs → 2 unique after case/whitespace-normalised dedup
        self.assertEqual(len(samples), 2)
        # Warning surfaces the dedup
        dedup_warning = [w for w in r.warnings
                          if w.code == "snippet_dedup"]
        self.assertEqual(len(dedup_warning), 1)

    def test_all_empty_samples_yields_no_samples_warning(self):
        """No usable contexts → friendly no_samples signal, not None."""
        from scripts.v2.tools.words.contexts import word_contexts

        v1_empty = {
            "author_regex": "^Doyle,", "word": "factory",
            "samples": [
                {"pg_id": "PG1", "title": "X", "context": None},
                {"pg_id": "PG2", "title": "Y", "context": ""},
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts",
                         return_value=v1_empty):
            r = word_contexts(author_regex="^Doyle,", word="factory")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        self.assertEqual(samples, [])
        no_samples = [w for w in r.warnings if w.code == "no_samples"]
        self.assertEqual(len(no_samples), 1)


class WordContextsGlobalSameNormalization(unittest.TestCase):
    """word_contexts_global must apply the same normalization — same
    failure mode otherwise (Stan «factory» tests this path)."""

    def test_data_samples_carry_snippet_text_global(self):
        from scripts.v2.tools.words.contexts import word_contexts_global

        v1_real_shape = {
            "word": "factory", "k": 3,
            "samples": [
                {"pg_id": "PG345", "title": "Hard Times", "author": "Dickens",
                 "context": "the factory whistle blew at six"},
                {"pg_id": "PG766", "title": "David Copperfield",
                 "author": "Dickens",
                 "context": "the bottle factory loomed by the river"},
            ],
            "unique_authors": 1,
        }
        with mock.patch("scripts.rag_tools.word_contexts_global",
                         return_value=v1_real_shape):
            r = word_contexts_global(word="factory")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        self.assertEqual(len(samples), 2)
        for s in samples:
            self.assertIn("snippet", s)
            self.assertNotEqual(s["snippet"].lower(), "none")
            self.assertTrue(s["snippet"].strip())

    def test_global_dedup_fires(self):
        from scripts.v2.tools.words.contexts import word_contexts_global

        v1_dup = {
            "word": "factory", "k": 3,
            "samples": [
                {"pg_id": "PG345", "author": "Dickens",
                 "context": "the factory whistle blew at six"},
                {"pg_id": "PG345", "author": "Dickens",
                 "context": "  the factory whistle blew at six  "},
                {"pg_id": "PG766", "author": "Dickens",
                 "context": "the bottle factory loomed by the river"},
            ],
        }
        with mock.patch("scripts.rag_tools.word_contexts_global",
                         return_value=v1_dup):
            r = word_contexts_global(word="factory")
        self.assertTrue(r.ok)
        samples = (r.data or {}).get("samples") or []
        self.assertEqual(len(samples), 2)


class WrapperVersionBumped(unittest.TestCase):
    """W-6 bumped wrapper_version on word_contexts so pre-W-6 cached
    samples (raw v1 shape with `context` only, `snippet=None`) get
    invalidated. Lock the bump so the next change must justify another."""

    def test_wrapper_version_v4_plus(self):
        from scripts.v2.tool_registry import REGISTRY
        spec = REGISTRY.get("word_contexts")
        self.assertIsNotNone(spec, "word_contexts must be registered")
        # Format: vN-phase2-contract — N >= 4 since W-6 bump
        version = getattr(spec, "wrapper_version", None) or ""
        self.assertTrue(version.startswith("v") and "phase2-contract" in version,
                         f"unexpected version format: {version!r}")
        major = int(version.split("-", 1)[0].lstrip("v"))
        self.assertGreaterEqual(major, 4,
                                  f"W-6 required v4+, got {version!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
