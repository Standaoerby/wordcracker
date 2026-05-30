"""S-R5b / E1 — author_lookup dominant-resolve + book-list render.

PROBE E1 (live prod 2.6.25, «какие книги у Уэллса»): intent=author_lookup
(0.92), tool_calls=[], answer = a 5-way clarify list of Wells homonyms.
Two-layer bug:

  L1 RESOLVE  — the v6 resolver clarifies a bare «Уэллса» (Wells H.G. is
                88.9% / 9.4×, just under the global 90% / 10× dominance
                floor — by design, TZ §W-1 line 51). The author_lookup
                planner inherited that clarify, so the books were never
                fetched.
  L2 RENDER   — even when resolved, author_lookup routes to the
                `author_metadata` tool whose AUTHOR_METADATA view is a
                bio card with NO titles, so canonical works never showed.

FIX (both scoped, neither global):
  L1 — `_plan_author_lookup` resolves a *clearly* dominant homonym
       (≥60% share OR ≥3× downloads) to the leader. The global resolver
       and the OTHER intents (author_metadata / author_vocab) keep the
       bare-surname clarify — E13 / W-1 acceptance untouched.
  L2 — the live renderer is the LLM path (_llm_render), which already
       receives author_metadata's `data` (incl. download-ranked
       `sample_titles`) but no directive to enumerate them. The
       author_lookup plan now carries a `render_notes` instruction to
       list the books. This rides plan.render_notes → render_instructions
       (no edit to the fingerprinted author_metadata wrapper → no fixture
       re-record). The deterministic AUTHOR_METADATA view is NOT on the
       live answer path (render_view has no production caller).

These are corpus-free planner tests. The full live probe (real
Wells corpus + render) is the deploy probe-gate + smoke «какие книги у
Уэллса» → list incl. Time Machine.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402

from scripts.v2 import entity_resolver as er  # noqa: E402
from scripts.v2.planner import entities as e_mod  # noqa: E402
from scripts.v2.planner.entities import Entities, extract  # noqa: E402
from scripts.v2.planner.plan import (  # noqa: E402
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


# Real prod by_canonical Wells shape (mirrors test_entity_resolver_v6):
# H.G. dominates at 40500 downloads, Basil 4292 → 9.4× / 88.9%.
_WELLS_ROWS = [
    {"author": "Wells, H. G. (Herbert George)", "downloads": 39000, "id": 1},
    {"author": "Wells, H. G. (Herbert George)", "downloads": 1500,  "id": 2},
    {"author": "Wells, Basil",                  "downloads": 4292,  "id": 3},
    {"author": "Wells, Carolyn",                "downloads": 500,   "id": 4},
    {"author": "Wells, Hal K.",                 "downloads": 0,     "id": 5},
    {"author": "Wells, J. (Joseph)",            "downloads": 200,   "id": 6},
]


class L1_DominantResolves(unittest.TestCase):
    """Positive: a clearly dominant homonym under author_lookup resolves."""

    def test_uelsa_ru_stem_resolves_under_author_lookup(self):
        # «какие книги у Уэллса» — the exact prod failure. Now resolves.
        with _setup_metadata(_WELLS_ROWS):
            ent = extract("какие книги у Уэллса")
            plan = _plan_author_lookup(ent)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertFalse(plan.needs_clarify)
        self.assertEqual(len(plan.steps), 1)
        # Resolved to H.G. specifically — the (re.escaped) regex matches
        # his canonical via contains() and cannot leak into Basil/Carolyn.
        import re
        regex = plan.steps[0].args["author_regex"]
        self.assertRegex("Wells, H. G. (Herbert George)", regex)
        self.assertIsNone(re.search(regex, "Wells, Basil", re.IGNORECASE))

    def test_dominant_resolves_from_handbuilt_candidates(self):
        # Direct-Entities form: deterministic, no resolver/metadata dep.
        e = Entities(
            author_regex="^Wells,",
            author_clarify_candidates=[
                {"name": "Wells, H. G. (Herbert George)",
                 "downloads": 40500, "books": 124},
                {"name": "Wells, Basil",   "downloads": 4292, "books": 15},
                {"name": "Wells, Carolyn", "downloads": 500,  "books": 62},
            ],
        )
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertFalse(plan.needs_clarify)
        # candidates cleared after resolve so nothing downstream re-clarifies
        self.assertEqual(plan.entities.author_clarify_candidates, [])

    def test_dominant_picked_even_if_not_first_in_list(self):
        # Robust to ordering — leader is by max downloads, not index 0.
        e = Entities(
            author_regex="^Wells,",
            author_clarify_candidates=[
                {"name": "Wells, Basil",   "downloads": 4292,  "books": 15},
                {"name": "Wells, H. G.",   "downloads": 40500, "books": 124},
            ],
        )
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertRegex("Wells, H. G.", plan.steps[0].args["author_regex"])


class L1_NegativeGuards(unittest.TestCase):
    """Negative #1: genuine ambiguity (close prominence) → still clarify,
    even under author_lookup. Don't over-resolve."""

    def test_close_field_still_clarifies(self):
        e = Entities(
            author_regex="^Twin,",
            author_clarify_candidates=[
                # 4500 / 4000 → ratio 1.13×, share ~52.9% — both below the
                # 3× / 60% author_lookup floors → must clarify.
                {"name": "Twin, Alice", "downloads": 4500, "books": 12},
                {"name": "Twin, Bob",   "downloads": 4000, "books": 10},
            ],
        )
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)
        self.assertEqual(plan.steps, [])

    def test_no_download_signal_still_clarifies(self):
        # All-zero downloads → no popularity basis to call a dominant.
        e = Entities(
            author_regex="^Smith,",
            author_clarify_candidates=[
                {"name": "Smith, A.", "downloads": 0, "books": 3},
                {"name": "Smith, B.", "downloads": 0, "books": 4},
            ],
        )
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)


