"""v4 — PlanSpec data type, JSON roundtrip, validator, DAG ordering.

The PlanSpec layer is the central abstraction for v4. Everything that
touches LLM-emitted plans goes through here:
  - LLM planner: parses JSON → PlanSpec → validates → router executes
  - Router: topo-sorts PlanSpec, resolves `$sN.field` refs at exec time
  - Observability: PlanSpec serialized back to JSON for the log

Robustness here means the LLM can be sloppy (extra fields, wrong order,
slightly bad arg names) and the validator still catches the real errors
without blocking executable plans.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner import plan_spec as ps


class FromJsonRoundtrip(unittest.TestCase):

    def test_minimal_clarify(self):
        plan = ps.from_json({"clarify": "what book?"})
        self.assertEqual(plan.clarify, "what book?")
        self.assertEqual(plan.steps, [])

    def test_minimal_step(self):
        plan = ps.from_json({
            "steps": [
                {"id": "s1", "tool": "find_book",
                 "args": {"title": "Beowulf"}},
            ],
        })
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].id, "s1")
        self.assertEqual(plan.steps[0].tool, "find_book")
        self.assertEqual(plan.steps[0].args, {"title": "Beowulf"})

    def test_full_compound_plan(self):
        plan = ps.from_json({
            "intent_hint": "etymology_ratio",
            "rationale": "two books, two families, four steps",
            "steps": [
                {"id": "s1", "tool": "resolve_book_title",
                 "args": {"query": "Beowulf"}},
                {"id": "s2", "tool": "resolve_book_title",
                 "args": {"query": "Paradise Lost"}},
                {"id": "s3", "tool": "find_words_by_etymology",
                 "args": {"scope": {"book": "$s1.pg_id"},
                          "family": "germanic"},
                 "needs": ["s1"]},
            ],
            "render_hint": "etymology_ratio_table",
        })
        self.assertEqual(plan.intent_hint, "etymology_ratio")
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.steps[2].needs, ["s1"])
        self.assertEqual(plan.render_hint, "etymology_ratio_table")

    def test_lenient_to_field_aliases(self):
        """LLMs sometimes emit `tool_name`/`step_id`/`arguments`. Tolerate."""
        plan = ps.from_json({
            "steps": [
                {"step_id": "s1", "tool_name": "find_book",
                 "arguments": {"title": "x"}, "depends_on": ["s0"]},
            ],
        })
        self.assertEqual(plan.steps[0].id, "s1")
        self.assertEqual(plan.steps[0].tool, "find_book")
        self.assertEqual(plan.steps[0].args, {"title": "x"})
        self.assertEqual(plan.steps[0].needs, ["s0"])

    def test_to_json_roundtrip_preserves(self):
        src = {
            "intent_hint": "x",
            "rationale": "y",
            "steps": [
                {"id": "s1", "tool": "find_book", "args": {"title": "z"},
                 "needs": [], "optional": False, "rationale": ""},
            ],
            "render_hint": "card",
            "expected_cost": "medium",
            "clarify": None,
            "next_steps": [],
        }
        plan = ps.from_json(src)
        out = ps.to_json(plan)
        # Subset of keys must match
        self.assertEqual(out["intent_hint"], "x")
        self.assertEqual(out["render_hint"], "card")
        self.assertEqual(len(out["steps"]), 1)


class RefParsing(unittest.TestCase):

    def test_parse_basic_ref(self):
        self.assertEqual(ps.parse_ref("$s1"), ("s1", None))
        self.assertEqual(ps.parse_ref("$s2.first_id"), ("s2", "first_id"))
        self.assertEqual(ps.parse_ref("$s10.matches.0.id"),
                         ("s10", "matches.0.id"))

    def test_non_ref_returns_none(self):
        self.assertIsNone(ps.parse_ref("hello"))
        self.assertIsNone(ps.parse_ref("$x"))         # not sN shape
        self.assertIsNone(ps.parse_ref("s1.foo"))     # no leading $
        self.assertIsNone(ps.parse_ref(123))           # not a string
        self.assertIsNone(ps.parse_ref(None))

    def test_walk_path_dict(self):
        obj = {"first_id": "PG1342", "matches": [{"id": "PG1342"}, {"id": "PG2"}]}
        self.assertEqual(ps.walk_path(obj, "first_id"), "PG1342")
        self.assertEqual(ps.walk_path(obj, "matches.0.id"), "PG1342")
        self.assertEqual(ps.walk_path(obj, "matches.1.id"), "PG2")
        self.assertIsNone(ps.walk_path(obj, "matches.99.id"))
        self.assertIsNone(ps.walk_path(obj, "nonexistent"))

    def test_walk_path_no_path_returns_obj(self):
        self.assertEqual(ps.walk_path({"x": 1}, None), {"x": 1})

    def test_resolve_refs_nested(self):
        args = {
            "scope": {"book": "$s1.pg_id"},
            "filters": ["a", "$s2.author"],
            "static": 42,
        }
        results = {
            "s1": {"pg_id": "PG16328"},
            "s2": {"author": "^Milton, John"},
        }
        out = ps.resolve_refs(args, results)
        self.assertEqual(out, {
            "scope": {"book": "PG16328"},
            "filters": ["a", "^Milton, John"],
            "static": 42,
        })


class ValidatorBasic(unittest.TestCase):

    def setUp(self):
        # Tiny fake registry — avoid importing the real one in unit
        # tests so we don't depend on tool side effects.
        class FakeSpec:
            def __init__(self, *, cost="medium", input_schema=None):
                self.cost = cost
                self.input_schema = input_schema or {
                    "type": "object", "properties": {}, "required": [],
                }
        self.reg = {
            "noop_tool": FakeSpec(),
            "needs_pg": FakeSpec(input_schema={
                "type": "object",
                "properties": {"pg_id": {"type": "string"}},
                "required": ["pg_id"],
            }),
            "heavy_tool": FakeSpec(cost="heavy"),
        }

    def test_empty_plan_fails(self):
        plan = ps.PlanSpec()
        rep = ps.validate(plan, registry=self.reg)
        self.assertFalse(rep.ok)
        codes = [i.code for i in rep.errors()]
        self.assertIn("empty_plan", codes)

    def test_clarify_only_is_valid(self):
        plan = ps.PlanSpec(clarify="what book?")
        rep = ps.validate(plan, registry=self.reg)
        self.assertTrue(rep.ok)

    def test_unknown_tool_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="not_a_real_tool", args={}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.errors()[0].code, "unknown_tool")

    def test_missing_required_arg_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="needs_pg", args={}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("missing_required_arg", codes)

    def test_required_arg_satisfied_by_ref(self):
        """Reference value counts as 'present' — the actual value is
        resolved at exec time, validator just checks that the key
        exists in args."""
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={}),
            ps.PlanStepSpec(id="s2", tool="needs_pg",
                             args={"pg_id": "$s1.pg_id"}, needs=["s1"]),
        ])
        rep = ps.validate(plan, registry=self.reg)
        self.assertTrue(rep.ok, msg=str(rep.issues))

    def test_unknown_dep_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={},
                             needs=["s99"]),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("unknown_dep", codes)

    def test_ref_unknown_step_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="needs_pg",
                             args={"pg_id": "$s99.first_id"}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("ref_unknown_step", codes)

    def test_self_dependency_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={},
                             needs=["s1"]),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("self_dependency", codes)

    def test_cycle_detected(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={},
                             needs=["s2"]),
            ps.PlanStepSpec(id="s2", tool="noop_tool", args={},
                             needs=["s1"]),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("cycle", codes)

    def test_duplicate_step_id_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={}),
            ps.PlanStepSpec(id="s1", tool="noop_tool", args={}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        codes = [i.code for i in rep.errors()]
        self.assertIn("duplicate_step_id", codes)

    def test_too_many_heavy_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id=f"s{i}", tool="heavy_tool", args={})
            for i in range(1, 8)
        ])
        rep = ps.validate(plan, registry=self.reg, max_heavy=6)
        codes = [i.code for i in rep.errors()]
        self.assertIn("too_many_heavy", codes)

    def test_too_many_steps_fails(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id=f"s{i}", tool="noop_tool", args={})
            for i in range(1, 14)
        ])
        rep = ps.validate(plan, registry=self.reg, max_steps=12)
        codes = [i.code for i in rep.errors()]
        self.assertIn("too_many_steps", codes)

    def test_unknown_arg_is_error_not_warning(self):
        """Sprint 20 — Stan 2026-05-19 prod: LLM emitted basis=pub_year
        for word_freq_timeline (not in schema). When this was a warning
        the plan passed validation and dispatcher failed at v1 layer.
        Now it's an error → retry triggers with the schema in the
        retry hint."""
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="needs_pg",
                             args={"pg_id": "PG1", "bogus_extra": True}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        self.assertFalse(rep.ok)
        codes = [i.code for i in rep.errors()]
        self.assertIn("unknown_arg", codes)

    def test_internal_underscore_args_exempt(self):
        """`_capped_from` and other underscore-prefixed args are
        v2-wrapper smuggle conventions — they bypass the schema check."""
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="needs_pg",
                             args={"pg_id": "PG1", "_capped_from": 100}),
        ])
        rep = ps.validate(plan, registry=self.reg)
        self.assertTrue(rep.ok, msg=str(rep.issues))


class TopologicalOrdering(unittest.TestCase):

    def test_simple_linear(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="x", args={}),
            ps.PlanStepSpec(id="s2", tool="x", args={"a": "$s1.b"}),
            ps.PlanStepSpec(id="s3", tool="x", args={"a": "$s2.c"}),
        ])
        order = [s.id for s in ps.topological_order(plan)]
        self.assertEqual(order, ["s1", "s2", "s3"])

    def test_fan_out_then_aggregate(self):
        # Etymology-ratio shape: two book resolves feed four etymology
        # calls (two per book). The two resolves can run in either order.
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="x", args={}),
            ps.PlanStepSpec(id="s2", tool="x", args={}),
            ps.PlanStepSpec(id="s3", tool="x",
                             args={"scope": {"book": "$s1.pg_id"}}),
            ps.PlanStepSpec(id="s4", tool="x",
                             args={"scope": {"book": "$s2.pg_id"}}),
        ])
        order = [s.id for s in ps.topological_order(plan)]
        self.assertEqual(set(order[:2]), {"s1", "s2"})
        self.assertEqual(set(order[2:]), {"s3", "s4"})

    def test_cycle_raises(self):
        plan = ps.PlanSpec(steps=[
            ps.PlanStepSpec(id="s1", tool="x", args={"a": "$s2.x"}),
            ps.PlanStepSpec(id="s2", tool="x", args={"a": "$s1.x"}),
        ])
        with self.assertRaises(ValueError):
            ps.topological_order(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
