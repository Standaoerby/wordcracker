"""Sprint 17 — Round 7 closures + performance tests.

Three closeable Round 7 findings:
  1) Multi-author word_contexts (Q8: «ajar у Остин/Диккенса/Дойла»)
  2) Bare-word extraction after «примеры/examples»
  3) Intent classifier short-circuit (perf, no behavior change)"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.entities import Entities


class MultiAuthorWordContexts(unittest.TestCase):
    """Round 7 Q8: «ajar у Остин/Диккенса/Дойла» — only Austen
    got processed in v3.0. plan now emits N steps."""

    def test_slash_separated_dispatches_all_three(self):
        e = ent_mod.extract('слова "ajar" у Остин/Диккенса/Дойла')
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 3)
        regexes = [s.args["author_regex"] for s in p.steps]
        self.assertEqual(regexes, ["^Austen,", "^Dickens,", "^Doyle,"])

    def test_comma_and_and_separated(self):
        e = ent_mod.extract('словом fog у Мелвилла, Стивенсона и Конрада')
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 3)
        ids = [s.args["author_regex"] for s in p.steps]
        self.assertIn("^Melville,", ids)
        self.assertIn("^Stevenson,", ids)
        self.assertIn("^Conrad,", ids)

    def test_caps_at_4_total(self):
        """Bound time — even with 6 authors mentioned, no more than 4 steps."""
        e = Entities(word="fog", author_regex="^A,",
                     multi_author_regex=["^B,", "^C,", "^D,", "^E,", "^F,"])
        p = plan_mod.build("word_contexts", e)
        self.assertLessEqual(len(p.steps), 4)

    def test_optional_for_secondaries(self):
        """Failure on one author doesn't abort the whole chain."""
        e = Entities(word="fog", author_regex="^A,",
                     multi_author_regex=["^B,"])
        p = plan_mod.build("word_contexts", e)
        self.assertFalse(p.steps[0].optional)   # primary is required
        self.assertTrue(p.steps[1].optional)    # secondary is best-effort

    def test_single_author_unchanged(self):
        """Single-author phrasing still produces one step (no regression)."""
        e = Entities(word="ajar", author_regex="^Austen,")
        p = plan_mod.build("word_contexts", e)
        self.assertEqual(len(p.steps), 1)


class BareWordAfterExamples(unittest.TestCase):
    """Sprint 17 — Round 7 Q8 phrasing «примеры ajar у X» where the
    target word isn't quoted and «слова» keyword isn't present."""

    def test_russian_primery_extracts_word(self):
        e = ent_mod.extract("примеры ajar у Доyла")
        self.assertEqual(e.word, "ajar")

    def test_english_examples_of(self):
        e = ent_mod.extract("examples of fog in Stevenson")
        self.assertEqual(e.word, "fog")

    def test_primery_ispolzovaniya(self):
        e = ent_mod.extract("примеры использования sword у Толкина")
        self.assertEqual(e.word, "sword")

    def test_doesnt_grab_russian_genitive_noise(self):
        """«примеры авторов» / «примеры слов» should NOT extract a word —
        Latin-only capture filters Russian common-noun fillers."""
        e = ent_mod.extract("примеры авторов которые писали о море")
        self.assertIsNone(e.word)

    def test_doesnt_grab_word_substring(self):
        """«примеры слова X» — when «слова» trigger is present, the bare
        rule shouldn't conflict with _WORD_AFTER_KEY."""
        e = ent_mod.extract("примеры слова fog у Мелвилла")
        self.assertEqual(e.word, "fog")


class CriticSkipListExtension(unittest.TestCase):
    """Sprint 17 — Phase E/F/G intents are pure table echo. Numeric audit
    now catches the high-value fabrication class. The LLM critic adds
    3-5s of latency for no benefit on these intents."""

    EXPECTED_SKIPS = {
        "learning", "top_authors_books", "vocab_passport",
        "author_lookup", "corpus_extremum", "book_extremum",
        "topic_book_search", "book_pub_year", "book_lookup",
    }

    def test_all_phase_e_f_g_intents_skip(self):
        from scripts.v2.critic import _INTENT_SKIP_CRITIC
        for intent in self.EXPECTED_SKIPS:
            with self.subTest(intent=intent):
                self.assertIn(intent, _INTENT_SKIP_CRITIC)

    def test_skip_returns_trust_with_reason(self):
        """review() must short-circuit cleanly for skipped intents — no
        LLM call, returns a verdict with `(critic skipped for…)` summary."""
        from scripts.v2.critic import review
        v = review("any answer body", [{"tool": "find_book", "data": {}}],
                    intent="book_lookup")
        self.assertTrue(v.verified)
        self.assertIn("critic skipped", v.summary)
        self.assertIn("book_lookup", v.summary)

    def test_non_skipped_intent_still_attempts_critic(self):
        """author_compare should still go through the LLM critic — not in
        the skip list, behavior unchanged."""
        from scripts.v2.critic import _INTENT_SKIP_CRITIC
        self.assertNotIn("author_compare", _INTENT_SKIP_CRITIC)
        self.assertNotIn("author_metadata", _INTENT_SKIP_CRITIC)
        self.assertNotIn("word_contexts", _INTENT_SKIP_CRITIC)


class IntentClassifierCorrectness(unittest.TestCase):
    """After Sprint 17 short-circuit, classifier results must be
    bit-identical to the pre-optimization output. Spot-check the most
    common intent paths to lock the contract before short-circuit
    lands."""

    SAMPLES = [
        ("Что ты умеешь?",                          "introduction"),
        ("сколько книг в базе",                     "corpus_meta"),
        ("когда родился Doyle",                     "author_metadata"),
        ("фирменные слова Wodehouse",               "author_vocab"),
        ("на кого по стилю похож Doyle",            "author_closest"),
        ("сравни Wodehouse и Twain",                "author_compare"),
        ("уровень сложности Pride and Prejudice",   "book_readability"),
        ("найди упоминания fog у Диккенса",         "word_contexts"),
        ("найди книгу про викторианский Лондон",    "topic_book_search"),
        ("когда была опубликована Война и мир",     "book_pub_year"),
        ("какие книги у Doyle",                     "author_lookup"),
        ("самый плодовитый автор",                  "corpus_extremum"),
        ("самая популярная книга",                  "book_extremum"),
        ("топ-10 авторов по числу книг",            "top_authors_books"),
    ]

    def test_canonical_classifications_stable(self):
        for q, expected in self.SAMPLES:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, expected,
                                  msg=f"classify({q!r}) → {m.label!r}, expected {expected!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
