"""Tests for the v2 history backfill — multi-turn entity threading."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract
from scripts.v2.planner.history import (
    _looks_like_followup,
    _scan_history_for_entities,
    merge_with_history,
)


class FollowupTriggers(unittest.TestCase):
    def test_примеры_такого(self):
        self.assertTrue(_looks_like_followup("приведи три примера такого использования"))

    def test_эти_слова(self):
        self.assertTrue(_looks_like_followup("а эти слова где встречаются?"))

    def test_more_examples(self):
        self.assertTrue(_looks_like_followup("more examples of this"))

    def test_еще(self):
        self.assertTrue(_looks_like_followup("ещё 30 слов из той же книги"))

    def test_standalone_question(self):
        # A complete query — no follow-up trigger.
        self.assertFalse(_looks_like_followup(
            "Какие фирменные слова у Wodehouse?"
        ))


class HistoryScanForEntities(unittest.TestCase):
    def test_picks_latest_user_message_with_entities(self):
        history = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Покажи фирменные слова Wodehouse"},
            {"role": "assistant", "content": "wicket, blighter, ..."},
        ]
        e = _scan_history_for_entities(history)
        self.assertIsNotNone(e)
        self.assertEqual(e.author_regex, "^Wodehouse,")

    def test_skips_assistant_messages(self):
        history = [
            {"role": "user", "content": "что-то непонятное без сущностей"},
            {"role": "assistant", "content": "Книга Pride and Prejudice"},
        ]
        # Only user-side carries entities; this user message has none, so None.
        e = _scan_history_for_entities(history)
        self.assertIsNone(e)

    def test_empty_history(self):
        self.assertIsNone(_scan_history_for_entities([]))


class MergeBackfill(unittest.TestCase):
    def test_followup_pulls_author_from_history(self):
        history = [
            {"role": "user", "content": "Покажи слова Достоевского"},
            {"role": "assistant", "content": "1. крестьянин 2. душа ..."},
        ]
        current = extract("приведи три примера такого использования")
        merged = merge_with_history(current, history, current.raw_misc["raw_text"])
        self.assertEqual(merged.author_regex, "^Dostoyevsky,")

    def test_no_trigger_keeps_current(self):
        history = [
            {"role": "user", "content": "Слова Достоевского"},
        ]
        current = extract("кто такой Конан Дойл?")
        merged = merge_with_history(current, history, current.raw_misc["raw_text"])
        # No follow-up trigger, current's own author (Doyle) stays unchanged
        # and the prior Dostoyevsky doesn't bleed in.
        self.assertEqual(merged.author_regex, "^Doyle,")

    def test_existing_field_not_overwritten(self):
        history = [
            {"role": "user", "content": "Слова Достоевского"},
        ]
        current = extract("приведи примеры из Pride and Prejudice ещё")
        merged = merge_with_history(current, history, current.raw_misc["raw_text"])
        # Current already names a book → backfill doesn't override.
        self.assertEqual(merged.book_id, "PG1342")
        # Author was unset in current and Dostoyevsky was in history → filled.
        self.assertEqual(merged.author_regex, "^Dostoyevsky,")

    def test_book_id_threads_through(self):
        history = [
            {"role": "user", "content": "Архаизмы в Dracula"},
            {"role": "assistant", "content": "ye, nay, amongst..."},
        ]
        current = extract("приведи примеры такого использования")
        merged = merge_with_history(current, history, current.raw_misc["raw_text"])
        self.assertEqual(merged.book_id, "PG345")


if __name__ == "__main__":
    unittest.main(verbosity=2)
