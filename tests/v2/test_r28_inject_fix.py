"""R-28 заход 3 — B120/B121: rank-индексированная инжекция router'а.

B120 (P1, «что почитать на B2» → learning_books → {} во все 10
book_readability): `router._inject` читал `row.get("pg_id")` из
`data["top"]` top_books_by_downloads, но РЕАЛЬНЫЕ v1-строки несут ключ
`id` (golden fixture scripts.rag_tools.top_books_by_downloads.json) —
`pg_id` был фантомным row-ключом схемы, подкреплённым R4-нарушением в
моке (тестовый pool нёс ОБА ключа). Инжекция молча доставляла {} во все
зависимые шаги с момента появления в 2.7.6 (268fc52, R-27 WP1); симптом
B118 «Не указано в данных» — этот же баг, не render-variance. Это НЕ
регрессия 2.7.9/2.7.10 — born-broken.

B121 (P2, under-fill): план фан-аутит N шагов независимо от фактической
длины источника. Теперь шаг с недоступной rank-инжекцией НЕ исполняется:
_inject возвращает None → исполнители кладут placeholder (выравнивание
results по индексам шагов) с warning'ом `inject_shortfall`, эмитят
step_skip/tool_skip, а рендер получает shortfall-ноту в словаре
count-honesty WP3 1e (top_requested / top_returned).

Моки источников зеркалят golden-фикстуры по НАБОРУ КЛЮЧЕЙ (per-row
assert против фикстурной строки) — форма не может разойтись с реальным
v1 незаметно (R4).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2._types import Coverage, ToolResult                  # noqa: E402
from scripts.v2.planner import plan as plan_mod                     # noqa: E402
from scripts.v2.planner import router as router_mod                 # noqa: E402
from scripts.v2.planner.entities import Entities                    # noqa: E402

_FIXTURES = _REPO / "scripts" / "v2" / "contracts" / "fixtures"


def _fixture_rows(fname: str, rows_key: str) -> list[dict]:
    raw = json.loads((_FIXTURES / fname).read_text(encoding="utf-8"))
    return raw[rows_key]


def _extend_rows(rows: list[dict], n: int, id_key: str) -> list[dict]:
    """n строк РОВНО с тем же набором ключей, что у реальной фикстурной
    строки (клонируем форму, не выдумываем ключи)."""
    template_keys = set(rows[0].keys())
    out = []
    for i in range(n):
        base = dict(rows[i % len(rows)])
        base[id_key] = f"{base[id_key]}_{i}" if i >= len(rows) else base[id_key]
        assert set(base.keys()) == template_keys
        out.append(base)
    return out


class _RankInjectHarness:
    """Общая параметризация pg_id@rank / word@rank (механика общая)."""

    source_tool: str
    consumer_tool: str
    fixture_file: str
    rows_key: str        # ключ списка строк в data источника
    row_id_key: str      # ключ значения в строке источника (real v1)
    consumer_arg: str    # arg, который должна доставить инжекция

    # ---- план через РЕАЛЬНЫЙ builder (не руками собранные шаги) ----

    def build_plan(self) -> plan_mod.QueryPlan:
        raise NotImplementedError

    # ---- инфраструктура ----

    def _rows(self, n: int) -> list[dict]:
        rows = _fixture_rows(self.fixture_file, self.rows_key)
        return _extend_rows(rows, n, self.row_id_key)

    def _run(self, plan, source_rows):
        calls: list[tuple[str, dict]] = []

        def spy(tool, args, budget=None):
            calls.append((tool, dict(args)))
            if tool == self.source_tool:
                return ToolResult.success(
                    tool=tool, data={self.rows_key: source_rows},
                    coverage=Coverage(books_matched=len(source_rows)),
                    query=dict(args))
            return ToolResult.success(
                tool=tool, data={"ok": True, **args}, query=dict(args))

        with mock.patch.object(router_mod, "dispatch", spy):
            rr = router_mod.execute(plan)
        return rr, calls

    # ---- B120 ----

    def test_b120_injection_delivers_id_per_rank(self):
        """Каждый зависимый шаг получает НЕПУСТОЙ инжектированный
        аргумент, соответствующий своей rank-строке источника.

        Падает на до-фиксовом коде: _inject читал фантомный ключ
        (pg_id) → все консьюмеры диспатчились с {}."""
        rows = self._rows(10)
        plan = self.build_plan()
        rr, calls = self._run(plan, rows)
        self.assertEqual(rr.kind, "results")
        consumer_calls = [a for t, a in calls if t == self.consumer_tool]
        self.assertEqual(len(consumer_calls), 10)
        for rank, args in enumerate(consumer_calls):
            self.assertTrue(
                args.get(self.consumer_arg),
                f"rank {rank}: {self.consumer_tool} получил пустой "
                f"{self.consumer_arg}: {args} (B120)")
            self.assertEqual(args[self.consumer_arg],
                             rows[rank][self.row_id_key],
                             f"rank {rank}: инжекция не из rank-строки")

    # ---- B121 ----

    def test_b121_underfill_skips_and_notes(self):
        """Источник 8 строк, план 10 → ровно 8 вызовов консьюмера,
        0 пустых, 2 step_skip, shortfall-нота в render_notes.

        Падает на до-фиксовом коде: все 10 диспатчились ({} на
        недостающих rank'ах), ноты не было."""
        rows = self._rows(8)
        plan = self.build_plan()
        rr, calls = self._run(plan, rows)
        consumer_calls = [a for t, a in calls if t == self.consumer_tool]
        self.assertEqual(len(consumer_calls), 8,
                         "шаги за пределами источника не должны диспатчиться")
        for args in consumer_calls:
            self.assertTrue(args.get(self.consumer_arg),
                            f"пустой вызов {self.consumer_tool}: {args}")
        skips = [e for e in rr.events if e.kind == "step_skip"]
        self.assertEqual(len(skips), 2)
        # results выровнены по шагам плана (depends_on индексирует results)
        self.assertEqual(len(rr.results), len(plan.steps))
        placeholders = [r for r in rr.results
                        if any(w.code == "inject_shortfall"
                               for w in r.warnings)]
        self.assertEqual(len(placeholders), 2)
        notes = " ".join(plan.render_notes or [])
        self.assertIn("SHORTFALL ИСТОЧНИКА", notes)
        self.assertIn("top_requested=10", notes)
        self.assertIn("top_returned=8", notes)

    def test_b121_full_source_unchanged(self):
        """Негатив: источник ≥ N → прежнее поведение, ноты нет."""
        rows = self._rows(10)
        plan = self.build_plan()
        rr, calls = self._run(plan, rows)
        self.assertEqual(
            len([a for t, a in calls if t == self.consumer_tool]), 10)
        self.assertFalse([e for e in rr.events if e.kind == "step_skip"])
        notes = " ".join(plan.render_notes or [])
        self.assertNotIn("SHORTFALL ИСТОЧНИКА", notes)

    def test_b121_underfill_stream_executor(self):
        """Стрим-исполнитель — те же skip-семантики (tool_skip event)."""
        rows = self._rows(8)
        plan = self.build_plan()
        calls: list[tuple[str, dict]] = []

        def spy(tool, args, budget=None):
            calls.append((tool, dict(args)))
            if tool == self.source_tool:
                return ToolResult.success(
                    tool=tool, data={self.rows_key: rows}, query=dict(args))
            return ToolResult.success(tool=tool, data={"ok": True},
                                      query=dict(args))

        with mock.patch.object(router_mod, "dispatch", spy):
            events = list(router_mod.execute_stream(plan))
        skips = [e for e in events if e.get("event") == "tool_skip"]
        self.assertEqual(len(skips), 2)
        self.assertEqual(
            len([a for t, a in calls if t == self.consumer_tool]), 8)


class PgIdRankThroughRouter(_RankInjectHarness, unittest.TestCase):
    """B120/B121 через РЕАЛЬНЫЙ план learning_books («что почитать на
    B2») — top_books_by_downloads(top=10) + book_readability × 10
    (pg_id@rank)."""

    source_tool = "top_books_by_downloads"
    consumer_tool = "book_readability"
    fixture_file = "scripts.rag_tools.top_books_by_downloads.json"
    rows_key = "top"
    row_id_key = "id"
    consumer_arg = "pg_id"

    def build_plan(self):
        e = Entities(raw_misc={"raw_text": "что почитать на B2"})
        plan = plan_mod.build("learning_books", e)
        assert [s.tool for s in plan.steps][0] == self.source_tool
        return plan

    def test_fixture_rows_have_no_pg_id(self):
        """Гард на корень B120: реальные v1-строки top_books несут `id`,
        НЕ `pg_id`. Если v1 однажды начнёт отдавать pg_id — этот тест
        скажет пересмотреть чтение в _inject, а не наоборот."""
        rows = _fixture_rows(self.fixture_file, self.rows_key)
        self.assertIn("id", rows[0])
        self.assertNotIn("pg_id", rows[0])


class WordRankThroughRouter(_RankInjectHarness, unittest.TestCase):
    """B120/B121-параметризация для word@rank — план learning
    («N слов с переводами»): learning_words + enrich_word × 10."""

    source_tool = "learning_words"
    consumer_tool = "enrich_word"
    fixture_file = "scripts.learning_tools.learning_words.json"
    rows_key = "results"
    row_id_key = "word"
    consumer_arg = "word"

    def build_plan(self):
        e = Entities(raw_misc={"raw_text": "дай 10 слов из Дракулы с переводами"},
                     book_id="PG345", top_n=10)
        plan = plan_mod.build("learning", e)
        assert [s.tool for s in plan.steps][0] == self.source_tool
        return plan


if __name__ == "__main__":
    unittest.main()
