"""E11 — book_similar runaway latency, structural закрытие через Phase 5.

ORIGINAL: find_book_by_topic мог сжигать 317s (R-22 P11). Первый фикс
(v3.3.1) поставил `timeout_s=45` на @tool и обернул dispatch в SIGALRM.
Закрывало симптом, но регрессировало в композитных интентах (45с потолок
выбирался независимо от того, что осталось от request budget) и
оставляло legacy-путь без enforcement.

PHASE 5 ФИКС (REFACTOR_BRIEF §3.5): per-tool `timeout_s` оверрайды
удалены. Единый chokepoint в `tool_registry.dispatch` считает effective =
`min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s)`. T5 (2026-05-23) —
`legacy_dispatch.py` удалён, тот же `dispatch` сам идёт по v1-пути при
промахе в REGISTRY. Гарантия: ни один вызов тула не может пережить
request budget, независимо от того, v1 он или v2.

Этот файл — гейт-тест Фазы 5: контракт, что:
  1) per-tool override снят (`spec.timeout_s == DEFAULT_TOOL_TIMEOUT_S`)
  2) cost остался heavy (estimator должен видеть это как тяжёлую)
  3) effective timeout сжимается под request budget
  4) при истёкшем бюджете вызов фейлится «timeout» (не «success на 60с»)
  5) v1-тулзы (не в REGISTRY) проходят через тот же chokepoint и тоже
     ограничены `effective_timeout_s(DEFAULT_TOOL_TIMEOUT_S, budget)`
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FindBookByTopicPhase5Contract(unittest.TestCase):
    """Tool spec не имеет per-tool timeout override после Phase 5."""

    def test_tool_has_no_per_tool_timeout_override(self):
        import scripts.v2.tools  # noqa: F401  triggers registration
        from scripts.v2.tool_registry import DEFAULT_TOOL_TIMEOUT_S, REGISTRY

        spec = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(spec, "find_book_by_topic must be registered")
        # Phase 5: per-tool overrides removed → spec inherits DEFAULT.
        self.assertEqual(
            spec.timeout_s, DEFAULT_TOOL_TIMEOUT_S,
            f"find_book_by_topic should use DEFAULT_TOOL_TIMEOUT_S "
            f"(Phase 5 removes per-tool overrides); got {spec.timeout_s}",
        )

    def test_tool_cost_heavy(self):
        import scripts.v2.tools  # noqa: F401
        from scripts.v2.tool_registry import REGISTRY

        spec = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(spec)
        self.assertEqual(
            spec.cost, "heavy",
            f"find_book_by_topic should be cost=heavy (E11); got {spec.cost}",
        )


class EffectiveTimeoutUnderBudget(unittest.TestCase):
    """Phase 5 chokepoint: effective timeout = min(spec, budget.remaining)."""

    def test_budget_caps_effective_timeout(self):
        from scripts.v2.budget import RequestBudget
        from scripts.v2.tool_registry import effective_timeout_s

        # Spec=60s, budget=10s → effective=10s
        b = RequestBudget(wall_clock_s=10.0)
        self.assertLessEqual(effective_timeout_s(60, b), 10)
        self.assertGreaterEqual(effective_timeout_s(60, b), 9)

        # Spec=60s, без budget → effective=60s
        self.assertEqual(effective_timeout_s(60, None), 60)

        # Spec=0 (unlimited) с budget → effective ≈ budget.remaining
        b2 = RequestBudget(wall_clock_s=5.0)
        self.assertLessEqual(effective_timeout_s(0, b2), 5)

    def test_drained_budget_returns_minimal_timeout(self):
        """Когда budget уже исчерпан, effective_timeout_s >= 1 — каждый
        тул успевает ответить timeout, а не SIGALRM(0) = бесконечность."""
        from scripts.v2.budget import RequestBudget
        from scripts.v2.tool_registry import effective_timeout_s

        b = RequestBudget(wall_clock_s=0.01)
        time.sleep(0.05)
        self.assertGreaterEqual(effective_timeout_s(60, b), 1)


class LegacyDispatchRespectsBudget(unittest.TestCase):
    """v1 тулзы (не в REGISTRY) проходят через тот же `dispatch` chokepoint
    и считают effective timeout относительно request budget.

    T5 (2026-05-23): `legacy_dispatch.py` удалён. `tool_registry.dispatch`
    сам ловит «name not in REGISTRY» и идёт в `_dispatch_legacy`, где
    эффективный тайм-аут = `min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s)`.

    На Windows SIGALRM нет — `_signal_timeout` сидит в no-op. Контракт
    «v1 идёт через chokepoint» проверяется на уровне:
      (a) сигнатура `dispatch` принимает `budget=`;
      (b) при вызове v1-имени через `dispatch` `_signal_timeout`
          вызывается с `effective_timeout_s(DEFAULT_TOOL_TIMEOUT_S, budget)`.
    """

    def test_dispatch_signature_accepts_budget(self):
        import inspect

        from scripts.v2.tool_registry import dispatch

        sig = inspect.signature(dispatch)
        self.assertIn(
            "budget", sig.parameters,
            "Phase 5: dispatch must accept `budget=` so v1 tools ride "
            "the same chokepoint as v2.",
        )

    def test_legacy_path_uses_effective_timeout(self):
        """v1 тул (не в REGISTRY) проходит через _signal_timeout с
        effective_timeout = min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s).
        """
        import sys
        import types
        import unittest.mock as mock

        from scripts.v2 import tool_registry
        from scripts.v2.budget import RequestBudget

        captured_seconds: list[int] = []

        # Stub a v1 module with one tool that just returns a dict.
        m = types.ModuleType("scripts.rag_query")
        m.TOOL_DISPATCH = {
            "fake_v1_tool": lambda **kw: {"answer": 42, "rows": []},
        }
        prev_rag_query = sys.modules.get("scripts.rag_query")
        sys.modules["scripts.rag_query"] = m
        tool_registry._reset_legacy_cache_for_tests()

        import contextlib

        @contextlib.contextmanager
        def _capturing_timeout(seconds: int):
            captured_seconds.append(seconds)
            yield

        try:
            # Tight budget (5s) — effective should be <= 5.
            budget = RequestBudget(wall_clock_s=5.0)
            with mock.patch.object(tool_registry, "_signal_timeout",
                                    _capturing_timeout):
                result = tool_registry.dispatch(
                    "fake_v1_tool", {}, budget=budget,
                )
            self.assertTrue(captured_seconds,
                            "_signal_timeout must wrap v1 dispatch")
            self.assertLessEqual(
                captured_seconds[-1], 5,
                "v1 effective timeout must be capped by budget.remaining_s",
            )
            self.assertGreaterEqual(captured_seconds[-1], 1)
            # The result should be a successful ToolResult wrapping the v1 raw.
            self.assertTrue(result.ok)
            self.assertEqual(result.tool, "fake_v1_tool")
        finally:
            tool_registry._reset_legacy_cache_for_tests()
            if prev_rag_query is None:
                sys.modules.pop("scripts.rag_query", None)
            else:
                sys.modules["scripts.rag_query"] = prev_rag_query

    def test_legacy_path_default_ceiling_without_budget(self):
        """Без budget effective timeout = DEFAULT_TOOL_TIMEOUT_S."""
        import sys
        import types
        import unittest.mock as mock

        from scripts.v2 import tool_registry

        captured_seconds: list[int] = []
        m = types.ModuleType("scripts.rag_query")
        m.TOOL_DISPATCH = {"fake_v1_tool": lambda **kw: {"ok": True}}
        prev_rag_query = sys.modules.get("scripts.rag_query")
        sys.modules["scripts.rag_query"] = m
        tool_registry._reset_legacy_cache_for_tests()

        import contextlib

        @contextlib.contextmanager
        def _capturing_timeout(seconds: int):
            captured_seconds.append(seconds)
            yield

        try:
            with mock.patch.object(tool_registry, "_signal_timeout",
                                    _capturing_timeout):
                tool_registry.dispatch("fake_v1_tool", {})
            self.assertEqual(
                captured_seconds[-1],
                tool_registry.DEFAULT_TOOL_TIMEOUT_S,
                "v1 path without budget must use DEFAULT_TOOL_TIMEOUT_S",
            )
        finally:
            tool_registry._reset_legacy_cache_for_tests()
            if prev_rag_query is None:
                sys.modules.pop("scripts.rag_query", None)
            else:
                sys.modules["scripts.rag_query"] = prev_rag_query


if __name__ == "__main__":
    unittest.main(verbosity=2)
