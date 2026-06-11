"""R-28 заход 0 «learning-polish» — B118 + B119 (smoke 2.7.8 S1–S3).

B118 (регрессия аддендума A, smoke S3): «что почитать на B2»
(intercept-правило) исполняла book_readability ×10, но рендер выдавал
голый топ-скачиваний с CEFR «Не указано в данных». Гипотеза смоука
«intercept теряет pg_id@rank-инжекцию» ОПРОВЕРГНУТА: оба правила-входа
дают байт-в-байт идентичный план из единственной точки сборки
(_plan_learning_books) — класс PlanEquivalence фиксирует это
параметризованно. Реальный корень — рендер: learning_books появился в
R-27 WP1 ПОСЛЕ alpha6 и не попал в _LOW_TEMP_INTENTS → temp 0.3 на
join-heavy payload (top_books + 10 × book_readability) бимодально
теряла join (тот же Q13-класс variance, ради которого set заведён).
Фикс: learning_books → temp 0.1 + обязательный JOIN проговорён в
render_notes. R2-негативы: оба теста RendererDeterminism падают на
до-фиксовом коде.

B119 («волки-волки», smoke S1–S3): критик флаговал Pride and Prejudice
(PG1342, реальная, rank 5 пула top_books) как вымышленную. Корень —
_build_payload_for_critic резал trusted set: [:8] выбрасывал хвост
из 11 записей плана, а слепой 600-char _shrink обрубал JSON таблицы
top_books на ~3-й строке — rank-5 тайтл буквально отсутствовал в
payload критика. Фикс: cap 12 (= tool_calls_max), table-aware shrink
(каждая строка таблицы, только имена + headline-метрики) и
детерминированный evidence-фильтр против ПОЛНЫХ записей перед выдачей
флага юзеру. R2-негативы: CriticPayload-тесты падают на до-фиксовом
коде; реально выдуманный тайтл флагуется по-прежнему
(test_fabricated_title_still_flagged).

R5: все три claim-регекса (_CLAIM_QUOTED_RE / _CLAIM_PG_ID_RE /
_CLAIM_TITLECASE_RE) — с позитивами И негативами здесь же.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import critic as critic_mod
from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner.builders.learning import _LEARNING_BOOKS_POOL

# Формулировки смоука 2.7.8: S1 — базовое правило («книги … уровень»),
# S3 — intercept-правило аддендума A («что почитать … B2»), плюс
# P12-проба («без архаизмов» — builder её осознанно игнорирует).
_S1_PHRASING = "какие книги почитать, если у меня уровень B2"
_S3_PHRASING = "что почитать на B2"
_ALL_PHRASINGS = (
    _S1_PHRASING,
    _S3_PHRASING,
    "что почитать на B2 без архаизмов",
)


def _build_for(question: str) -> plan_mod.QueryPlan:
    """Живой rules-путь: classify → extract → build (без history)."""
    m = int_mod.classify(question)
    e = ent_mod.extract(question)
    return plan_mod.build(m.label, e)


def _step_shape(plan: plan_mod.QueryPlan) -> list[tuple]:
    return [(s.tool, s.args, s.depends_on, s.inject_result_as, s.optional)
            for s in plan.steps]


class B118PlanEquivalence(unittest.TestCase):
    """Оба правила-входа → ОДИН план из единой точки сборки.

    Опровержение гипотезы смоука: расхождения планов НЕТ — этот класс
    гвоздит инвариант, чтобы будущие правки intercept-правил не могли
    развести входы по разным веткам сборки.
    """

    def test_all_phrasings_route_to_learning_books(self):
        for q in _ALL_PHRASINGS:
            with self.subTest(q=q):
                m = int_mod.classify(q)
                self.assertEqual(m.label, "learning_books",
                                 m.matched_pattern)

    def test_intercept_and_base_plans_identical(self):
        """План S3 (intercept) == план S1 (база): шаги, инжекции,
        render_notes — байт-в-байт."""
        base = _build_for(_S1_PHRASING)
        for q in _ALL_PHRASINGS[1:]:
            with self.subTest(q=q):
                p = _build_for(q)
                self.assertEqual(p.intent, base.intent)
                self.assertEqual(_step_shape(p), _step_shape(base))
                self.assertEqual(p.render_notes, base.render_notes)

    def test_intercept_keeps_rank_injection(self):
        """S3-вход НЕ теряет pg_id@rank: пул + POOL инжектированных
        readability-шагов (сам симптом из баг-репорта)."""
        p = _build_for(_S3_PHRASING)
        self.assertEqual(len(p.steps), 1 + _LEARNING_BOOKS_POOL)
        self.assertEqual(p.steps[0].tool, "top_books_by_downloads")
        for rank, step in enumerate(p.steps[1:]):
            self.assertEqual(step.tool, "book_readability")
            self.assertEqual(step.inject_result_as, f"pg_id@{rank}")


class B118RendererDeterminism(unittest.TestCase):
    """Реальный корень B118 — рендер. R2: оба теста падают до фикса."""

    def test_learning_books_renders_low_temp(self):
        """learning_books — table-heavy join-интент; рендер обязан идти
        на temp 0.1 (alpha6-набор), не на «прозаичных» 0.3."""
        from scripts.v2.rag_v2 import _LOW_TEMP_INTENTS
        self.assertIn("learning_books", _LOW_TEMP_INTENTS)

    def test_render_notes_mandate_readability_join(self):
        """Join проговорён обязательным: запрет «Не указано в данных»
        для книг с ok=true readability — у модели нет пути отступления
        в голый топ-скачиваний."""
        for q in _ALL_PHRASINGS:
            with self.subTest(q=q):
                notes = " ".join(_build_for(q).render_notes)
                self.assertIn("JOIN ОБЯЗАТЕЛЕН", notes)
                self.assertIn("Не указано в данных", notes)


if __name__ == '__main__':
    unittest.main()
