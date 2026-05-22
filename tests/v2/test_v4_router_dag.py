"""v4 — router DAG support: execute_spec + $sN.field threading.

The v4 router takes a PlanSpec and runs it like a DAG. After each step,
its `ToolResult.data` becomes available to later steps via `$sN.field`
references in args. Failure handling, optional-step semantics, and the
stream variant all mirror the v3 router path so chat_server's SSE
handler doesn't need to fork.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.planner import plan_spec as ps
from scripts.v2.planner import router as router_mod


def _fake_tool_result(tool: str, data: dict, ok: bool = True) -> ToolResult:
    if ok:
        return ToolResult.success(
            tool=tool, data=data,
            coverage=Coverage(books_matched=1, books_total=1),
        )
    return ToolResult.fail(
        tool=tool, err_type="internal", message="forced",
    )


class ExecuteSpecBasic(unittest.TestCase):

    def test_clarify_only_spec(self):
        spec = ps.PlanSpec(clarify="which book?")
        rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "clarify")
        self.assertEqual(rr.message, "which book?")

    def test_no_steps_no_clarify(self):
        spec = ps.PlanSpec()
        rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "no_steps")

    def test_single_step_runs(self):
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="x_tool",
                              args={"q": "Beowulf"}),
        ])
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          return_value=_fake_tool_result("x_tool",
                                                          {"pg_id": "PG1"})):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        self.assertEqual(len(rr.results), 1)
        self.assertEqual(rr.results[0].data["pg_id"], "PG1")

    def test_dependency_ref_resolved(self):
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="resolver",
                              args={"q": "Beowulf"}),
            ps.PlanStepSpec(id="s2", tool="reader",
                              args={"pg_id": "$s1.pg_id"},
                              needs=["s1"]),
        ])
        captured_args = []
        def fake_dispatch(name, args, **_kw):
            captured_args.append((name, args))
            if name == "resolver":
                return _fake_tool_result(name, {"pg_id": "PG16328"})
            return _fake_tool_result(name, {"flesch": 60})
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        # s2 should have received the resolved pg_id
        self.assertEqual(captured_args[1][1]["pg_id"], "PG16328")

    def test_nested_dict_ref_resolved(self):
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="resolver",
                              args={"q": "Beowulf"}),
            ps.PlanStepSpec(id="s2", tool="reader",
                              args={"scope": {"book": "$s1.pg_id"}},
                              needs=["s1"]),
        ])
        captured = []
        def fake_dispatch(name, args, **_kw):
            captured.append((name, args))
            return _fake_tool_result(
                name,
                {"pg_id": "PG16328"} if name == "resolver" else {"x": 1},
            )
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            router_mod.execute_spec(spec)
        self.assertEqual(captured[1][1], {"scope": {"book": "PG16328"}})

    def test_failure_aborts_unless_optional(self):
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="a", args={}),
            ps.PlanStepSpec(id="s2", tool="b", args={}, needs=["s1"]),
        ])
        def fake_dispatch(name, _args, **_kw):
            return _fake_tool_result(name, {}, ok=(name != "a"))
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        # s1 failed (not ok) and not optional → s2 not executed
        self.assertEqual(len(rr.results), 1)
        self.assertFalse(rr.results[0].ok)

    def test_optional_failure_continues(self):
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="a", args={}, optional=True),
            ps.PlanStepSpec(id="s2", tool="b", args={}),
        ])
        def fake_dispatch(name, _args, **_kw):
            return _fake_tool_result(name, {}, ok=(name != "a"))
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        # s1 failed optional + s2 ran
        self.assertEqual(len(rr.results), 2)

    def test_leaf_failure_continues_siblings(self):
        """Sprint 20 — Stan 2026-05-19 prod regression: multi-word
        fan-out (3 parallel word_freq_timeline) aborted on first
        failure, killing siblings. None of s1/s2/s3 has dependents →
        a failure in s1 should NOT prevent s2 + s3 from running."""
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="x", args={"word": "telephone"}),
            ps.PlanStepSpec(id="s2", tool="x", args={"word": "automobile"}),
            ps.PlanStepSpec(id="s3", tool="x", args={"word": "aeroplane"}),
        ])
        call_words: list[str] = []
        def fake_dispatch(name, args, **_kw):
            call_words.append(args.get("word"))
            # First call fails, others succeed
            ok = args.get("word") != "telephone"
            return _fake_tool_result(name, {"freq": 5}, ok=ok)
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        # All 3 dispatched (leaf failure did NOT abort)
        self.assertEqual(len(rr.results), 3)
        self.assertEqual(set(call_words),
                          {"telephone", "automobile", "aeroplane"})
        # First result is the failed one
        self.assertFalse(rr.results[0].ok)
        # Others succeeded
        self.assertTrue(rr.results[1].ok)
        self.assertTrue(rr.results[2].ok)

    def test_failure_with_dependent_still_aborts(self):
        """Regression: when a failed step DOES have a downstream
        dependent, we still hard-abort (cascade) — only LEAF failures
        get the soft treatment."""
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="resolver", args={"q": "X"}),
            ps.PlanStepSpec(id="s2", tool="reader",
                              args={"pg_id": "$s1.pg_id"}, needs=["s1"]),
        ])
        called: list[str] = []
        def fake_dispatch(name, _args, **_kw):
            called.append(name)
            return _fake_tool_result(name, {}, ok=(name != "resolver"))
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        # s1 failed and has dependent s2 → abort, s2 not dispatched
        self.assertEqual(called, ["resolver"])
        self.assertEqual(len(rr.results), 1)
        self.assertFalse(rr.results[0].ok)


class ExecuteSpecDAGOrdering(unittest.TestCase):

    def test_topological_order_independent_steps(self):
        """s1 and s2 don't depend on each other — both should run."""
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="a", args={"q": "x"}),
            ps.PlanStepSpec(id="s2", tool="b", args={"q": "y"}),
        ])
        order: list[str] = []
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=lambda name, _args, **_kw: (
                              order.append(name) or
                              _fake_tool_result(name, {}))):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        self.assertEqual(sorted(order), ["a", "b"])

    def test_fan_out_etymology_ratio_shape(self):
        """Two resolves feed four etymology calls. Resolve order is
        free; etymology calls must follow their respective resolve."""
        spec = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="resolve_book_title",
                              args={"query": "Beowulf"}),
            ps.PlanStepSpec(id="s2", tool="resolve_book_title",
                              args={"query": "Paradise Lost"}),
            ps.PlanStepSpec(id="s3", tool="find_words_by_etymology",
                              args={"scope": {"book": "$s1.pg_id"},
                                    "family": "germanic"}),
            ps.PlanStepSpec(id="s4", tool="find_words_by_etymology",
                              args={"scope": {"book": "$s1.pg_id"},
                                    "family": "latin"}),
            ps.PlanStepSpec(id="s5", tool="find_words_by_etymology",
                              args={"scope": {"book": "$s2.pg_id"},
                                    "family": "germanic"}),
            ps.PlanStepSpec(id="s6", tool="find_words_by_etymology",
                              args={"scope": {"book": "$s2.pg_id"},
                                    "family": "latin"}),
        ])
        # Mock: resolvers return pg_id, etymology returns top-words
        def fake_dispatch(name, args, **_kw):
            if name == "resolve_book_title":
                pg = "PG16328" if args["query"] == "Beowulf" else "PG26"
                return _fake_tool_result(name, {"pg_id": pg})
            return _fake_tool_result(name, {"top": [{"word": "x"}]})
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            rr = router_mod.execute_spec(spec)
        self.assertEqual(rr.kind, "results")
        self.assertEqual(len(rr.results), 6)


