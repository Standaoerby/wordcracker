"""W-1 v2 (2026-05-24) — over-eager author disambiguation, prod path.

The previous fix (commit bd3cf1a, 2026-05-23) was verified at the v6
resolver level — `tests/v2/test_entity_resolver_v6.py` covers
`resolve_v6()` and `resolve_v6_for_alias()` directly.

An external probe on 2026-05-24 still reported the four W-1 acceptance
queries failing. The gap was that *no test* exercised the actual prod
path — `scripts.v2.planner.entities.extract(query)` — and that's where
the v6 result gets translated into `author_regex` / `author_clarify_candidates`
for the planner. A silent `except Exception: pass` around the v6 call
was masking failures, and the surname-only fallback (`_collect_surname_candidates`)
had no first-name awareness — so any v6 hiccup in prod produced the
exact over-eager clarify the TZ was trying to close.

These tests pin the acceptance contract at the prod boundary:

  (1) extras filter — given a first name / initials, candidates without
      that token are removed; if exactly one matcher remains, resolve
  (2) dominance — ≥90% share OR ≥10× ratio resolves (existing Rules
      4.45 / 4.6 verified through the extract path)
  (3) canonical format «Surname, Firstname» always resolves, even
      cross-alphabet, and never bounces back to clarify
  (4) bare surname (no first name) still clarifies — Wells stays as
      the regression sentinel
  (5) defensive: even if v6 raises, the fallback in `extract()` honours
      first-name extras and doesn't blindly clarify

Test isolation: each test sets up an isolated `_metadata_df` mock
(prod-shape Wells/Doyle/Marlowe/Tolstoy distribution) + resets the
prominence index. No shared state between tests.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2 import entity_resolver as er
from scripts.v2.planner.entities import extract


def _fixture_df() -> pd.DataFrame:
    """Reproduces prod-shape Wells / Doyle / Marlowe / Tolstoy
    distribution used in tests/v2/test_entity_resolver_v6.py — keeps
    both suites pinned to the same fixture so any change in prod
    distribution (e.g. new Doyle author imported) reproduces here
    AND in the resolver-level tests.
    """
    return pd.DataFrame([
        # Wells — H.G. dominant at ~88.93% (below 90% on purpose so
        # the bare-surname clarify path stays exercised by the regression
        # sentinel «у Уэллса»).
        {"author": "Wells, H. G. (Herbert George)", "downloads": 39000, "id": 1},
        {"author": "Wells, H. G. (Herbert George)", "downloads": 1500, "id": 2},
        {"author": "Wells, Basil", "downloads": 4292, "id": 3},
        {"author": "Wells, Carolyn", "downloads": 500, "id": 4},
        {"author": "Wells, Hal K.", "downloads": 0, "id": 5},
        {"author": "Wells, J. (Joseph)", "downloads": 200, "id": 6},
        {"author": "Wells, Frederic DeWitt", "downloads": 50, "id": 7},
        # Marlowe — Christopher dominant
        {"author": "Marlowe, Christopher", "downloads": 12000, "id": 8},
        {"author": "Marlowe, Stephen", "downloads": 50, "id": 9},
        {"author": "Marlowe, Amy Bell", "downloads": 20, "id": 10},
        # Doyle — Arthur Conan dominant (99% share, 325× ratio)
        {"author": "Doyle, Arthur Conan", "downloads": 65000, "id": 11},
        {"author": "Doyle, Charles", "downloads": 200, "id": 12},
        {"author": "Doyle, William", "downloads": 50, "id": 13},
    ])


class _ExtractPathTestBase(unittest.TestCase):
    """Base — fresh metadata mock + prominence index per test."""

    def setUp(self):
        self._patcher = mock.patch(
            "scripts.rag_tools._metadata_df",
            return_value=_fixture_df(),
        )
        self._patcher.start()
        with er._prom_lock:
            er._prom_state["data"] = None
        er.get_prominence_index(force_reload=True)

    def tearDown(self):
        self._patcher.stop()
        with er._prom_lock:
            er._prom_state["data"] = None


class W1V2AcceptanceViaExtractPath(_ExtractPathTestBase):
    """Pin TZ §W-1 acceptance at the prod boundary (extract → planner).

    Without these, the next regression hides behind the v6 unit suite
    passing while the real chat path keeps clarifying.
    """

    # (1) — first-name filter: «Christopher Marlowe» resolves directly,
    #       no clarify, regex narrows to the canonical-specific form
    def test_christopher_marlowe_resolves_via_extract_path(self):
        e = extract("сколько книг написал Christopher Marlowe")
        self.assertEqual(
            e.author_clarify_candidates, [],
            msg="W-1 (1): explicit first name must NOT trigger clarify",
        )
        # Resolved regex must be the specific canonical, not the bare
        # surname — bare would mean over-aggregation downstream.
        self.assertEqual(e.author_regex, "^Marlowe, Christopher")

    # (2) — dominance via the Russian-genitive path that the W-1 spec
    #       originally tripped on: «Конан Дойла»
    def test_konan_dojla_resolves_via_extract_path(self):
        e = extract("фирменные слова Конан Дойла")
        self.assertEqual(
            e.author_clarify_candidates, [],
            msg="W-1 (2): «Конан Дойла» must resolve to Arthur Conan",
        )
        self.assertEqual(e.author_regex, "^Doyle, Arthur Conan")

    # (3) — canonical format suggested back to the user resolves on
    #       the FIRST retry, no clarify loop
    def test_canonical_format_marlowe_christopher_resolves(self):
        e = extract("Marlowe, Christopher")
        self.assertEqual(
            e.author_clarify_candidates, [],
            msg="W-1 (3): «Surname, Firstname» MUST resolve, not bounce",
        )
        self.assertEqual(e.author_regex, "^Marlowe, Christopher")

    # (4) — bare surname (no first name) still clarifies — sentinel
    def test_bare_uelsa_still_clarifies(self):
        e = extract("какие книги у Уэллса")
        # Wells fixture distribution is sub-90% / sub-10× by design,
        # so bare RU stem must still ask the user.
        self.assertGreaterEqual(
            len(e.author_clarify_candidates), 2,
            msg="W-1 (4): bare surname without name MUST clarify",
        )
        self.assertEqual(e.author_regex, "^Wells,")

    # (3.b) — canonical format inside a sentence too
    def test_canonical_format_in_sentence_resolves(self):
        e = extract("фирменные слова Doyle, Arthur Conan")
        self.assertEqual(e.author_clarify_candidates, [])
        self.assertEqual(e.author_regex, "^Doyle, Arthur Conan")


class W1V2FilterByExtras(_ExtractPathTestBase):
    """Direction (1) — the filter is an explicit predicate, not just an
    up-weight. Pin its observable behaviour through `extract()`."""

    def test_basil_wells_resolves_to_basil_via_extract(self):
        # H.G. is 88.93% prominent — without the first-name filter, a
        # naive resolver could still pick him. The «basil» extra MUST
        # narrow to Basil.
        e = extract("Basil Wells")
        self.assertEqual(e.author_clarify_candidates, [])
        self.assertEqual(e.author_regex, "^Wells, Basil")

    def test_carolyn_wells_resolves_to_carolyn_via_extract(self):
        # Same sanity: a less-prominent Wells with explicit first name
        # must not get steamrolled by H.G. prominence.
        e = extract("какие книги у Carolyn Wells")
        self.assertEqual(e.author_clarify_candidates, [])
        self.assertEqual(e.author_regex, "^Wells, Carolyn")


class W1V2CanonicalReplyToClarify(_ExtractPathTestBase):
    """W-1 v2 (2026-05-24) — «не зацикливается».

    Spec acceptance line 4: «В ответ на дизамбиг-подсказку «Marlowe,
    Christopher» → запрос выполняется, не зацикливается». The bare
    canonical-form reply has no referring particles / affirmatives,
    so the existing `_looks_like_followup` missed it; the planner
    asked «не уверен, что ты имеешь в виду» — perceived as a loop.

    Hook (`_is_canonical_author_reply_to_clarify` in history.py): when
    the immediately-preceding assistant turn was an author-clarify
    AND the current text is a bare «Surname, FirstName», replay the
    prior NON-clarify user intent.
    """

    def test_canonical_reply_replays_prior_intent(self):
        from scripts.v2.planner.intent import classify
        from scripts.v2.planner import history as history_mod
        from scripts.v2.planner import plan as plan_mod
        history = [
            {"role": "user",
             "content": "сколько книг написал Marlowe"},
            {"role": "assistant",
             "content": (
                 "Под фамилией «Marlowe» в корпусе несколько авторов. "
                 "Кого ты имеешь в виду? "
                 "• Marlowe, Christopher • Marlowe, Stephen "
                 "• Marlowe, Amy Bell. "
                 "Уточни полное имя — например, «Marlowe, Christopher» — "
                 "и я повторю запрос."
             )},
        ]
        q = "Marlowe, Christopher"
        e = extract(q)
        e = history_mod.merge_with_history(e, history, q)
        # Resolver layer
        self.assertEqual(e.author_regex, "^Marlowe, Christopher")
        self.assertEqual(e.author_clarify_candidates, [])
        # Intent re-inference
        initial_intent = classify(q)
        inferred = history_mod.infer_followup_intent(q, history)
        self.assertIsNotNone(
            inferred,
            msg="canonical reply to clarify MUST inherit prior intent",
        )
        self.assertNotEqual(
            inferred, "clarify",
            msg="inferred intent must be a real action, not clarify",
        )
        # Planner output — no loop
        p = plan_mod.build(inferred, e)
        self.assertFalse(
            p.needs_clarify,
            msg="planner must NOT bounce «Marlowe, Christopher» to clarify",
        )

    def test_fresh_canonical_query_does_NOT_inherit(self):
        # Critical regression guard: «Marlowe, Christopher» as the
        # FIRST turn (no history, or last assistant turn wasn't a
        # clarify) must NOT silently inherit some random prior intent.
        from scripts.v2.planner import history as history_mod
        self.assertIsNone(
            history_mod.infer_followup_intent("Marlowe, Christopher", None),
            msg="fresh canonical query must not infer follow-up",
        )
        self.assertIsNone(
            history_mod.infer_followup_intent("Marlowe, Christopher", []),
        )
        unrelated_history = [
            {"role": "user", "content": "что значит ajar"},
            {"role": "assistant", "content": "ajar — приоткрытый."},
        ]
        self.assertIsNone(
            history_mod.infer_followup_intent(
                "Marlowe, Christopher", unrelated_history,
            ),
            msg="canonical reply after non-clarify must not inherit",
        )


class W1V2DefensiveFallback(_ExtractPathTestBase):
    """W-1 v2 root cause: the silent `except Exception: pass` in
    `entities.extract()`. When v6 raises in prod (any reason — transient
    error, schema drift, anything), the old fallback
    `_collect_surname_candidates` was used WITHOUT first-name awareness
    and clarified over-eagerly.

    These tests force v6 to raise and assert the fallback honours
    first-name extras instead of clarifying on every surname.
    """

    def test_fallback_resolves_when_extras_unique_match(self):
        # Even if v6 explodes, «Christopher Marlowe» must still resolve
        # to Marlowe, Christopher through the fallback path because
        # the surname-candidate list filtered by extras has exactly
        # one match.
        with mock.patch(
            "scripts.v2.entity_resolver_v6.main.resolve_v6_for_alias",
            side_effect=RuntimeError("simulated v6 failure"),
        ):
            e = extract("сколько книг написал Christopher Marlowe")
        self.assertEqual(
            e.author_clarify_candidates, [],
            msg="fallback must honour extras and not clarify",
        )
        self.assertEqual(e.author_regex, "^Marlowe, Christopher")

    def test_fallback_filters_clarify_list_to_matchers(self):
        # «у Wells, Joseph» — extras = «joseph». Without the
        # extras-aware fallback, all 5 Wells canonicals would appear.
        # With it, only matching ones do (here: Joseph).
        with mock.patch(
            "scripts.v2.entity_resolver_v6.main.resolve_v6_for_alias",
            side_effect=RuntimeError("simulated v6 failure"),
        ):
            e = extract("какие книги у Joseph Wells")
        # Either resolved to the single Joseph, or the clarify list
        # narrowed — both honour direction (1).
        if e.author_clarify_candidates:
            names = {c.get("name") for c in e.author_clarify_candidates}
            self.assertTrue(
                all("Joseph" in n or "J." in n for n in names),
                msg=f"fallback clarify list must be filtered to "
                    f"first-name matchers, got: {names}",
            )
        else:
            self.assertEqual(e.author_regex, "^Wells, J. (Joseph)")

    def test_fallback_keeps_full_list_when_no_extras_match(self):
        # If the corpus genuinely lacks a matching first name, fall
        # back to the unfiltered surname list so the user can pick.
        # Marlowe canonicals are [Christopher, Stephen, Amy Bell]; if
        # the user asks for «Tudor Marlowe», no Marlowe contains
        # «tudor» → keep full list.
        with mock.patch(
            "scripts.v2.entity_resolver_v6.main.resolve_v6_for_alias",
            side_effect=RuntimeError("simulated v6 failure"),
        ):
            e = extract("какие книги у Tudor Marlowe")
        # Either v5-fallback gave us the full list, or extract dropped
        # to no clarify (regex stays the bare surname). Both are valid;
        # we mainly verify NO crash and NO wrong resolve.
        self.assertIn(e.author_regex, ("^Marlowe,", None))
        if e.author_clarify_candidates:
            # Must include the full Marlowe set since «tudor» matches
            # none of them — direction (1) is about explicit filtering,
            # not silent dropping.
            names = {c.get("name") for c in e.author_clarify_candidates}
            self.assertIn("Marlowe, Christopher", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
