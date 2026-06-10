# -*- coding: utf-8 -*-
"""R-27 «honest renderer» (WP2a/WP2b + мини S-R1/S-R2/S-R3, 2026-06-10).

Пять фиксов одного слоя честности, негативные кейсы обязательны (R2/R5):

  WP2a — RENDER_PROMPT rule 21: never invent counts/operations; claim a
         filter only when it's in tool calls/args (или явный data-дисклоз).
         Repro: прод-трейс af384edfae2d — affinity_by_author вызван только
         с pos_filter, рендерер написал «после фильтрации имён собственных
         и русских фамилий».
  WP2b — critic/numeric_audit: critical → repair или suppression, не
         warn-only. Заявленные-но-не-выполненные операции вырезаются
         (operation-claims guard); fabricated counts чинятся подстановкой
         top_returned, числа без опоры в data — удалением предложения.
  S-R1 — _plan_author_lookup render_notes запрещает выдумывать описания
         книг (в author_metadata есть только названия + счётчики).
  S-R2 — «расскажи про слово ajar» → word_contexts (W-10 бандл), а не
         word_pos_distribution (E-routing nit 2026-06-02).
  S-R3 — честный фолбэк B108: «книга не найдена в корпусе, попробуй
         английское название» вместо «сервис поиска не отозвался».

Все тесты corpus-free и не требуют Ollama (детерминированные пути +
текстовые контракты промптов). Фикстуры не трогаются — re-record не нужен.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ====================================================================
# WP2a — RENDER_PROMPT rule 21 (контракт текста, по образцу test_w7)
# ====================================================================

class RenderPromptCarriesOperationHonestyRule(unittest.TestCase):

    def test_rule_21_canonical_sentence_present(self):
        """Каноническая англоязычная формулировка правила — целиком."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Never invent counts, percentages, years, ranks, "
                      "scores or frequencies", RENDER_PROMPT)
        self.assertIn("Never claim that a filter, exclusion, or operation "
                      "was applied unless it is present in the tool "
                      "calls/args", RENDER_PROMPT)
        self.assertIn("this filtering is not supported yet", RENDER_PROMPT)

    def test_rule_21_has_allow_and_forbid_examples(self):
        """Allow/forbid примеры по образцу rule 16."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("после фильтрации имён собственных и русских фамилий",
                      RENDER_PROMPT)
        self.assertIn("pos_filter=['ADJ']", RENDER_PROMPT)
        # И запрещающий, и допускающий маркеры присутствуют у правила 21.
        self.assertIn("Честность чисел и операций", RENDER_PROMPT)

    def test_rule_21_cites_repro_trace(self):
        """Repro-кейс af384edfae2d процитирован — ревьюер видит, что
        именно правило блокирует."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("af384edfae2d", RENDER_PROMPT)

    def test_prior_rules_intact(self):
        """Правило 21 добавлено, 16/17/20 не задеты."""
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Book titles > PG ids", RENDER_PROMPT)       # 16
        self.assertIn("Антивыдумка", RENDER_PROMPT)                # 20
        self.assertIn("не указано в данных", RENDER_PROMPT)        # 20


class CriticPromptFlagsUnperformedOperations(unittest.TestCase):

    def test_critic_prompt_lists_operation_fabrication_sign(self):
        from scripts.v2.critic import _CRITIC_PROMPT
        self.assertIn("ОПЕРАЦИЮ", _CRITIC_PROMPT)
        self.assertIn("после фильтрации имён собственных", _CRITIC_PROMPT)


# ====================================================================
# WP2b — operation-claims guard (детерминированный, без Ollama)
# ====================================================================

def _affinity_record(*, with_propn_disclosure: bool = False,
                     with_filter_arg: bool = False) -> dict:
    """Запись tool_results в форме critic_summary_records (rag_v2):
    affinity_by_author, вызванный ТОЛЬКО с pos_filter — точная форма
    прод-трейса af384edfae2d."""
    query = {"author_regex": "^Tolstoy,", "top": 30,
             "pos_filter": ["NOUN"], "min_corpus_count": 200}
    if with_filter_arg:
        query["exclude_names"] = True
    data = {
        "top": [{"word": "samovar", "affinity": 12.1},
                {"word": "icon", "affinity": 9.7}],
        "top_requested": 30, "top_returned": 2,
    }
    if with_propn_disclosure:
        data["proper_noun_filter"] = "v2 surname blocklist dropped 5"
    return {"tool": "affinity_by_author", "ok": True,
            "data": data, "query": query,
            "coverage": {"books_matched": 47, "books_total": 47},
            "warnings": []}


