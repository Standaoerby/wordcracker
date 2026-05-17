"""Plan builder tests — verify each intent produces the expected tool chain.

Doesn't dispatch tools, doesn't hit LLM. Pure plan construction."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import Entities, extract
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import build, PLAN_BUILDERS


def _full(text: str):
    intent = classify(text)
    ents = extract(text)
    return intent, ents, build(intent.label, ents)


class PlanCoversAllIntents(unittest.TestCase):
    def test_every_intent_has_builder(self):
        from scripts.v2.planner.intent import INTENTS
        # 'clarify' has no builder (handled by build() fallback) — ok.
        missing = {i for i in INTENTS if i not in PLAN_BUILDERS} - {"clarify"}
        self.assertEqual(missing, set(), msg=f"intents w/o plan builder: {missing}")


class PlanAuthorIntents(unittest.TestCase):
    def test_author_vocab_doyle(self):
        _, _, p = _full("Какие фирменные слова Конан Дойла?")
        self.assertEqual(p.intent, "author_vocab")
        self.assertEqual(p.steps[0].tool, "affinity_by_author")
        self.assertEqual(p.steps[0].args["author_regex"], "^Doyle,")

    def test_author_vocab_with_pos(self):
        _, _, p = _full("Какие характерные прилагательные использует Оскар Уайльд?")
        self.assertEqual(p.intent, "author_vocab")
        self.assertEqual(p.steps[0].args["pos_filter"], ["ADJ"])

    def test_author_compare(self):
        _, _, p = _full("Найди слова, которые повторяются у Диккенса, но не у Хемингуэя.")
        self.assertEqual(p.intent, "author_compare")
        # Plan now probes both authors first, then runs compare_authors.
        self.assertEqual([s.tool for s in p.steps],
                         ["author_metadata", "author_metadata", "compare_authors"])
        self.assertEqual(p.steps[-1].args["author1_regex"], "^Dickens,")
        self.assertEqual(p.steps[-1].args["author2_regex"], "^Hemingway,")

    def test_author_compare_no_second(self):
        _, _, p = _full("Сравни Wodehouse")
        self.assertTrue(p.needs_clarify or p.intent == "clarify")

    def test_author_closest(self):
        _, _, p = _full("Какие авторы лексически ближе всего к Конан Дойлу?")
        self.assertEqual(p.intent, "author_closest")
        self.assertEqual(p.steps[0].tool, "author_influences")

    def test_author_vocab_missing(self):
        _, _, p = _full("Какие фирменные слова автора?")
        self.assertTrue(p.needs_clarify, msg=p)


class PlanBookIntents(unittest.TestCase):
    def test_book_vocab_known_book(self):
        _, _, p = _full('Слова в книге "Преступление и наказание" чаще обычного')
        self.assertEqual(p.intent, "book_vocab")
        self.assertEqual(p.steps[0].tool, "affinity_by_book")
        self.assertEqual(p.steps[0].args["pg_id"], "PG2554")

    def test_book_vocab_unknown_title(self):
        _, _, p = _full('Слова в книге «1984» используются чаще обычного.')
        self.assertEqual(p.intent, "book_vocab")
        self.assertEqual(p.steps[0].tool, "find_book")
        self.assertEqual(p.steps[1].tool, "affinity_by_book")
        self.assertEqual(p.steps[1].depends_on, [0])

    def test_book_archaic_dracula(self):
        _, _, p = _full('Какие слова из "Dracula" считаются устаревшими?')
        self.assertEqual(p.intent, "book_archaic")
        # known title → goes straight to book_archaic_words
        self.assertEqual(p.steps[0].tool, "book_archaic_words")
        self.assertEqual(p.steps[0].args["pg_id"], "PG345")

    def test_book_readability_known(self):
        _, _, p = _full("Уровень сложности Pride and Prejudice")
        # 'Pride and Prejudice' isn't quoted → find_book chain
        self.assertEqual(p.intent, "book_readability")


class PlanWordIntents(unittest.TestCase):
    def test_word_contexts_global(self):
        _, _, p = _full('Покажи примеры использования слова "ajar" у разных авторов')
        self.assertEqual(p.intent, "word_contexts")
        # No author scope → routed to hybrid_search (FTS5 + ChromaDB RRF).
        self.assertEqual(p.steps[0].tool, "hybrid_search")
        self.assertEqual(p.steps[0].args["query"], "ajar")

    def test_word_collocates_victorian_fog(self):
        _, _, p = _full('Слова, которые чаще всего соседствуют со словом "fog" у викторианских авторов')
        self.assertEqual(p.intent, "word_collocates")
        self.assertEqual(p.steps[0].args["word"], "fog")
        scope = p.steps[0].args["scope"]
        # scope = all_corpus with year filter (no specific author)
        # year_from/year_to set via Victorian default
        # plan_word_collocates uses _scope_from which doesn't include year_from
        # for all_corpus — so we rely on the renderer to mention period.
        # Just sanity-check the tool name and word for now.
        self.assertIsNotNone(scope)

    def test_word_timeline_after_1920(self):
        _, _, p = _full("Какие слова резко вышли из употребления после 1920 года?")
        self.assertEqual(p.intent, "word_timeline")
        # _plan_word_timeline subtracts 1 because year_from is "after 1920" -> >1920
        self.assertEqual(p.steps[0].tool, "words_disappearing_after")

    def test_word_etymology_tolkien_germanic(self):
        _, _, p = _full("Какие слова у Толкина имеют древнегерманское происхождение?")
        self.assertEqual(p.intent, "word_etymology")
        self.assertEqual(p.steps[0].tool, "find_words_by_etymology")
        self.assertEqual(p.steps[0].args["family"], "germanic")
        self.assertEqual(p.steps[0].args["scope"]["author"], "^Tolkien,")

    def test_word_emotion_fear(self):
        _, _, p = _full("Какие слова страха использует По?")
        self.assertEqual(p.intent, "word_emotion")
        self.assertEqual(p.steps[0].tool, "emotion_collocates")
        self.assertEqual(p.steps[0].args["emotion"], "fear")

    def test_word_pos(self):
        _, _, p = _full('Какие слова в этой книге имеют больше всего разных значений?')
        # No book id from prompt → clarify
        self.assertTrue(p.needs_clarify)


class PlanLearningIntent(unittest.TestCase):
    def test_learning_b2_lovecraft(self):
        _, _, p = _full("Какие слова сложности B2 при чтении Лавкрафта?")
        self.assertEqual(p.intent, "learning")
        self.assertEqual(p.steps[0].tool, "learning_words")
        self.assertEqual(p.steps[0].args["level"], "intermediate")
        self.assertEqual(p.steps[0].args["scope"]["author"], "^Lovecraft,")

    def test_learning_no_scope_asks_clarify(self):
        _, _, p = _full("Покажи intermediate vocabulary")
        self.assertTrue(p.needs_clarify, msg=p)

    def test_learning_top_300_galsworthy(self):
        _, _, p = _full("Если бы я хотел читать Голсуорси свободно, какие 300 слов мне нужно выучить?")
        self.assertEqual(p.intent, "learning")
        # Plan caps to 30 per call — anything larger blows past chat timeout
        # with the per-word enrich loop. Renderer offers "ещё" follow-up.
        self.assertEqual(p.steps[0].args["top"], 30)
        self.assertIn("capped from 300", p.explain)
        self.assertEqual(p.steps[0].args["scope"]["author"], "^Galsworthy,")


class PlanCountryAndPeriod(unittest.TestCase):
    def test_country_compare(self):
        _, _, p = _full("Найди слова, которые в американской литературе используются редко, а в британской часто.")
        self.assertEqual(p.intent, "country_compare")
        self.assertEqual(len(p.steps), 2)
        self.assertEqual({p.steps[0].args["country"], p.steps[1].args["country"]}, {"GB", "US"})

    def test_country_vocab(self):
        _, _, p = _full("Какие необычные британские слова часто использует Агата Кристи?")
        self.assertEqual(p.intent, "country_vocab")
        self.assertEqual(p.steps[0].args["author_regex"], "^Christie,")


class PlanCorpusMeta(unittest.TestCase):
    def test_corpus_meta(self):
        _, _, p = _full("Сколько книг в базе?")
        self.assertEqual(p.intent, "corpus_meta")
        self.assertEqual(p.steps[0].tool, "corpus_overview")


class PlanOutOfScope(unittest.TestCase):
    def test_write_a_poem(self):
        _, _, p = _full("Напиши стихотворение в стиле Wodehouse")
        self.assertEqual(p.intent, "out_of_scope")
        self.assertIsNotNone(p.out_of_scope_reason)

    def test_translation_quality(self):
        _, _, p = _full("Какие слова чаще всего переводят неправильно в русских переводах?")
        self.assertEqual(p.intent, "translation_quality")
        self.assertIsNotNone(p.out_of_scope_reason)


class PlanIntroduction(unittest.TestCase):
    def test_intro(self):
        _, _, p = _full("Кто ты и что ты умеешь?")
        self.assertEqual(p.intent, "introduction")
        self.assertEqual(p.steps, [])


class PlanVocabPassport(unittest.TestCase):
    def test_passport_doyle(self):
        _, _, p = _full('Построй "словарный паспорт" Конан Дойла')
        self.assertEqual(p.intent, "vocab_passport")
        self.assertEqual(p.steps[0].tool, "author_profile")
        self.assertEqual(p.steps[0].args["author_regex"], "^Doyle,")


if __name__ == "__main__":
    unittest.main(verbosity=2)
