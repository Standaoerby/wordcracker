"""Round 9 naive-user UX gap closure.

10 queries from a first-time user without docs (Round 9 simulated by
external tester). v3.0.2 baseline was 3/10 strict; Sprint 18+
naive-pass adds 7 closeable rules. Lock all 10 in so future intent
refactors don't silently regress."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod


class Round9NaiveUserUX(unittest.TestCase):
    """Each row: (query, expected_intent, expected_plan_tool_or_None)."""

    CASES = [
        ("привет",                                "introduction",       None),
        ("что ты можешь рассказать интересного?", "introduction",       None),
        ("посоветуй интересную книжку",           "book_recommendation","top_books_by_downloads"),
        ("найди мне рассказ про море",            "topic_book_search",  "find_book_by_topic"),
        ("что значит слово gloomy?",              "word_etymology",     "word_etymology"),
        ("у тебя есть Гарри Поттер?",             "book_lookup",        "find_book"),
        ("посоветуй книгу для 12 лет",            "book_recommendation","top_books_by_downloads"),
        ("сколько страниц в Войне и Мире?",       "book_readability",   "book_readability"),
        ("переведи мне фразу to be or not to be", "out_of_scope",       None),
        ("кто такой ШеКспир?",                    "author_metadata",    "author_metadata"),
    ]

    def test_all_10_classify_correctly(self):
        for q, expected_intent, _ in self.CASES:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, expected_intent,
                    msg=f"{q!r} → {m.label!r}, expected {expected_intent!r}")

    def test_plans_dispatch_correctly(self):
        for q, expected_intent, expected_tool in self.CASES:
            with self.subTest(query=q):
                e = ent_mod.extract(q)
                p = plan_mod.build(expected_intent, e)
                if expected_tool is None:
                    # introduction / out_of_scope — no tool steps
                    continue
                self.assertGreater(len(p.steps), 0,
                    msg=f"{q!r} produced clarify, expected tool {expected_tool!r}")
                tools = [s.tool for s in p.steps]
                self.assertIn(expected_tool, tools,
                    msg=f"{q!r} dispatched to {tools!r}, expected {expected_tool!r}")


class Round9SpecificCoverage(unittest.TestCase):
    """Per-fix targeted assertions — even if the broad classification
    test above passes, lock the specific phrasings that motivated each
    rule. Future regex refactors will keep these passing or surface
    immediately."""

    def test_kto_takoj_x_routes_to_author_metadata(self):
        for q in [
            "кто такой Шекспир",
            "кто такой ШеКспир?",   # mixed case (Round 9 N10)
            "кто такая Остин",
            "who is Hemingway",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "author_metadata")

    def test_naidi_rasskaz_pro_x(self):
        """«рассказ/повесть/short story» теперь покрывается topic_book_search."""
        for q in [
            "найди мне рассказ про море",
            "посоветуй повесть о любви",
            "find a short story about ghosts",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "topic_book_search")

    def test_u_tebja_est_x(self):
        for q in [
            "у тебя есть Гарри Поттер?",
            "есть ли у тебя Dracula?",
            "do you have Hamlet?",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_lookup")

    def test_chto_znachit_slovo_x(self):
        for q in [
            "что значит слово gloomy?",
            "определи слово sword",
            "что означает слово ajar",
            "what does sword mean",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "word_etymology")

    def test_perevedi_frazu(self):
        for q in [
            "переведи мне фразу to be or not to be",
            "переведи цитату",
            "translate this phrase",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "out_of_scope")

    def test_pages_in_book(self):
        for q in [
            "сколько страниц в Войне и Мире?",
            "сколько страниц в Dracula",
            "how many pages in Hamlet",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "book_readability")

    def test_vague_curiosity(self):
        for q in [
            "что ты можешь рассказать интересного?",
            "удиви меня",
            "расскажи что-нибудь",
            "tell me something cool",
            "surprise me",
        ]:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "introduction")


if __name__ == "__main__":
    unittest.main(verbosity=2)