class RendererDoesNotClaimUnperformedFilter(unittest.TestCase):
    """Negative-тест R2 по трейсу af384edfae2d: tool_calls без
    name-фильтра → в финальном ответе нет «после фильтрации имён»."""

    _LYING_ANSWER = (
        "Вот характерные существительные Толстого. "
        "Список получен после фильтрации имён собственных и русских "
        "фамилий по корпусу. Самое характерное слово — samovar."
    )

    def test_renderer_does_not_claim_unperformed_filter(self):
        from scripts.v2 import critic as critic_mod
        records = [_affinity_record()]  # только pos_filter, без дисклоза
        rep = critic_mod.audit_operation_claims(self._LYING_ANSWER, records)
        self.assertTrue(rep.has_issues(),
                        "claim без опоры в args/data должен флагаться")
        fixed = critic_mod.suppress_operation_claims(
            self._LYING_ANSWER, rep)
        self.assertNotIn("после фильтрации имён", fixed,
                         "заявление о невыполненном фильтре должно быть "
                         "вырезано из ответа")
        # Полезное содержимое ответа уцелело.
        self.assertIn("samovar", fixed)
        # Честный дисклоз вместо лжи.
        self.assertIn("не выполняли", fixed)

    def test_claim_survives_when_data_discloses_propn_drops(self):
        """Negative guard: когда data.proper_noun_filter явно сообщает о
        дропах, заявление о фильтрации имён — правда, не трогаем."""
        from scripts.v2 import critic as critic_mod
        records = [_affinity_record(with_propn_disclosure=True)]
        rep = critic_mod.audit_operation_claims(self._LYING_ANSWER, records)
        self.assertFalse(rep.has_issues())
        self.assertEqual(
            critic_mod.suppress_operation_claims(self._LYING_ANSWER, rep),
            self._LYING_ANSWER)

    def test_claim_survives_when_args_carry_name_filter(self):
        """Negative guard: name-фильтр в tool args → claim подтверждён."""
        from scripts.v2 import critic as critic_mod
        records = [_affinity_record(with_filter_arg=True)]
        rep = critic_mod.audit_operation_claims(self._LYING_ANSWER, records)
        self.assertFalse(rep.has_issues())

    def test_pos_filter_claim_is_not_flagged(self):
        """pos_filter есть в args — заявление о POS-фильтрации легально
        и не попадает под name-filter guard."""
        from scripts.v2 import critic as critic_mod
        answer = ("Существительные отобраны по части речи "
                  "(pos_filter=['NOUN']): samovar, icon.")
        rep = critic_mod.audit_operation_claims(answer,
                                                [_affinity_record()])
        self.assertFalse(rep.has_issues())

    def test_clean_answer_untouched(self):
        from scripts.v2 import critic as critic_mod
        answer = "Топ-2 слова Толстого: samovar (12.1), icon (9.7)."
        rep = critic_mod.audit_operation_claims(answer,
                                                [_affinity_record()])
        self.assertFalse(rep.has_issues())
        self.assertEqual(
            critic_mod.suppress_operation_claims(answer, rep), answer)


# ====================================================================
# WP2b — numeric repair-or-suppress (facts-only numeric, scope-док §10)
# ====================================================================