class StreamingVariant(unittest.TestCase):

    def test_stream_emits_events_in_order(self):
        spec = ps.PlanSpec(
            intent_hint="readability_lookup",
            steps=[
                ps.PlanStepSpec(id="s1", tool="resolve_book_title",
                                  args={"query": "Beowulf"}),
                ps.PlanStepSpec(id="s2", tool="book_readability",
                                  args={"pg_id": "$s1.pg_id"},
                                  needs=["s1"]),
            ],
        )
        def fake_dispatch(name, args, **_kw):
            return _fake_tool_result(
                name,
                {"pg_id": "PG16328"} if name == "resolve_book_title"
                else {"flesch": 60},
            )
        with mock.patch("scripts.v2.planner.router.dispatch_any",
                          side_effect=fake_dispatch):
            events = list(router_mod.execute_spec_stream(spec))

        kinds = [e.get("event") for e in events]
        self.assertEqual(kinds[0], "intent")
        self.assertIn("plan", kinds)
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        self.assertEqual(kinds[-1], "done")

    def test_stream_clarify_short_circuits(self):
        spec = ps.PlanSpec(clarify="what?")
        events = list(router_mod.execute_spec_stream(spec))
        self.assertEqual(events[0]["event"], "intent")
        self.assertEqual(events[1]["event"], "clarify")
        self.assertEqual(events[-1]["event"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
