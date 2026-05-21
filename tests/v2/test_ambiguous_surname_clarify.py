"""B-R17-1 stage3.2 v2 (Stan UX correction) — when planner extracts a
bare surname matching multiple canonical authors, plan builders must
emit `needs_clarify=True` with a list of options instead of silently
aggregating.

Tested plans:
  * _plan_author_metadata  — «биография Wells» → clarify
  * _plan_author_lookup    — «какие книги у Wells» → clarify
  * _plan_author_vocab     — «фирменные слова Wells» → clarify

Stable surnames (Wodehouse / specific aliases like Hardy → Thomas)
must NOT trigger clarify.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2 import entity_resolver as er
from scripts.v2.planner import entities as e_mod
from scripts.v2.planner.entities import Entities, extract
from scripts.v2.planner.plan import (
    _ambiguous_author_clarify,
    _plan_author_lookup,
    _plan_author_metadata,
    _plan_author_vocab,
)


def _setup_metadata(rows):
    with er._prom_lock:
        er._prom_state["data"] = None
    e_mod._AUTHOR_KEYS_SORTED = None
    return mock.patch("scripts.rag_tools._metadata_df",
                       return_value=pd.DataFrame(rows))


class AmbiguousClarifyHelper(unittest.TestCase):
    def test_returns_clarify_with_candidate_list(self):
        e = Entities(
            author_regex="^Wells,",
            author_clarify_candidates=[
                {"name": "Wells, H. G.",   "downloads": 50000, "books": 22},
                {"name": "Wells, Basil",   "downloads": 0,     "books": 1},
                {"name": "Wells, Carolyn", "downloads": 0,     "books": 5},
            ],
        )
        plan = _ambiguous_author_clarify(e)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)
        # B-R17-1 stage3.2 v3 — must be authoritative so v4 LLM planner
        # in rag_v2 doesn't override with a generic top_books plan.
        self.assertTrue(plan.authoritative_clarify,
                          "ambiguous-author clarify must set "
                          "authoritative_clarify=True so v4 LLM "
                          "planner doesn't take over")
        # List of options surfaced in question text
        self.assertIn("Wells, H. G.", plan.clarify_question)
        self.assertIn("Wells, Basil", plan.clarify_question)
        # Original surname mentioned
        self.assertIn("Wells", plan.clarify_question)

    def test_no_clarify_when_unambiguous(self):
        e = Entities(author_regex="^Wells,",
                     author_clarify_candidates=[])
        self.assertIsNone(_ambiguous_author_clarify(e))

    def test_no_clarify_when_single_candidate(self):
        """One canonical → no real ambiguity; let normal path proceed."""
        e = Entities(
            author_regex="^Wodehouse,",
            author_clarify_candidates=[
                {"name": "Wodehouse, P. G.", "downloads": 5000, "books": 65},
            ],
        )
        # Helper only triggers on ≥2 candidates.
        self.assertIsNone(_ambiguous_author_clarify(e))


class AuthorLookupClarifies(unittest.TestCase):
    def test_wells_lookup_returns_clarify_plan(self):
        with _setup_metadata([
            {"author": "Wells, H. G.",   "downloads": 50000, "id": 1},
            {"author": "Wells, Basil",   "downloads": 0,     "id": 2},
            {"author": "Wells, Carolyn", "downloads": 0,     "id": 3},
        ]):
            ent = extract("какие книги у Wells")
            plan = _plan_author_lookup(ent)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)
        self.assertIn("Wells, H. G.", plan.clarify_question)
        self.assertEqual(plan.steps, [])

    def test_wodehouse_lookup_returns_normal_plan(self):
        """Wodehouse — only one canonical → normal author_metadata plan."""
        with _setup_metadata([
            {"author": "Wodehouse, P. G.", "downloads": 5000, "id": 1},
        ]):
            ent = extract("какие книги у Wodehouse")
            plan = _plan_author_lookup(ent)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertFalse(plan.needs_clarify)
        self.assertEqual(len(plan.steps), 1)


class AuthorMetadataClarifies(unittest.TestCase):
    def test_wells_metadata_returns_clarify(self):
        with _setup_metadata([
            {"author": "Wells, H. G.",   "downloads": 50000, "id": 1},
            {"author": "Wells, Basil",   "downloads": 0,     "id": 2},
        ]):
            ent = extract("биография Wells")
            plan = _plan_author_metadata(ent)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)


class AuthorVocabClarifies(unittest.TestCase):
    def test_wells_vocab_returns_clarify(self):
        with _setup_metadata([
            {"author": "Wells, H. G.",   "downloads": 50000, "id": 1},
            {"author": "Wells, Basil",   "downloads": 0,     "id": 2},
        ]):
            ent = extract("фирменные слова Wells")
            plan = _plan_author_vocab(ent)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)


class SpecificAliasNeverClarifies(unittest.TestCase):
    """Hardy alias `^Hardy, Thomas` is specific — no clarify even if
    metadata has multiple Hardy canonicals."""

    def test_hardy_lookup_normal_plan(self):
        with _setup_metadata([
            {"author": "Hardy, Thomas", "downloads": 12000, "id": 1},
            {"author": "Hardy, E. D.",  "downloads": 0,     "id": 2},
        ]):
            ent = extract("какие книги у Hardy")
            plan = _plan_author_lookup(ent)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertFalse(plan.needs_clarify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
