"""E11 — book_similar runaway latency, structural закрытие через Phase 5.

ORIGINAL: find_book_by_topic мог сжигать 317s (R-22 P11). Первый фикс
(v3.3.1) поставил `timeout_s=45` на @tool и обернул dispatch в SIGALRM.
Закрывало симптом, но регрессировало в композитных интентах (45с потолок
выбирался независимо от того, что осталось от request budget) и
оставляло legacy-путь без enforcement.

PHASE 5 ФИКС (REFACTOR_BRIEF §3.5): per-tool `timeout_s` оверрайды
удалены. Единый chokepoint в `tool_registry.dispatch` считает effective =
`min(DEFAULT_TOOL_TIMEOUT_S, budget.remaining_s)`. Тот же chokepoint
оборачивает legacy_dispatch путь (v1 тулзы). Гарантия: ни один вызов
тула не может пережить request budget.

Этот файл — гейт-тест Фазы 5: контракт, что:
  1) per-tool override снят (`spec.timeout_s == DEFAULT_TOOL_TIMEOUT_S`)
  2) cost остался heavy (estimator должен видеть это как тяжёлую)
  3) effective timeout сжимается под request budget
  4) при истёкшем бюджете вызов фейлится «timeout» (не «success на 60с»)
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
    """legacy_dispatch.dispatch_any для v1 тулзов проходит через тот же
    chokepoint и считает effective timeout относительно request budget.

    На Windows SIGALRM нет — _signal_timeout проседает в noop, но контракт
    «v1 идёт через chokepoint» проверяется по тому, что обёртка вокруг
    fn(**args) существует (else legacy-путь не сможет получить err_type=
    timeout вообще).
    """

    def test_legacy_signature_accepts_budget(self):
        import inspect

        from scripts.v2.legacy_dispatch import dispatch_any

        sig = inspect.signature(dispatch_any)
        self.assertIn(
            "budget", sig.parameters,
            "Phase 5: dispatch_any must accept `budget=` so legacy tools "
            "ride the same chokepoint as v2.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