class NumericCriticalRepairsOrSuppresses(unittest.TestCase):

    def test_compare_authors_fabricated_count_suppressed(self):
        """Scope-док §10: «сравни По и Лавкрафта по стилю» → в ответе нет
        fabricated counts. Unit на numeric_audit+repair без живого Ollama:
        выдуманное «4500 уникальных слов» (нет в data) удаляется из тела
        ответа; честные числа из data остаются."""
        from scripts.v2.numeric_audit import audit_numbers, repair_with_audit
        records = [{
            "tool": "compare_authors",
            "data": {
                "author1": "Poe, Edgar Allan",
                "author2": "Lovecraft, H. P.",
                "jaccard_top200": 0.18,
                "delta": 0.74,
                "unique_a": ["raven", "pendulum"],
                "unique_b": ["eldritch", "cyclopean"],
            },
            "query": {"author1_regex": "^Poe,",
                      "author2_regex": "^Lovecraft,", "top": 20},
        }]
        answer = (
            "Стили По и Лавкрафта пересекаются слабо (Jaccard 0.18). "
            "По использует 4500 уникальных слов в своём корпусе. "
            "У По характерны raven и pendulum, у Лавкрафта — eldritch."
        )
        report = audit_numbers(answer, records, intent="author_compare")
        self.assertTrue(report.has_issues(),
                        "4500 нет в tool data — должен флагаться")
        fixed = repair_with_audit(answer, report)
        self.assertNotIn("4500", fixed.split("🔧")[0].split("📊")[0],
                         "fabricated count обязан исчезнуть из тела ответа")
        self.assertIn("Jaccard 0.18", fixed)
        self.assertIn("eldritch", fixed)

    def test_compare_authors_honest_answer_unchanged(self):
        """Negative: честный ответ (все числа из data) не трогается."""
        from scripts.v2.numeric_audit import audit_numbers, repair_with_audit
        records = [{
            "tool": "compare_authors",
            "data": {"jaccard_top200": 0.18, "delta": 0.74},
            "query": {"top": 20},
        }]
        answer = ("Пересечение словарей Jaccard = 0.18, Burrows Delta = "
                  "0.74 — стили заметно различаются.")
        report = audit_numbers(answer, records, intent="author_compare")
        self.assertFalse(report.has_issues())
        self.assertEqual(repair_with_audit(answer, report), answer)

    def test_count_claim_repaired_to_top_returned(self):
        """Stan-class «список из 50 слов» при top_returned=19 → repair
        подстановкой истинного счёта (не warn-only)."""
        from scripts.v2.numeric_audit import audit_numbers, repair_with_audit
        records = [{
            "tool": "affinity_by_author",
            "data": {"top_requested": 100, "top_returned": 19,
                     "top": [{"word": f"w{i}"} for i in range(19)]},
            "query": {"top": 100},
        }]
        answer = "Представляю список из 50 слов автора."
        report = audit_numbers(answer, records, intent="author_vocab")
        fixed = repair_with_audit(answer, report)
        body = fixed.split("🔧")[0]  # 🔧-футер ЦИТИРУЕТ исправление
        self.assertIn("список из 19 слов", body,
                      "счёт должен быть починен на top_returned")
        self.assertNotIn("список из 50 слов", body,
                         "fabricated счёт обязан исчезнуть из тела ответа")
        # Дисклоз обязателен — молчаливая правка текста сама нечестна.
        self.assertIn("Honesty repair", fixed)

    def test_excise_sentence_skips_table_rows(self):
        """Числа в markdown-таблицах не вырезаются (детерминированный
        рендер из data; защита от порчи таблиц)."""
        from scripts.v2.numeric_audit import _excise_sentence
        text = ("| Слово | Счёт |\n| --- | --- |\n| raven | 4500 |\n\n"
                "Прозы без этого числа тут нет.")
        out, ok = _excise_sentence(text, "4500")
        self.assertFalse(ok)
        self.assertEqual(out, text)

    def test_excise_sentence_removes_prose_sentence(self):
        from scripts.v2.numeric_audit import _excise_sentence
        text = "Первое предложение. Тут выдумка про 4500 слов. Третье."
        out, ok = _excise_sentence(text, "4500")
        self.assertTrue(ok)
        self.assertNotIn("4500", out)
        self.assertIn("Первое предложение.", out)
        self.assertIn("Третье", out)


# ====================================================================
# S-R1 — author_lookup render_notes: запрет выдумывать описания книг
# ====================================================================

