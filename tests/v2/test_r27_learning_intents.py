"""R-27 WP1 (B106 + P1.2) — first-session & learning intents.

Прод-жалобы из /feedback (01.06 + 10.06, уже на 2.6.47):
  · «какие книги почитать, если у меня уровень B2» → intent=learning
    (0.7, голое CEFR-правило), tool_calls=[], ~40s → canned «не
    получилось разобрать».
  · «я учу английский, с чего начать?» — то же; нота юзера: «надо
    советовать книгу с минимальным порогом вхождения».

Фикс (композиция СУЩЕСТВУЮЩИХ тулов, новых fingerprinted-врапперов нет):
  1. Интент learning_books → план top_books_by_downloads(top=10) +
     book_readability × 10 (router-injected pg_id@rank) + CEFR-фильтр
     на рендере.
  2. Meta-pack §10: «у вас есть <автор>» → author_metadata-делегация в
     _plan_book_lookup; обратный порядок «какой автор самый популярный»
     / «какая книга самая длинная» → corpus_extremum / book_extremum.
  3. Fail-fast: authoritative_clarify на осознанных clarify
     (_plan_learning unscoped, _plan_book_extremum length-extremum) —
     ответ за секунды вместо v4 LLM ~40s.

R2: интент-позитивы падают на до-фиксовом коде (B106-фразы уходили в
learning/clarify); негативы защищают learning_words, book_recommendation
(Q30) и обычный book-поиск от угона.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import ToolResult
from scripts.v2.budget import INTENT_BUDGETS_S, RequestBudget
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.builders.book import (
    _plan_book_extremum,
    _plan_book_lookup,
)
from scripts.v2.planner.builders.learning import (
    _LEARNING_BOOKS_POOL,
    _plan_learning,
    _plan_learning_books,
)
from scripts.v2.planner.entities import Entities
from scripts.v2.planner.router import _inject


def _e(text: str, **kw) -> Entities:
    return Entities(raw_misc={"raw_text": text}, **kw)


class LearningBooksIntent(unittest.TestCase):
    """B106 позитивы — обе прод-формулировки + RU/EN варианты."""

    def test_b106_books_for_b2(self):
        m = int_mod.classify("какие книги почитать, если у меня уровень B2")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_b106_uchu_angliyskiy_s_chego_nachat(self):
        m = int_mod.classify("я учу английский, с чего начать?")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_knigi_dlya_urovnya(self):
        m = int_mod.classify("книги для уровня B2")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_legkie_knigi_dlya_izuchayushchih(self):
        m = int_mod.classify("лёгкие книги для изучающих английский")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_knigi_pochitat_uchu_angliyskiy(self):
        m = int_mod.classify("какие книги почитать, если я учу английский?")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_en_books_at_b2_level(self):
        m = int_mod.classify("what books to read at B2 level?")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_en_easy_books_for_learners(self):
        m = int_mod.classify("easy books for English learners")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    # ---- негативы: не угонять чужие интенты ----

    def test_plain_pochitat_not_learning_books(self):
        """«какие книги почитать» БЕЗ learning-контекста — обычный
        book-поиск / clarify, НЕ learning_books."""
        m = int_mod.classify("какие книги почитать?")
        self.assertNotEqual(m.label, "learning_books", m.matched_pattern)

    def test_slova_urovnya_b2_still_learning(self):
        """Intent-конфьюжн с learning_words: СЛОВА уровня — по-прежнему
        learning."""
        m = int_mod.classify("слова уровня B2")
        self.assertEqual(m.label, "learning", m.matched_pattern)

    def test_q7_words_from_book_still_learning(self):
        m = int_mod.classify(
            '20 слов уровня intermediate из "Pride and Prejudice"')
        self.assertEqual(m.label, "learning", m.matched_pattern)

    def test_slova_iz_knigi_still_learning(self):
        """«слова … из книги» содержит токен «книги», но это запрос про
        СЛОВА — learning_books угонять не должен. (Вариант с заглавным
        титулом после «книги» уходит в author_lookup через E1b
        bare-genitive ещё ДО этой правки — вне скоупа WP1.)"""
        m = int_mod.classify("слова уровня B2 из книги")
        self.assertEqual(m.label, "learning", m.matched_pattern)

    def test_q30_recommendation_unbroken(self):
        """Q30 остаётся на book_recommendation (регресс-гард)."""
        m = int_mod.classify(
            "Какие произведения подойдут для читателя уровня B2: "
            "не слишком простые, но без плотного слоя архаизмов?"
        )
        self.assertEqual(m.label, "book_recommendation", m.matched_pattern)


class LearningBooksPlan(unittest.TestCase):
    """План = пул + book_readability fan-out, без новых тулов."""

    def test_plan_shape(self):
        p = _plan_learning_books(
            _e("какие книги почитать, если у меня уровень B2",
               level="intermediate"))
        self.assertEqual(p.intent, "learning_books")
        self.assertFalse(p.needs_clarify)
        self.assertEqual(len(p.steps), 1 + _LEARNING_BOOKS_POOL)
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")
        self.assertEqual(p.steps[0].args["top"], _LEARNING_BOOKS_POOL)
        for rank, step in enumerate(p.steps[1:]):
            self.assertEqual(step.tool, "book_readability")
            self.assertEqual(step.depends_on, [0])
            self.assertEqual(step.inject_result_as, f"pg_id@{rank}")
            self.assertTrue(step.optional)

    def test_plan_registered(self):
        self.assertIn("learning_books", plan_mod.PLAN_BUILDERS)
        self.assertIn("learning_books", int_mod.INTENTS)

    def test_b2_band_in_render_notes(self):
        p = _plan_learning_books(
            _e("какие книги почитать, если у меня уровень B2",
               level="intermediate"))
        notes = " ".join(p.render_notes)
        self.assertIn("B2", notes)
        self.assertIn("B1-B2", notes)

    def test_default_is_min_entry_threshold(self):
        """Без уровня — дефолт «минимальный порог вхождения» (нота
        юзера из /feedback)."""
        p = _plan_learning_books(_e("я учу английский, с чего начать?"))
        notes = " ".join(p.render_notes)
        self.assertIn("минимальный порог", notes)
        self.assertIn("flesch_reading_ease", notes)

    def test_fits_request_budget(self):
        """1 пул + POOL readability ≤ tool_calls_max — иначе router
        оборвёт план на полпути."""
        p = _plan_learning_books(_e("книги для уровня B2"))
        self.assertLessEqual(len(p.steps), RequestBudget().tool_calls_max)
        self.assertIn("learning_books", INTENT_BUDGETS_S)

    # ---- R-28 P12 (деплой 2.7.12 → rollback): exclude_archaic ----
    # До 2.7.6 «что почитать на B2 без архаизмов» шёл в
    # book_recommendation с W-9 дисклоз-нотой («архаизм» в ответе в
    # каждом прогоне → P12 same_contains держался). learning_books
    # перехватил фразу и ронял e.exclude_archaic молча; с честной
    # B120-инжекцией (2.7.12) упоминание архаизмов стало LLM-лотереей
    # → P12 across-runs флип → авто-rollback. R2(b): позитив падает на
    # до-фиксовом билдере (ноты не было вовсе).

    def test_exclude_archaic_note_present(self):
        p = _plan_learning_books(
            _e("что почитать на B2 без архаизмов", exclude_archaic=True))
        notes = " ".join(p.render_notes)
        self.assertIn("архаизм", notes)
        # Фильтр ПРИМЕНЁН, а не только продисклозен: C2+-банда исключается.
        self.assertIn("C2+", notes)
        self.assertIn("ИСКЛЮЧИ", notes)
        # Дисклоз честный: запрещаем «отфильтровал все архаизмы».
        self.assertIn("эвристическ", notes)
        self.assertIn("exclude_archaic", p.explain)

    def test_no_archaic_note_without_flag(self):
        """Без «без архаизмов» нота не ставится — рендер не должен
        рассуждать про архаизмы там, где юзер не просил."""
        p = _plan_learning_books(_e("что почитать на B2"))
        self.assertNotIn("архаизм", " ".join(p.render_notes))

    def test_p12_phrase_sets_exclude_archaic(self):
        """Экстрактор взводит exclude_archaic на дословной фразе пробы
        P12 — иначе нота билдера недостижима на проде."""
        from scripts.v2.planner.entities import _find_exclude_archaic
        self.assertTrue(_find_exclude_archaic("что почитать на B2 без архаизмов"))

    def test_p12_phrase_routes_to_learning_books(self):
        """Дословная фраза пробы P12 → learning_books (Дополнение А),
        пин маршрута, на котором живёт гейт."""
        m = int_mod.classify("что почитать на B2 без архаизмов")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)


class InterceptPopularityRoute(unittest.TestCase):
    """Дополнение А (Q13, тест-ран на 2.7.3+): learning-формулировки с
    уровнем перехватываются у book_recommendation (top_books_by_downloads
    с дисклеймером), а популярность БЕЗ learning-контекста не угоняется."""

    def test_q13_chto_pochitat_na_b2(self):
        m = int_mod.classify("что почитать на B2")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_chto_pochitat_dlya_urovnya_b2(self):
        m = int_mod.classify("что почитать для уровня B2")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    def test_knigi_dlya_b2(self):
        m = int_mod.classify("книги для B2")
        self.assertEqual(m.label, "learning_books", m.matched_pattern)

    # ---- негативы: популярность без learning-контекста ----

    def test_popular_books_not_hijacked(self):
        """«самые популярные книги» — popularity-путь без readability
        fan-out. (Сегодня фраза классифицируется в top_authors_books →
        top_authors_by — это существовавший ДО WP1 маршрут; WP1
        гарантирует лишь, что learning_books её не угоняет.)"""
        q = "самые популярные книги"
        m = int_mod.classify(q)
        self.assertNotEqual(m.label, "learning_books", m.matched_pattern)
        p = plan_mod.build(m.label, _e(q))
        tools = [s.tool for s in p.steps]
        self.assertNotIn("book_readability", tools)
        self.assertTrue(
            set(tools) <= {"top_books_by_downloads", "top_authors_by"},
            tools)

    def test_samaya_populyarnaya_kniga_still_top_books(self):
        """Singular-superlative остаётся на top_books_by_downloads."""
        q = "Какая самая популярная книга?"
        m = int_mod.classify(q)
        self.assertEqual(m.label, "book_extremum", m.matched_pattern)
        p = plan_mod.build(m.label, _e(q))
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")

    def test_pochitat_posle_x_still_book_similar(self):
        m = int_mod.classify("что почитать после Crime and Punishment")
        self.assertEqual(m.label, "book_similar", m.matched_pattern)


class TranslateComposition(unittest.TestCase):
    """Дополнение Б (Q20): «дай N слов из книги X с переводами» — было
    46s → 0 calls → ложное «упомяни книгу». Теперь: learning_words(book)
    + enrich_word fan-out (word@rank) одним заходом."""

    def test_q20_intent(self):
        m = int_mod.classify("дай 10 слов из Дракулы с переводами")
        self.assertEqual(m.label, "learning", m.matched_pattern)

    def test_q20_intent_without_translations(self):
        m = int_mod.classify("дай 10 слов из Дракулы")
        self.assertEqual(m.label, "learning", m.matched_pattern)

    def test_q20_plan_shape(self):
        p = _plan_learning(
            _e("дай 10 слов из Дракулы с переводами",
               book_id="PG345", top_n=10))
        self.assertEqual(p.intent, "learning")
        self.assertEqual(len(p.steps), 1 + 10)
        self.assertEqual(p.steps[0].tool, "learning_words")
        self.assertEqual(p.steps[0].args["scope"], {"book": "PG345"})
        self.assertEqual(p.steps[0].args["top"], 10)
        for rank, step in enumerate(p.steps[1:]):
            self.assertEqual(step.tool, "enrich_word")
            self.assertEqual(step.args["target_lang"], "ru")
            self.assertEqual(step.depends_on, [0])
            self.assertEqual(step.inject_result_as, f"word@{rank}")
            self.assertTrue(step.optional)
        self.assertIn("ПЕРЕВОД", " ".join(p.render_notes))
        # бюджет: 11 calls ≤ tool_calls_max
        self.assertLessEqual(len(p.steps), RequestBudget().tool_calls_max)

    def test_no_translation_no_enrich(self):
        """Негатив Б: просьба без переводов не тащит translate-этап."""
        p = _plan_learning(
            _e("дай 10 слов из Дракулы", book_id="PG345", top_n=10))
        tools = [s.tool for s in p.steps]
        self.assertEqual(tools, ["learning_words"])

    def test_translation_cap_10(self):
        """30 слов с переводами — кап 10 + честный _capped_from."""
        p = _plan_learning(
            _e("дай 30 слов из Дракулы с переводами",
               book_id="PG345", top_n=30))
        self.assertEqual(p.steps[0].args["top"], 10)
        self.assertEqual(p.steps[0].args["_capped_from"], 30)
        self.assertEqual(
            sum(1 for s in p.steps if s.tool == "enrich_word"), 10)


class WordRankInjection(unittest.TestCase):
    """router._inject «word@N» — инжекция слова из learning_words
    data["results"] в enrich_word."""

    @staticmethod
    def _words(n=3):
        return ToolResult.success(
            tool="learning_words",
            data={"results": [{"word": f"w{i}", "scope_count": 5,
                               "corpus_count": 100} for i in range(n)]},
        )

    def test_injects_word(self):
        out = _inject({"target_lang": "ru"}, [self._words()], [0], "word@1")
        self.assertEqual(out.get("word"), "w1")
        self.assertEqual(out["target_lang"], "ru")

    def test_rank_out_of_range_skips(self):
        # R-28 B121 — недоступный rank теперь сигнал skip (None), а не
        # тихий диспатч с пустыми args.
        out = _inject({}, [self._words(2)], [0], "word@7")
        self.assertIsNone(out)


class RankInjection(unittest.TestCase):
    """router._inject «pg_id@N» — rank-indexed injection из data["top"]."""

    @staticmethod
    def _pool(n=3):
        # R-28 B120 (R4) — мок зеркалит РЕАЛЬНЫЕ строки v1: ключ `id`,
        # БЕЗ `pg_id` (golden fixture top_books_by_downloads). Прежний
        # мок нёс оба ключа — и прятал то, что _inject читал фантомный.
        return ToolResult.success(
            tool="top_books_by_downloads",
            data={"top": [{"id": f"PG{i}",
                           "title": f"T{i}", "author": "A",
                           "downloads": 100 - i} for i in range(n)]},
        )

    def test_injects_rank_row(self):
        out = _inject({}, [self._pool()], [0], "pg_id@2")
        self.assertEqual(out.get("pg_id"), "PG2")

    def test_rank_zero(self):
        out = _inject({}, [self._pool()], [0], "pg_id@0")
        self.assertEqual(out.get("pg_id"), "PG0")

    def test_rank_out_of_range_skips(self):
        # R-28 B121 — rank за пределами источника → skip-сигнал.
        out = _inject({}, [self._pool(2)], [0], "pg_id@5")
        self.assertIsNone(out)

    def test_failed_pool_skips(self):
        bad = ToolResult.fail(tool="top_books_by_downloads",
                              err_type="not_found", message="x")
        out = _inject({}, [bad], [0], "pg_id@0")
        self.assertIsNone(out)


class FailFastClarify(unittest.TestCase):
    """Task 3 — распознанный интент без плана отвечает сразу
    (authoritative_clarify), а не через v4 LLM ~40s."""

    def test_unscoped_learning_authoritative(self):
        p = _plan_learning(_e("дай слова уровня B2", level="intermediate"))
        self.assertTrue(p.needs_clarify)
        self.assertTrue(p.authoritative_clarify)
        # рабочие примеры, а не те, что сами не работают
        self.assertIn("Pride and Prejudice", p.clarify_question)
        self.assertIn("книги", p.clarify_question)  # подсказка на learning_books

    def test_longest_book_authoritative(self):
        m = int_mod.classify("какая самая длинная книга?")
        self.assertEqual(m.label, "book_extremum", m.matched_pattern)
        p = _plan_book_extremum(_e("какая самая длинная книга?"))
        self.assertTrue(p.needs_clarify)
        self.assertTrue(p.authoritative_clarify)
        self.assertEqual(p.steps, [])


class MetaQueryPack(unittest.TestCase):
    """P1.2 §10 — только то, что собирается из существующих тулов."""

    # ---- «у вас есть <автор>?» → author_metadata ----

    def test_u_vas_est_author_intent(self):
        m = int_mod.classify("у вас есть Шекспир?")
        self.assertEqual(m.label, "book_lookup", m.matched_pattern)

    def test_bare_presence_intent(self):
        m = int_mod.classify("Шекспир есть?")
        self.assertEqual(m.label, "book_lookup", m.matched_pattern)

    def test_bare_presence_wh_guard(self):
        """«Что есть?» — wh-слово, presence-правило молчит."""
        m = int_mod.classify("Что есть?")
        self.assertNotEqual(m.label, "book_lookup", m.matched_pattern)

    def test_author_presence_delegates_to_author_metadata(self):
        p = _plan_book_lookup(
            _e("у вас есть Шекспир?", author_regex="^Shakespeare,"))
        self.assertEqual(len(p.steps), 1)
        self.assertEqual(p.steps[0].tool, "author_metadata")
        self.assertIn("PRESENCE", " ".join(p.render_notes))

    def test_resolved_book_beats_author_redirect(self):
        """Если разрезолвилась КНИГА — find_book, как раньше."""
        p = _plan_book_lookup(
            _e("у вас есть Гордость и предубеждение?",
               book_title="Pride and Prejudice"))
        self.assertEqual(p.steps[0].tool, "find_book")

    def test_word_query_not_author_presence(self):
        """«у вас есть слово ardour» не должно матчить author-presence,
        даже если экстрактор зацепил автора заодно."""
        p = _plan_book_lookup(
            _e("у вас есть слово ardour?",
               author_regex="^Shakespeare,", word="ardour"))
        tools = [s.tool for s in p.steps]
        self.assertNotIn("author_metadata", tools)

    # ---- обратный порядок superlative-фраз (§10) ----

    def test_kakoy_avtor_samyi_populyarnyi(self):
        m = int_mod.classify("какой автор самый популярный?")
        self.assertEqual(m.label, "corpus_extremum", m.matched_pattern)

    def test_kakaya_kniga_samaya_dlinnaya(self):
        m = int_mod.classify("какая книга самая длинная?")
        self.assertEqual(m.label, "book_extremum", m.matched_pattern)


if __name__ == "__main__":
    unittest.main()
