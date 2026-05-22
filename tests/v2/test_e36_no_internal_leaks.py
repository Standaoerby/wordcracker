"""E36 (2026-05-22) — no internal config / dev-test words in user-facing
clarify or render_note text.

ROOT CAUSE: Stan caught this in prod 2026-05-22 — `_plan_translate_word_list`
clarify_question contained:
  - «Совет: спроси админа включить `WC_LLM_PLANNER=on` — v4 LLM-планнер
    видит conversation и решает это автоматически.»
  - hardcoded dev-test words «tuppence, stitching, embroidery, strychnine,
    vavasour»

Same antipattern existed in `learning_words.py:194-202` _render_note
(text fed to renderer). Both were end-user visible:
  - clarify_question is rendered DIRECTLY as the user-facing answer
  - _render_note is included in the LLM renderer prompt and frequently
    quoted verbatim

This test directly calls the two affected code paths and asserts the
specific bad strings are not in the output. Tight, no false positives.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TranslateWordListClarifyMessage(unittest.TestCase):
    """E36.1 — `_plan_translate_word_list` clarify message has been
    cleaned. Tests the actual QueryPlan output, not source text."""

    def test_clarify_no_env_var_or_admin_advice(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import _plan_translate_word_list

        # Force extraction-failed path: empty _prior_words
        e = Entities(raw_misc={"_prior_words": [], "_prior_words_total": 0})
        plan = _plan_translate_word_list(e)
        self.assertTrue(plan.needs_clarify)
        msg = plan.clarify_question or ""

        # E36 — none of these should appear in the message
        self.assertNotIn("WC_LLM_PLANNER", msg,
                          "env var name leaked to user")
        self.assertNotIn("спроси админа", msg,
                          "«ask admin» phrase leaked to user")
        self.assertNotIn("админ", msg.lower(),
                          "any admin-related advice leaked")
        # Hardcoded SPGC dev-test fixture words
        self.assertNotIn("tuppence", msg)
        self.assertNotIn("vavasour", msg)
        self.assertNotIn("strychnine", msg)

    def test_clarify_still_useful(self):
        """Make sure the cleanup didn't gut the message — it must still
        tell the user WHAT happened and HOW to recover."""
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import _plan_translate_word_list

        e = Entities(raw_misc={"_prior_words": [], "_prior_words_total": 0})
        plan = _plan_translate_word_list(e)
        msg = (plan.clarify_question or "").lower()
        # Tells the user what happened
        self.assertTrue("не получилось" in msg or "не смог" in msg,
                         "must explain what failed")
        # Gives recovery action
        self.assertTrue("скопируй" in msg or "перечисли" in msg or "пришли" in msg,
                         "must give recovery action")
        # Has example pattern (without specific words)
        self.assertIn("переведи", msg)


class LearningWordsRenderNoteText(unittest.TestCase):
    """E36.2 — `learning_words` translate-followup disclosure note has
    been cleaned of internal architecture jargon and dev-test words."""

    def test_disclosure_no_dev_test_words_or_internal_jargon(self):
        from unittest import mock
        from scripts.v2.tools.learning.learning_words import learning_words

        # Mock the v1 call (returns a normal CEFR list)
        fake_v1_raw = {
            "scope": {"book": "PG1342"},
            "level": "B2", "band": "intermediate",
            "scope_tokens": 100000, "scope_vocab": 5000,
            "results": [{"word": "civility", "cefr": "B2", "count": 12}],
        }
        with mock.patch("scripts.learning_tools.learning_words",
                         return_value=fake_v1_raw):
            # _translate_followup_disclose=True triggers the disclosure block
            result = learning_words(scope={"book": "PG1342"}, level="B2",
                                     _translate_followup_disclose=True)

        self.assertTrue(result.ok)
        note = (result.data.get("_render_note") or "")

        # The bad strings must not appear
        self.assertNotIn("tuppence", note)
        self.assertNotIn("stitching", note)
        self.assertNotIn("embroidery", note)
        self.assertNotIn("v3 rules-path", note)

        # The note still does its job — tells renderer the list was reshaped
        self.assertIn("ПЕРЕФОРМИРОВАН", note)
        self.assertIn("CEFR", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
