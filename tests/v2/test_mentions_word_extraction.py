"""Sprint 20 — Stan 2026-05-19 prod log:

    «найди упоминания burger у Дойла»
    intent: word_contexts ✓     plan: clarify ✗
    («Уточни какое слово. Пример: «слово "fog"», «слово ajar».»)

The intent rule fired correctly. The drop was in the entity layer —
`_WORD_AFTER_VERB` had триггеры «этимолог / происхожден / соседств»
but not «упоминан / вхождени / встречаемост / mentions of /
occurrences of». So the word slot stayed None and the plan builder
clarified.

Two patches:
  (a) word extractor: add упоминан/вхождени/встречаемост/mentions of/
      occurrences of as trigger keywords in `_WORD_AFTER_VERB`.
  (b) intent classifier: broaden the word_contexts rule to fire on
      bare «упоминания X» / «occurrences of X» without the «найди»
      lead — natural human phrasings most users actually type.

Real systemic fix remains v3.2.0-alpha1 — but rules-path needs to
work until the v4 flag is on in prod.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class WordExtraction(unittest.TestCase):
    """Trigger words for word-after-verb extraction."""

    def test_stans_burger_query(self):
        e = ent_mod.extract("найди упоминания burger у Дойла")
        self.assertEqual(e.word, "burger")
        self.assertEqual(e.author_regex, "^Doyle,")

    def test_upominaniya_alone(self):
        e = ent_mod.extract("упоминания fog в текстах")
        self.assertEqual(e.word, "fog")

    def test_vhozhdeniya_slova(self):
        e = ent_mod.extract("вхождения слова whale")
        self.assertEqual(e.word, "whale")

    def test_vstrechaemost(self):
        e = ent_mod.extract("встречаемость duty у Austen")
        self.assertEqual(e.word, "duty")

    def test_mentions_of_en(self):
        e = ent_mod.extract("find mentions of fog in Dickens")
        self.assertEqual(e.word, "fog")

    def test_occurrences_of_en(self):
        e = ent_mod.extract("occurrences of whale in Moby Dick")
        self.assertEqual(e.word, "whale")


class IntentClassification(unittest.TestCase):
    """word_contexts intent must fire on bare «упоминания X» — not just
    «найди упоминания»."""

    def test_stans_burger_intent(self):
        m = int_mod.classify("найди упоминания burger у Дойла")
        self.assertEqual(m.label, "word_contexts")

    def test_bare_upominaniya_intent(self):
        m = int_mod.classify("упоминания fog в текстах")
        self.assertEqual(m.label, "word_contexts")

    def test_bare_occurrences_intent(self):
        m = int_mod.classify("occurrences of duty in Austen")
        self.assertEqual(m.label, "word_contexts")

    def test_bare_vhozhdeniya_intent(self):
        m = int_mod.classify("вхождения слова whale в Moby Dick")
        self.assertEqual(m.label, "word_contexts")


class EndToEndPlan(unittest.TestCase):
    """No clarify when both intent + word are correctly extracted."""

    def test_stans_burger_plan(self):
        q = "найди упоминания burger у Дойла"
        m = int_mod.classify(q)
        e = ent_mod.extract(q)
        p = plan_mod.build(m.label, e)
        self.assertFalse(p.needs_clarify)
        # word_contexts needs both word + author
        self.assertEqual(p.steps[-1].tool, "word_contexts")
        self.assertEqual(p.steps[-1].args.get("word"), "burger")


if __name__ == "__main__":
    unittest.main(verbosity=2)
