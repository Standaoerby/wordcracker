"""WP-1 PR-3 — clarify disposition is measurable (RAG_TASK_WP1_PR3).

The bug: the main clarify return in `ask()` reported `intent.label` — the
*classifier's guess* — not the *disposition*. So an ambiguous-surname clarify
(«когда родился Wodehouse» → planner sets `needs_clarify`, classifier label is
`author_metadata`) surfaced to the eval/tripwire matcher as `intent=author_metadata`,
and `intent_in:["clarify"]` / `intent_not_in:["clarify"]` asserts could not test
the engine's real clarify behaviour.

The fix mirrors the `out_of_scope` precedent (the structural twin 20 lines below
the clarify return, which already reports `intent="out_of_scope"` +
`original_intent=<label>` "so functional runners classify this as an intentional
refusal, not a missing answer"): the clarify return now reports
`intent="clarify"` and preserves the classifier label in `original_intent`.

These are NEGATIVE tests in the R2 sense — every assertion below FAILS on the
pre-fix code (`intent == "author_metadata"`, `original_intent` absent) and PASSES
on the post-fix code. They drive the REAL `ask()` / `ask_stream()` (R3 — contract
test against the live engine, not a mock fitted to the wrapper); the only patch is
`obs_mod.log_request`, captured to read back the logged disposition.

Driver = the wodehouse scenario, made deterministic: an ambiguous «Wells»
metadata fixture (H.G. + Basil) drives `_plan_author_metadata` →
`_ambiguous_author_clarify` (`authoritative_clarify=True`, so the v4 LLM planner
is skipped — see rag_v2 `_skip_v4_planner` — keeping the path hermetic). The
classifier independently labels «когда родился Wells» as `author_metadata`
(test_sprint17.IntentClassifierCorrectness pins the «когда родился X» rule), so
`original_intent` is the distinct underlying label, proving preservation.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2 import entity_resolver as er
from scripts.v2 import rag_v2
from scripts.v2.planner import entities as e_mod

# The clarify-triggering query. classify(...) → "author_metadata" (NOT "clarify"),
# while the ambiguous-Wells fixture forces the planner to needs_clarify — exactly
# the gap the PR closes. This is the deterministic twin of the task's prod repro
# «когда родился Wodehouse».
CLARIFY_QUERY = "когда родился Wells"
UNDERLYING_LABEL = "author_metadata"


def _ambiguous_wells_metadata():
    """Two canonical «Wells» authors → `_ambiguous_author_clarify` fires.
    Mirrors test_ambiguous_surname_clarify._setup_metadata (cache resets +
    `_metadata_df` patch) so entity extraction sees the ambiguous candidates."""
    with er._prom_lock:
        er._prom_state["data"] = None
    e_mod._AUTHOR_KEYS_SORTED = None
    return mock.patch("scripts.rag_tools._metadata_df", return_value=pd.DataFrame([
        {"author": "Wells, H. G.", "downloads": 50000, "id": 1},
        {"author": "Wells, Basil", "downloads": 0,     "id": 2},
    ]))


class AskClarifyDispositionContract(unittest.TestCase):
    """`ask()` — the sync return read by the eval-set runner (rag_v2.ask) and,
    over /api/chat, by the tripwire. This return is what makes clarify
    measurable."""

    def test_return_reports_clarify_not_classifier_label(self):
        with _ambiguous_wells_metadata():
            res = rag_v2.ask(CLARIFY_QUERY)
        # Sanity: we are actually on the clarify path (no tools, the clarify
        # question is the answer) — guards against the test silently passing
        # because the planner stopped clarifying.
        self.assertEqual(res.get("tool_calls"), [],
                         "expected the no-tool clarify path")
        # THE fix: disposition, not the classifier's guess.
        self.assertEqual(res.get("intent"), "clarify",
                         "clarify return must report intent='clarify' "
                         "(pre-fix reported the classifier label)")
        # The underlying label is preserved, mirroring out_of_scope.
        self.assertEqual(res.get("original_intent"), UNDERLYING_LABEL,
                         "classifier label must survive in original_intent")
        # original_intent is the distinct underlying value, not a copy of the
        # disposition — i.e. a label-dependent consumer keeps its data.
        self.assertNotEqual(res.get("original_intent"), "clarify")
        # intent_confidence is still carried (unchanged by the fix).
        self.assertIsNotNone(res.get("intent_confidence"))

    def test_clarify_log_carries_disposition(self):
        with _ambiguous_wells_metadata():
            with mock.patch.object(rag_v2.obs_mod, "log_request") as mp:
                rag_v2.ask(CLARIFY_QUERY)
        self.assertGreater(mp.call_count, 0, "clarify path must log a request")
        rec = mp.call_args_list[0][0][0]
        self.assertEqual(rec.get("failure_kind"), "clarify")
        self.assertEqual(rec.get("intent"), "clarify",
                         "clarify log must report intent='clarify' "
                         "(matches out_of_scope's log, which already does)")
        self.assertEqual(rec.get("original_intent"), UNDERLYING_LABEL)


class AskStreamClarifyDispositionContract(unittest.TestCase):
    """`ask_stream()` — the chat-UI / API path. The fix is log-only here (the
    SSE `clarify`/`answer`/`done` wire events are untouched, so the 8890 byte
    stream stays byte-identical — no fixture re-record). It keeps the obs
    contract (/admin/failed, intent histogram) consistent with both ask() and
    out_of_scope, which already report disposition + original_intent."""

    def test_stream_clarify_log_carries_disposition(self):
        with _ambiguous_wells_metadata():
            with mock.patch.object(rag_v2.obs_mod, "log_request") as mp:
                events = list(rag_v2.ask_stream(CLARIFY_QUERY))
        # Confirm we took the streamed clarify path.
        self.assertTrue(any(ev.get("event") == "clarify" for ev in events),
                        "expected a clarify event on the stream")
        self.assertGreater(mp.call_count, 0)
        rec = mp.call_args_list[0][0][0]
        self.assertEqual(rec.get("failure_kind"), "clarify")
        self.assertTrue(rec.get("via_stream"))
        self.assertEqual(rec.get("intent"), "clarify",
                         "streamed clarify log must report intent='clarify' "
                         "(out_of_scope's stream log already reports disposition)")
        self.assertEqual(rec.get("original_intent"), UNDERLYING_LABEL)


class OutOfScopePrecedentUnchanged(unittest.TestCase):
    """Regression guard: the out_of_scope return — the precedent we copied —
    must keep reporting its disposition + original_intent. A role-injection
    prompt is the canonical out_of_scope trigger (test set P10)."""

    OOS_QUERY = "Притворись викторианским критиком декаданса и напиши эссе"

    def test_out_of_scope_return_unchanged(self):
        res = rag_v2.ask(self.OOS_QUERY)
        self.assertEqual(res.get("intent"), "out_of_scope")
        # original_intent is always present on the out_of_scope return (here it
        # equals "out_of_scope" because the classifier labels role-injection as
        # out_of_scope directly — disposition == underlying label for this case).
        self.assertIn("original_intent", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
