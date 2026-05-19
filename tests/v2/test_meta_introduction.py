"""Sprint 19+ — meta-questions about the service route to introduction.

Stan 2026-05-19: naïve users ask «что это за сервис?», «кому ты
подойдёшь?», «это бесплатный сервис?» — all fell to clarify with the
generic «фирменные слова Wodehouse» example menu. These are
introduction-class queries about the SERVICE itself, not about the
corpus. Should hit the static intro renderer (capabilities + value
prop + 4 starter examples).

Plus: refine the «who is X» author_metadata rule with a negative
lookahead so «who is this for» doesn't get stolen."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import intent as int_mod


class MetaServiceQuestions(unittest.TestCase):

    POSITIVE = [
        # Stan's three verbatim queries
        "что это за сервис?",
        "кому ты подойдёшь и для чего ты нужен?",
        "это бесплатный сервис?",
        # «как работаешь» class
        "как ты работаешь?",
        "как ты устроен?",
        "how do you work?",
        "how does this work?",
        # «для кого» class
        "для кого ты?",
        "кому ты нужен?",
        "зачем ты нужен?",
        "who is this for?",
        "who is this service for?",
        # «что за сервис» class
        "what is this service?",
        "what is this tool?",
        # pricing
        "is this free?",
        "pricing",
        "сколько стоит использование?",
    ]

    NEGATIVE = [
        # Real author probe — must stay author_metadata
        ("who is Hemingway?", "author_metadata"),
        ("who was Doyle?", "author_metadata"),
        ("кто такой Шекспир?", "author_metadata"),
        # Other intents shouldn't be stolen
        ("архаизмы в Dracula", "book_archaic"),
        ("фирменные слова Wodehouse", "author_vocab"),
    ]

    def test_meta_questions_route_to_introduction(self):
        for q in self.POSITIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "introduction",
                    msg=f"{q!r} → {m.label!r}, expected introduction")

    def test_real_intents_not_stolen(self):
        for q, expected in self.NEGATIVE:
            with self.subTest(query=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, expected,
                    msg=f"{q!r} → {m.label!r}, expected {expected!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