class AuthorLookupRenderNotesNoFabrication(unittest.TestCase):

    def _plan(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.builders.author import _plan_author_lookup
        e = Entities(author_regex="^Wodehouse,",
                     author_clarify_candidates=[])
        return _plan_author_lookup(e)

    def test_author_lookup_render_notes_no_fabrication(self):
        """render_notes явно запрещает придумывать описания книг —
        в author_metadata есть только названия и счётчики."""
        plan = self._plan()
        self.assertEqual(plan.intent, "author_lookup")
        notes = " ".join(plan.render_notes)
        self.assertIn("НЕ ВЫДУМЫВАЙ описания книг", notes)
        self.assertIn("без описаний", notes)
        # Подсказан честный выход — follow-up вместо выдумки.
        self.assertIn("follow-up", notes)

    def test_book_list_directive_intact(self):
        """S-R5b директива (перечислить книги из sample_titles +
        books_matched) не потеряна при расширении notes."""
        plan = self._plan()
        notes = " ".join(plan.render_notes)
        self.assertIn("sample_titles", notes)
        self.assertIn("books_matched", notes)


# ====================================================================
# S-R2 — ajar→word_pos routing-nit
# ====================================================================

class AjarRoutesToWordBundle(unittest.TestCase):

    def _classify(self, q):
        from scripts.v2.planner import intent
        return intent.classify(q)

    def test_tell_about_word_routes_to_word_contexts(self):
        """«расскажи про слово ajar» → word_contexts (W-10 бандл:
        перевод + примеры + этимология), НЕ word_pos и НЕ clarify."""
        for q in ("расскажи про слово ajar",
                  "расскажи о слове ajar",
                  "расскажи мне про слово whimsy",
                  "tell me about the word ajar"):
            with self.subTest(q=q):
                r = self._classify(q)
                self.assertEqual(r.label, "word_contexts",
                                 f"{q!r} → {r.label}")
                self.assertNotEqual(r.label, "word_pos")

    def test_word_contexts_plan_is_the_bundle_not_word_pos(self):
        """Сквозной негатив: план для «расскажи про слово ajar» не
        содержит word_pos_distribution и содержит enrich_word
        (перевод/этимология — суть W-10 бандла)."""
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.builders.word import _plan_word_contexts
        plan = _plan_word_contexts(Entities(word="ajar"))
        tools = [s.tool for s in plan.steps]
        self.assertNotIn("word_pos_distribution", tools)
        self.assertIn("enrich_word", tools)

    def test_introduction_and_corpus_meta_untouched(self):
        """Negative guard (R5): соседние «расскажи …»-правила целы."""
        self.assertEqual(self._classify("расскажи о себе").label,
                         "introduction")
        self.assertEqual(self._classify("расскажи про корпус").label,
                         "corpus_meta")

    def test_real_pos_queries_still_route_to_word_pos(self):
        """Negative guard: настоящие POS-запросы остаются в word_pos."""
        self.assertEqual(
            self._classify("как часто ajar используется как noun").label,
            "word_pos")
        self.assertEqual(
            self._classify("какие слова имеют больше всего разных "
                           "значений у Шекспира").label,
            "word_pos")


# ====================================================================
# S-R3 — честный фолбэк resolve_book_title (B108, текстовая часть)
# ====================================================================

class BookTitleFallbackText(unittest.TestCase):

    def test_book_title_fallback_text(self):
        """resolve_book_title not_found → «не найдена в корпусе» +
        совет про английское название; НЕ «сервис не отозвался»."""
        from scripts.v2.rag_v2 import (_ToolPipelineEmpty,
                                       _short_render_error_message)
        msg = _short_render_error_message(_ToolPipelineEmpty(
            "resolve_book_title: no book matched 'Евгений Онегин'"))
        self.assertIn("не найдена в корпусе", msg)
        self.assertIn("английское название", msg)
        self.assertIn("Евгений Онегин", msg)
        self.assertNotIn("не отозвался", msg)

    def test_friendly_render_error_carries_honest_lead(self):
        """Сквозной путь _dispatch_render-формы: failed resolve →
        лид ответа честный, без «сервис не отозвался»."""
        from scripts.v2._types import ToolResult
        from scripts.v2.rag_v2 import (_ToolPipelineEmpty,
                                       _friendly_render_error)
        failed = ToolResult.fail(
            tool="resolve_book_title", err_type="not_found",
            message="no book matched 'Евгений Онегин'")
        out = _friendly_render_error(
            _ToolPipelineEmpty("resolve_book_title: no book matched "
                               "'Евгений Онегин'"),
            [failed])
        self.assertIn("не найдена в корпусе", out)
        self.assertIn("английское название", out)
        self.assertNotIn("не отозвался", out)

    def test_generic_pipeline_empty_keeps_old_text(self):
        """Negative guard: не-книжный pipeline-empty сохраняет прежнюю
        формулировку (там «сервис не отозвался» может быть правдой)."""
        from scripts.v2.rag_v2 import (_ToolPipelineEmpty,
                                       _short_render_error_message)
        msg = _short_render_error_message(_ToolPipelineEmpty(
            "hybrid_search: boom"))
        self.assertIn("инструменты не вернули данных", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