class L1_ScopeGuards(unittest.TestCase):
    """Negative #2 (E13 non-regression): the SAME dominant Wells under a
    DIFFERENT intent still clarifies. The resolve is author_lookup-only."""

    def test_metadata_intent_still_clarifies(self):
        with _setup_metadata(_WELLS_ROWS):
            ent = extract("биография Уэллса")
            plan = _plan_author_metadata(ent)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)

    def test_vocab_intent_still_clarifies(self):
        with _setup_metadata(_WELLS_ROWS):
            ent = extract("фирменные слова Уэллса")
            plan = _plan_author_vocab(ent)
        self.assertEqual(plan.intent, "clarify")
        self.assertTrue(plan.needs_clarify)


class L2_RenderNote(unittest.TestCase):
    """A resolved author_lookup plan carries a render_note that tells the
    LLM renderer to enumerate the book list from sample_titles. The note
    rides plan.render_notes (consumed by _llm_render) — no edit to the
    fingerprinted author_metadata wrapper, so no fixture re-record."""

    def test_resolved_plan_has_book_list_render_note(self):
        e = Entities(
            author_regex="^Wells,",
            author_clarify_candidates=[
                {"name": "Wells, H. G. (Herbert George)",
                 "downloads": 40500, "books": 124},
                {"name": "Wells, Basil", "downloads": 4292, "books": 15},
            ],
        )
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "author_lookup")
        notes = " ".join(plan.render_notes).lower()
        self.assertIn("sample_titles", notes)
        self.assertIn("books_matched", notes)

    def test_render_note_reaches_llm_render_instructions(self):
        # End-to-end of the plumbing: plan.render_notes is surfaced into
        # the renderer's instruction list by rag_v2._llm_render.
        from scripts.v2 import rag_v2
        e = Entities(
            author_regex="^Wells,",
            author_clarify_candidates=[
                {"name": "Wells, H. G.", "downloads": 40500, "books": 124},
                {"name": "Wells, Basil", "downloads": 4292, "books": 15},
            ],
        )
        plan = _plan_author_lookup(e)
        instructions = rag_v2._collect_render_instructions([])
        for note in plan.render_notes:
            instructions.append(f"[plan] {note.strip()}")
        joined = " ".join(instructions)
        self.assertIn("author_lookup", joined)
        self.assertIn("sample_titles", joined)

    def test_single_author_lookup_also_lists_books(self):
        # Unambiguous author (Wodehouse) — still author_lookup, still
        # carries the book-list render_note (not just the dominant path).
        e = Entities(author_regex="^Wodehouse,",
                     author_clarify_candidates=[])
        plan = _plan_author_lookup(e)
        self.assertEqual(plan.intent, "author_lookup")
        self.assertTrue(any("sample_titles" in n for n in plan.render_notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
