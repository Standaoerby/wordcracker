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
# P12-проба («без архаизмов» — с 2.7.13 builder её УВАЖАЕТ:
# entity-driven нота exclude_archaic, см. R-28 P12 rollback-фикс).
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
        render_notes — байт-в-байт. Для «без архаизмов» (R-28 P12,
        2.7.13) шаги/инжекции те же, а notes = базовые + ровно одна
        entity-driven нота exclude_archaic — это НЕ развод входов по
        веткам сборки (точка сборки одна), а осознанная реакция на
        заявленный фильтр (R-2: applied or disclosed, never silent)."""
        base = _build_for(_S1_PHRASING)
        for q in _ALL_PHRASINGS[1:]:
            with self.subTest(q=q):
                p = _build_for(q)
                self.assertEqual(p.intent, base.intent)
                self.assertEqual(_step_shape(p), _step_shape(base))
                if "без архаизмов" in q:
                    self.assertEqual(p.render_notes[:len(base.render_notes)],
                                     base.render_notes)
                    extra = p.render_notes[len(base.render_notes):]
                    self.assertEqual(len(extra), 1, extra)
                    self.assertIn("архаизм", extra[0])
                else:
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


# ---------------------------------------------------------------------------
# B119 — критик-«волки-волки»
# ---------------------------------------------------------------------------

_PNP_TITLE = "Pride and Prejudice"


def _learning_books_records() -> list[dict]:
    """11 записей плана learning_books: пул top_books (P&P на rank 5,
    как в smoke S1–S3) + 10 book_readability. Строки пула нарочно
    «прод-толстые» (subjects и пр.), чтобы слепой 600-char shrink
    гарантированно обрубал таблицу ДО rank 5 — как на проде."""
    titles = [
        "Frankenstein; Or, The Modern Prometheus",
        "Moby Dick; Or, The Whale",
        "Romeo and Juliet",
        "Alice's Adventures in Wonderland",
        _PNP_TITLE,                      # rank 5 — герой бага
        "The Great Gatsby",
        "Dracula",
        "The Adventures of Sherlock Holmes",
        "A Tale of Two Cities",
        "Great Expectations",
    ]
    rows = []
    for i, title in enumerate(titles):
        rows.append({
            "rank": i + 1,
            "pg_id": f"PG{1342 if title == _PNP_TITLE else 9000 + i}",
            "title": title,
            "author": f"Author Authorson the {i + 1}th of His Name",
            "downloads": 90000 - i * 1000,
            "lang": "en",
            "subjects": ("Fiction; Classic literature; England — "
                         "Social life and customs — 19th century"),
        })
    records = [{
        "tool": "top_books_by_downloads", "ok": True,
        "data": {"top": rows, "metric": "downloads"},
        "query": {"top": 10, "lang": "en"},
        "coverage": {"books_matched": 10, "books_total": 47000},
        "warnings": [],
    }]
    for row in rows:
        records.append({
            "tool": "book_readability", "ok": True,
            "data": {"pg_id": row["pg_id"], "title": row["title"],
                     "flesch_reading_ease": 72.5,
                     "flesch_kincaid_grade": 7.1,
                     "cefr_heuristic": "B1-B2", "words": 34427},
            "query": {"pg_id": row["pg_id"], "sample_chars": 200000},
            "coverage": {"books_matched": 1, "books_total": 1},
            "warnings": [],
        })
    return records


class B119CriticPayload(unittest.TestCase):
    """Trusted set критика обязан включать ВСЕ записи плана и все
    тайтлы таблицы. R2: оба теста падают на до-фиксовом коде
    ([:8]-cap и слепой 600-char shrink)."""

    def test_payload_includes_all_plan_records(self):
        records = _learning_books_records()
        self.assertEqual(len(records), 11)  # прекондиция: полный план
        payload = critic_mod._build_payload_for_critic(
            "ответ", records, intent="learning_books")
        self.assertEqual(len(payload["tool_results"]), len(records))

    def test_rank5_title_survives_shrink(self):
        """Слепой _shrink(600) обрубал JSON пула на ~3-й строке —
        rank-5 P&P отсутствовала в payload, критик флаговал реальную
        книгу. Table-aware shrink сохраняет КАЖДУЮ строку."""
        payload = critic_mod._build_payload_for_critic(
            "ответ", _learning_books_records(), intent="learning_books")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIn(_PNP_TITLE, blob)
        self.assertIn("PG1342", blob)

    def test_render_note_dropped_from_critic_payload(self):
        """_render_note — инструкция рендереру, не свидетельство."""
        out = critic_mod._shrink_table_aware(
            {"top": [{"rank": 1, "title": "T", "pg_id": "PG1"}],
             "_render_note": "renderer must..."},
            max_chars=600)
        self.assertNotIn("_render_note", json.dumps(out))

    def test_non_table_data_still_char_capped(self):
        """Не-табличные данные — прежний жёсткий char-cap (бюджет
        критика не раздуваем)."""
        out = critic_mod._shrink_table_aware(
            {"blob": "x" * 5000}, max_chars=600)
        s = json.dumps(out, ensure_ascii=False, default=str)
        self.assertLess(len(s), 1000)
        self.assertIn("truncated", s)


class B119ClaimEntityRegexes(unittest.TestCase):
    """R5 — позитивы и негативы трёх claim-регексов."""

    def test_quoted_span_extracted(self):
        cands = critic_mod._claim_entity_candidates(
            "Книга «Pride and Prejudice» не присутствует в tool_results")
        self.assertIn("Pride and Prejudice", cands)

    def test_pg_id_extracted(self):
        cands = critic_mod._claim_entity_candidates(
            "PG1342 нет ни в одном tool_result")
        self.assertIn("PG1342", cands)

    def test_titlecase_run_extracted_without_quotes(self):
        """Одиночная заглавная «A» в артикль не входит ([A-Z][a-z]+ —
        анти-шум), но усечённый кандидат остаётся подстрокой полного
        тайтла в данных — suppress срабатывает."""
        cands = critic_mod._claim_entity_candidates(
            "ответ упоминает A Tale of Two Cities без опоры на данные")
        self.assertIn("Tale of Two Cities", cands)
        kept, suppressed = critic_mod.filter_claims_with_data_evidence(
            ["ответ упоминает A Tale of Two Cities без опоры на данные"],
            _learning_books_records())
        self.assertEqual(kept, [])
        self.assertEqual(len(suppressed), 1)

    # ---- негативы ----

    def test_single_capitalized_word_not_extracted(self):
        """Одиночные Capitalized-слова (Flesch, CEFR, Анна) — шум, не
        кандидаты."""
        cands = critic_mod._claim_entity_candidates(
            "значение Flesch не совпадает с данными")
        self.assertEqual(cands, [])

    def test_numbers_only_claim_no_candidates(self):
        cands = critic_mod._claim_entity_candidates(
            "в ответе 250 книг, в данных 47")
        self.assertEqual(cands, [])


class B119EvidenceFilter(unittest.TestCase):
    """Детерминированный evidence-фильтр против ПОЛНЫХ записей."""

    def test_claim_with_real_title_suppressed(self):
        kept, suppressed = critic_mod.filter_claims_with_data_evidence(
            ["Книга «Pride and Prejudice» не присутствует в tool_results"],
            _learning_books_records())
        self.assertEqual(kept, [])
        self.assertEqual(len(suppressed), 1)

    def test_claim_with_real_pg_id_suppressed(self):
        kept, suppressed = critic_mod.filter_claims_with_data_evidence(
            ["PG1342 выдуман"], _learning_books_records())
        self.assertEqual(kept, [])
        self.assertEqual(len(suppressed), 1)

    def test_fabricated_title_kept(self):
        """R2-негатив: реально выдуманный тайтл проходит фильтр и
        остаётся флагом."""
        kept, suppressed = critic_mod.filter_claims_with_data_evidence(
            ["Книга «Necronomicon of Steel» не присутствует в данных"],
            _learning_books_records())
        self.assertEqual(len(kept), 1)
        self.assertEqual(suppressed, [])

    def test_numbers_only_claim_stays_llm_call(self):
        """Клейм без распознаваемых сущностей фильтр не трогает."""
        kept, _ = critic_mod.filter_claims_with_data_evidence(
            ["в ответе 250 книг, в данных 47"],
            _learning_books_records())
        self.assertEqual(len(kept), 1)


class _FakeResp:
    def __init__(self, verdict: dict):
        self._verdict = verdict

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": json.dumps(self._verdict,
                                                  ensure_ascii=False)},
                "prompt_eval_count": 100, "eval_count": 50}


class B119ReviewIntegration(unittest.TestCase):
    """review() end-to-end с замоканным Ollama: фильтр стоит МЕЖДУ
    LLM-вердиктом и юзером."""

    def _review(self, claims: list[str]) -> critic_mod.CriticVerdict:
        fake = _FakeResp({"verified": False, "unsupported_claims": claims,
                          "missing_caveats": [], "summary": "s"})
        with mock.patch.object(critic_mod.requests, "post",
                               return_value=fake):
            with mock.patch.object(critic_mod, "CRITIC_ENABLED", True):
                return critic_mod.review(
                    "ответ с таблицей", _learning_books_records(),
                    intent="learning_books",
                    ollama_host="http://mocked:11434")

    def test_pnp_flag_suppressed_and_verified_restored(self):
        """Smoke S1–S3: флаг нёсся ТОЛЬКО клеймом про P&P → после
        suppress остаток пуст, красный бейдж не шьётся."""
        v = self._review(
            ["Книга «Pride and Prejudice» не присутствует в tool_results"])
        self.assertEqual(v.unsupported_claims, [])
        self.assertTrue(v.verified)

    def test_fabricated_title_still_flagged(self):
        """R2-негатив: критик НЕ ослеп — выдуманный тайтл по-прежнему
        флагуется, verified остаётся False."""
        v = self._review(
            ["Книга «Necronomicon of Steel» не присутствует в данных"])
        self.assertEqual(len(v.unsupported_claims), 1)
        self.assertFalse(v.verified)


if __name__ == "__main__":
    unittest.main()
