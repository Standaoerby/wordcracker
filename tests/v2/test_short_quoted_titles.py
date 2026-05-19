"""Sprint 19+ hotfix — context-aware short-quoted-title heuristic.

Stan 2026-05-19 caught «эмоциональный профиль "Politics" Аристотеля»
and «"Leviathan"» falling to clarify. Both are single-word quotes
<12 chars long — entity extractor was applying a defensive «short
single-word = target word, not book» rule, designed for «слова
"ajar"». But in book-context queries (профиль / архаизм / уровень
сложности / стиль / характерные / vocab) we want them as book titles.

Fix: keep the conservative rule, but bypass when the surrounding
text carries an explicit book-context trigger."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class ShortQuotedTitleInBookContext(unittest.TestCase):
    """When a book-context trigger is present, short quoted tokens
    become book_title candidates (planner chains via find_book)."""

    CASES_BOOK_TITLE = [
        # (query, expected_title)
        ('эмоциональный профиль "Politics" Аристотеля', "Politics"),
        ('эмоциональный профиль "Leviathan"',           "Leviathan"),
        ('уровень сложности "Iliad"',                    "Iliad"),
        ('архаизмы в "Beowulf"',                         "Beowulf"),
        ('характерные слова в "Faust"',                  "Faust"),
        ('сравни "Hamlet" и "Macbeth"',                  "Hamlet"),
    ]

    def test_all_keep_book_title(self):
        for q, expected_title in self.CASES_BOOK_TITLE:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                self.assertEqual(
                    e.book_title, expected_title,
                    msg=f"{q!r} → book_title={e.book_title!r}, "
                        f"expected {expected_title!r}",
                )

    def test_emotion_query_dispatches_find_book_chain(self):
        """End-to-end: book_emotion intent + unresolved title → 2-step
        chain via find_book."""
        e = ent_mod.extract('эмоциональный профиль "Politics"')
        p = plan_mod.build("book_emotion", e)
        self.assertFalse(p.needs_clarify)
        tools = [s.tool for s in p.steps]
        self.assertEqual(tools, ["find_book", "book_emotion_profile"])


class WordContextStillWorks(unittest.TestCase):
    """The original heuristic still protects «слова "ajar"» from
    being classified as a book — no book-context trigger means short
    quoted tokens stay as target words."""

    CASES_WORD_TARGET = [
        ('примеры использования слова "ajar"',  "ajar"),
        ('найди упоминания "fog"',              "fog"),
        ('этимология слова "sword"',            "sword"),
        ('контексты "haze"',                    "haze"),
    ]

    def test_short_quoted_no_book_context_stays_word(self):
        for q, expected_word in self.CASES_WORD_TARGET:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                self.assertIsNone(e.book_title,
                    msg=f"{q!r} mistakenly extracted book_title={e.book_title!r}")
                self.assertEqual(e.word, expected_word)


class LongQuotedAlwaysBook(unittest.TestCase):
    """Multi-word or >12-char quoted titles always stay as book titles
    (the short-token defensive heuristic never kicked in for them)."""

    def test_multiword(self):
        e = ent_mod.extract('что-то про "Pride and Prejudice"')
        self.assertEqual(e.book_title, "Pride and Prejudice")

    def test_long_single_word(self):
        e = ent_mod.extract('"Frankenstein" — про что?')
        self.assertEqual(e.book_title, "Frankenstein")


if __name__ == "__main__":
    unittest.main(verbosity=2)
