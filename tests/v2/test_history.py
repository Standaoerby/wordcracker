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


class ReRankFollowup(unittest.TestCase):
    """Stan 2026-05-18 round 2: after «дай фирменные слова пушкина» returns
    a table, «отсортируй их по количеству упоминаний» used to clarify-out.
    The trigger now feeds infer_followup_intent through the history to
    pick up the prior intent and re-run the plan (cache hit → LLM
    re-renders sorted)."""

    def test_otsortiruy_returns_prior_intent(self):
        from scripts.v2.planner.history import infer_followup_intent
        history = [
            {"role": "user", "content": "дай фирменные слова пушкина"},
            {"role": "assistant", "content": "gavril 5445 | lisaveta 4022 ..."},
        ]
        self.assertEqual(
            infer_followup_intent("отсортируй их по количеству упоминаний",
                                   history),
            "author_vocab",
        )

    def test_rerank_synonyms(self):
        from scripts.v2.planner.history import infer_followup_intent
        history = [
            {"role": "user", "content": "сравни By и Lovecraft"},
            {"role": "assistant", "content": "..."},
        ]
        for q in ("переразложи в другом порядке",
                  "по убыванию",
                  "sort them by frequency"):
            with self.subTest(q=q):
                self.assertEqual(
                    infer_followup_intent(q, history), "author_compare",
                )

    def test_rerank_without_history_returns_none(self):
        from scripts.v2.planner.history import infer_followup_intent
        self.assertIsNone(infer_followup_intent("отсортируй", history=None))


class CopyrightRefusal(unittest.TestCase):
    """Stan 2026-05-18 round 2: copyright refusal used to be «отсутствует
    в корпусе». Now structured: metadata-only + analog hint, with
    leading-the fuzzy match for «Old Man and the Sea»."""

    def test_lotr_returns_oos_with_analog(self):
        from scripts.v2.planner.entities import extract
        from scripts.v2.planner.plan import _copyright_refusal_if_book_under_copyright
        e = extract('слова из "The Lord of the Rings"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent, "out_of_scope")
        # Sprint 21 B102 — refusal now mentions BOTH options: metadata
        # OR local upload (fair use). Check the key phrases.
        self.assertIn("copyright", plan.out_of_scope_reason)
        self.assertIn("public-domain", plan.out_of_scope_reason)
        self.assertIn("Мета-информация", plan.out_of_scope_reason)
        self.assertIn("загруженной локально", plan.out_of_scope_reason)
        self.assertIn("Моррис", plan.out_of_scope_reason)

    def test_old_man_without_the_prefix_still_matches(self):
        from scripts.v2.planner.entities import extract
        from scripts.v2.planner.plan import _copyright_refusal_if_book_under_copyright
        e = extract('фирменные слова из "Old Man and the Sea"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan, msg="leading-the fuzzy match should catch this")
        self.assertIn("Twain", plan.out_of_scope_reason)


class HighTranslitCorpusFloor(unittest.TestCase):
    """Stan 2026-05-18: «фирменные слова пушкина» used to return character
    names (Gavril/Lisaveta/Korsakoff/Pushkin himself, kibitka, mossoo,
    beaupre) because min_corpus_count=100 was too soft for transliterated
    proper nouns. Russian authors now get min_corpus_count=1500."""

    def test_pushkin_high_floor(self):
        from scripts.v2.planner.plan import _auto_min_corpus_count
        e = extract("дай фирменные слова пушкина")
        self.assertEqual(_auto_min_corpus_count(e), 1500)

    def test_tolstoy_high_floor(self):
        from scripts.v2.planner.plan import _auto_min_corpus_count
        self.assertEqual(_auto_min_corpus_count(extract("слова Толстого")), 1500)

    def test_english_author_normal_floor(self):
        from scripts.v2.planner.plan import _auto_min_corpus_count
        # English author with no special filtering — uses default 500
        self.assertEqual(_auto_min_corpus_count(extract("слова Wodehouse")), 500)


class ContextSwapFollowup(unittest.TestCase):
    """Stan round 5 critical: «теперь у Диккенса» / «а у пушкина?» /
    «давай теперь Толстого» — context-swap follow-ups that mention a new
    entity but inherit the previous turn's intent."""

    def test_teper_u_X(self):
        from scripts.v2.planner.history import (
            _looks_like_followup, _is_context_swap, infer_followup_intent,
        )
        history = [
            {"role": "user", "content": "архаизмы в Dracula"},
            {"role": "assistant", "content": "..."},
        ]
        self.assertTrue(_looks_like_followup("теперь у Диккенса"))
        self.assertTrue(_is_context_swap("теперь у Диккенса"))
        self.assertEqual(infer_followup_intent("теперь у Диккенса", history),
                          "book_archaic")

    def test_a_u_X(self):
        from scripts.v2.planner.history import infer_followup_intent
        history = [{"role": "user", "content": "фирменные слова Doyle"},
                   {"role": "assistant", "content": "..."}]
        self.assertEqual(infer_followup_intent("а у пушкина?", history),
                          "author_vocab")

    def test_davai_teper(self):
        from scripts.v2.planner.history import infer_followup_intent
        history = [{"role": "user", "content": "сравни Doyle и Wodehouse"},
                   {"role": "assistant", "content": "..."}]
        self.assertEqual(
            infer_followup_intent("давай теперь Толстого", history),
            "author_compare",
        )

    def test_no_history_no_inheritance(self):
        from scripts.v2.planner.history import infer_followup_intent
        self.assertIsNone(infer_followup_intent("теперь у Диккенса", history=None))
        self.assertIsNone(infer_followup_intent("теперь у Диккенса", history=[]))


class PoPrepositionCollision(unittest.TestCase):
    """Stan round 5 corner-stone bug: «по» preposition collided with the
    «по» alias of «Poe», 100% of «дай статистику по X» returned Poe."""

    def test_po_preposition_not_extracted_as_author(self):
        from scripts.v2.planner.entities import extract
        # When «по» is followed by an author name or noun, it's a
        # preposition, not the author Poe.
        e = extract("дай статистику по Wodehouse")
        self.assertEqual(e.author_regex, "^Wodehouse,",
                          msg="«по» preposition shouldn't pick Poe over Wodehouse")
        e = extract("дай статистику по Чехову")
        self.assertEqual(e.author_regex, "^Chekhov,")
        e = extract("статистика по корпусу")
        self.assertIsNone(e.author_regex)
        e = extract("найди слова по теме природы")
        self.assertIsNone(e.author_regex)

    def test_real_poe_still_works(self):
        from scripts.v2.planner.entities import extract
        # Real Poe references — capitalized or clearly proper-noun
        self.assertEqual(extract("фирменные слова По").author_regex, "^Poe,")
        self.assertEqual(extract("сравни По и Лавкрафта").author_regex, "^Poe,")
        self.assertEqual(extract("Эдгар Аллан По").author_regex, "^Poe,")
        self.assertEqual(extract("poe").author_regex, "^Poe,")


if __name__ == "__main__":
    unittest.main(verbosity=2)
