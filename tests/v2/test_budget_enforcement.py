"""Budget enforcement in router — v5 Phase 5.

Closes B-R14-10 / Q114 (310s runaway) structurally: no plan can exceed
the request envelope. Router aborts mid-DAG when budget expires,
returns partial results with budget_exceeded=True.

Coverage:
  - execute() without budget → backward compat (1209-test baseline)
  - execute() with budget → soft abort
  - execute_spec() without budget → backward compat (DAG path)
  - execute_spec() with budget → soft abort + budget_exceeded event
  - usage.exceeded() / tick() math
"""
from __future__ import annotations

import sys
import time
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import budget as b
from scripts.v2._types import ToolResult, Coverage
from scripts.v2.planner import router as router_mod
from scripts.v2.planner.plan import PlanStep, QueryPlan
from scripts.v2.planner.plan_spec import PlanSpec, PlanStepSpec as SpecStep


# =====================================================================
# BudgetUsage math
# =====================================================================


class BudgetUsageMath(unittest.TestCase):
    def test_fresh_usage_not_exceeded(self):
        budget = b.RequestBudget(wall_clock_s=10.0)
        usage = b.BudgetUsage()
        self.assertFalse(usage.exceeded(budget))

    def test_exceeded_after_wall_clock(self):
        budget = b.RequestBudget(wall_clock_s=0.05)
        usage = b.BudgetUsage()
        time.sleep(0.06)
        self.assertTrue(usage.exceeded(budget))
        self.assertEqual(usage.exceeded_at, "wall_clock")

    def test_exceeded_after_tool_calls(self):
        budget = b.RequestBudget(wall_clock_s=10.0, tool_calls_max=2)
        usage = b.BudgetUsage()
        usage.tool_calls_used = 3
        self.assertTrue(usage.exceeded(budget))
        self.assertEqual(usage.exceeded_at, "tool_calls")


# =====================================================================
# execute() v3 path
# =====================================================================


def _fake_step(tool: str, args: dict | None = None, optional: bool = False) -> PlanStep:
    return PlanStep(tool=tool, args=args or {}, depends_on=[],
                    inject_result_as=None, optional=optional)


def _stub_dispatch_factory(per_call_delay: float = 0.0):
    """Returns a dispatch_any-shaped stub that sleeps + returns success.

    Accepts and ignores `budget=` kwarg (Phase 5 added it to the dispatch
    signature so the chokepoint can compute effective tool timeout)."""
    def _stub(tool: str, args: dict, *, budget=None) -> ToolResult:
        if per_call_delay > 0:
            time.sleep(per_call_delay)
        return ToolResult.success(
            tool=tool, data={"x": 1},
            coverage=Coverage(books_matched=1, books_total=1),
        )
    return _stub


class ExecuteV3Backwards(unittest.TestCase):
    def test_without_budget_succeeds(self):
        """No budget kwarg → behaves exactly as before."""
        plan = QueryPlan(
            intent="dummy", entities=None,
            steps=[_fake_step("tool_a"), _fake_step("tool_b")],
        )
        with mock.patch.object(router_mod, "dispatch_any",
                                 _stub_dispatch_factory()):
            rr = router_mod.execute(plan)
        self.assertEqual(rr.kind, "results")
        self.assertEqual(len(rr.results), 2)
        self.assertFalse(rr.budget_exceeded)


class ExecuteV3Budget(unittest.TestCase):
    def test_budget_aborts_after_n_steps(self):
        """Tight budget (50ms) + each step takes 30ms — after 2-3 steps
        budget is exceeded, remaining aborted."""
        plan = QueryPlan(
            intent="dummy", entities=None,
            steps=[_fake_step(f"tool_{i}") for i in range(5)],
        )
        budget = b.RequestBudget(wall_clock_s=0.05)
        with mock.patch.object(router_mod, "dispatch_any",
                                 _stub_dispatch_factory(per_call_delay=0.030)):
            rr = router_mod.execute(plan, budget=budget)
        self.assertTrue(rr.budget_exceeded)
        # Some steps executed before abort, not all 5
        self.assertLess(len(rr.results), 5)
        # Last event is budget_exceeded
        budget_evts = [e for e in rr.events if e.kind == "budget_exceeded"]
        self.assertEqual(len(budget_evts), 1)
        self.assertIn("budget", budget_evts[0].reason)

    def test_loose_budget_completes(self):
        plan = QueryPlan(
            intent="dummy", entities=None,
            steps=[_fake_step(f"tool_{i}") for i in range(3)],
        )
        budget = b.RequestBudget(wall_clock_s=5.0)
        with mock.patch.object(router_mod, "dispatch_any",
                                 _stub_dispatch_factory()):
            rr = router_mod.execute(plan, budget=budget)
        self.assertFalse(rr.budget_exceeded)
        self.assertEqual(len(rr.results), 3)


# =====================================================================
# execute_spec() v4 DAG path
# =====================================================================


class ExecuteSpecBackwards(unittest.TestCase):
    def test_without_budget_succeeds(self):
        spec = PlanSpec(
            intent_hint="dummy",
            steps=[
                SpecStep(id="s1", tool="tool_a", args={}),
                SpecStep(id="s2", tool="tool_b", args={}),
            ],
        )
        with mock.patch.object(router_mod, "dispatch_any",
                                 _stub_dispatch_factory()):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        self.assertEqual(len(rr.results), 2)
        self.assertFalse(rr.budget_exceeded)


class ExecuteSpecBudget(unittest.TestCase):
    def test_budget_aborts_dag_execution(self):
        spec = PlanSpec(
            intent_hint="dummy",
            steps=[SpecStep(id=f"s{i}", tool=f"tool_{i}", args={})
                   for i in range(5)],
        )
        budget = b.RequestBudget(wall_clock_s=0.05)
        with mock.patch.object(router_mod, "dispatch_any",
                                 _stub_dispatch_factory(per_call_delay=0.030)):
            rr = router_mod.execute_spec(spec, budget=budget)
        self.assertTrue(rr.budget_exceeded)
        self.assertLess(len(rr.results), 5)
        # The budget_exceeded event is emitted
        budget_evts = [e for e in rr.events if e.kind == "budget_exceeded"]
        self.assertEqual(len(budget_evts), 1)
        self.assertIn("wall_clock", budget_evts[0].reason)


# Phase 1 (2026-05-22) — `render_v5` + prose-binder were deleted (R1: no
# dark code behind off flags). The budget-exceeded surface lives now in
# the router (RouterResult.budget_exceeded + StepEvent budget_exceeded)
# and in rag_v2's legacy renderer — both already exercised above.


if __name__ == "__main__":
    unittest.main(verbosity=2)
