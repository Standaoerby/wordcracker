"""Sprint 19+ — extended book_emotion phrasings + case-sensitive guards.

Stan 2026-05-19: «эмоции и настроение в Frankenstein» fell to clarify.
Root cause: the strict «эмоции\\s+в» rule didn't allow «и настроение»
inserted between «эмоции» and «в». Fix: allow up to 40 chars of
filler. Bonus rule for bare-genitive «тональность X» / «атмосфера Y»
using inline `(?-i:[A-Z])` to enforce capital-letter proper-noun guard.

Critical side-effect: classify() no longer lowercases input
preemptively — case-sensitive checks (`(?-i:[A-Z])`) only work on
original-case text, so we dropped `.lower()` from classify(). All
existing rules use `re.IGNORECASE` so case-insensitive matching is
preserved by-default."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import intent as int_mod


class BookEmotionExtendedPhrasings(unittest.TestCase):

    POSITIVE = [
        # Stan's verbatim
        "эмоции и настроение в Frankenstein",
        # With «в» preposition
        "настроение в Dracula",
        "тон в Pride and Prejudice",
        "атмосфера в Wuthering Heights",
        "mood in Hamlet",
        # Bare genitive (no preposition, but capital letter follows)
        "тональность Frankenstein",
        "атмосфера Hamlet",
        "настроение Dracula",
        # Conjunction inserted between emotion-keyword and «в»
        "эмоции, тон и атмосфера в Pride and Prejudice",
        # Original rules still work
        "эмоциональный профиль Dracula",
        "эмоции в Frankenstein",
        "sentiment in Dracula",
    ]

    NEGATIVE = [
        # Bare-genitive rule must NOT fire on lowercase common nouns
        "тон героя",
        "атмосфера комнаты",
        "настроение героя",
        "атмосфера праздника",
        # Word-emotion path (slova straha) — different intent
        "слова страха у Лавкрафта",
        "fear words in Poe",
    ]

    def test_positive_phrasings(self):
        for q in self.POSITIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_emotion",
                    msg=f"{q!r} → {m.label!r}, expected book_emotion")

    def test_negative_phrasings(self):
        for q in self.NEGATIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertNotEqual(m.label, "book_emotion",
                    msg=f"{q!r} mistakenly classified as book_emotion")


class CaseSensitiveClassifierOriginalCasePreserved(unittest.TestCase):
    """Sprint 19+: classify() no longer lowercases input. Confirms
    `(?-i:[A-Z])` and similar case-sensitive guards work, and that all
    existing case-insensitive (via re.IGNORECASE) rules still fire on
    arbitrary input case."""

    def test_mixed_case_existing_rule(self):
        # «ФиРмЕнНыЕ слова Wodehouse» — should still match author_vocab
        # via re.IGNORECASE on the existing rule.
        m = int_mod.classify("ФиРмЕнНыЕ слова Wodehouse")
        self.assertEqual(m.label, "author_vocab")

    def test_lowercase_proper_noun_doesnt_match_bare_genitive(self):
        # The bare-genitive rule requires uppercase letter after the
        # emotion-keyword. Lowercase «frankenstein» (user mistyped)
        # should fall to clarify, NOT book_emotion.
        m = int_mod.classify("тональность frankenstein")
        self.assertNotEqual(m.label, "book_emotion")


if __name__ == "__main__":
    unittest.main(verbosity=2)
