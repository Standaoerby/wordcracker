"""Tool Router tests — verifies the deterministic execute() loop.

Uses monkeypatched v1 + v2 tools so we don't touch the real corpus. The router
is pure orchestration: dispatch → thread result → continue."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _stub_find_book(**kw):
    return {
        "title_query": kw.get("title", ""),
        "matches": [{"id": "PG1342", "title": "Pride and Prejudice",
                     "author": "Austen, Jane", "downloads": 100}],
        "total_matches": 1,
    }


def _stub_affinity_book(**kw):
    return {"pg_id": kw.get("pg_id", "?"),
            "top_words": [{"word": "civility"}]}


def _fake_v1_query():
    """Stub of scripts.rag_query — provides TOOL_DISPATCH for legacy_dispatch."""
    m = types.ModuleType("scripts.rag_query")
    m.TOOL_DISPATCH = {
        "affinity_by_author": lambda **kw: {
            "author_regex": kw["author_regex"],
            "top_words": [{"word": "wicket", "affinity": 22.1}],
        },
        "compare_authors": lambda **kw: {
            "author1_regex": kw["author1_regex"],
            "author2_regex": kw["author2_regex"],
            "top_unique_a": [{"word": "blighter"}],
            "top_unique_b": [{"word": "fish"}],
        },
        "affinity_by_book": _stub_affinity_book,
        "book_archaic_words": lambda **kw: {
            "pg_id": kw["pg_id"], "archaic": [{"word": "ye"}],
        },
        "find_book": _stub_find_book,
        "boom": lambda **_: (_ for _ in ()).throw(RuntimeError("kaboom")),
    }
    return m


def _fake_v1_tools():
    """Stub of scripts.rag_tools — v2 find_book wrapper imports from here."""
    m = types.ModuleType("scripts.rag_tools")
    m.find_book = _stub_find_book
    m.author_metadata = lambda author_regex: {
        "author": "Test", "books_total": 1,
    }
    m.top_authors_by = lambda **kw: {"metric": kw.get("metric", "books"),
                                     "top": []}
    m.top_authors_by_country = lambda **kw: {"country": kw["country"], "top": []}
    return m


class RouterClarifyAndOutOfScope(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules["scripts.rag_query"] = _fake_v1_query()
        sys.modules["scripts.rag_tools"] = _fake_v1_tools()
        # Reset v2 registry to a known set.
        from scripts.v2.tool_registry import REGISTRY
        self._snap = dict(REGISTRY); REGISTRY.clear()
        # Reload v2 tools to repopulate.
        for mod in list(sys.modules):
            if mod.startswith("scripts.v2.tools"):
                del sys.modules[mod]
        import scripts.v2.tools  # noqa: F401

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear(); REGISTRY.update(self._snap)
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)

    def test_clarify_plan_returns_clarify(self):
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(intent="clarify",
                         entities=type("E", (), {})(),
                         steps=[],
                         needs_clarify=True,
                         clarify_question="who is the author?")
        r = execute(plan)
        self.assertEqual(r.kind, "clarify")
        self.assertIn("author", r.message)

    def test_out_of_scope_plan(self):
        from scripts.v2.planner.plan import QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(intent="out_of_scope",
                         entities=type("E", (), {})(),
                         steps=[],
                         out_of_scope_reason="no fiction generation")
        r = execute(plan)
        self.assertEqual(r.kind, "out_of_scope")
        self.assertEqual(r.message, "no fiction generation")


class RouterExecutesSteps(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules["scripts.rag_query"] = _fake_v1_query()
        sys.modules["scripts.rag_tools"] = _fake_v1_tools()
        from scripts.v2 import legacy_dispatch
        legacy_dispatch._LEGACY_DISPATCH_CACHE.clear()
        legacy_dispatch._LEGACY_DISPATCH_CACHE.update({"dispatch": None, "loaded": False})
        # Snapshot + rebuild v2 registry so find_book wrapper resolves the stub.
        from scripts.v2.tool_registry import REGISTRY
        self._snap = dict(REGISTRY); REGISTRY.clear()
        for mod in list(sys.modules):
            if mod.startswith("scripts.v2.tools"):
                del sys.modules[mod]
        import scripts.v2.tools  # noqa: F401

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear(); REGISTRY.update(self._snap)
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)

    def test_single_legacy_step(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(
            intent="author_vocab", entities=Entities(author_regex="^Wodehouse,"),
            steps=[PlanStep(tool="affinity_by_author",
                            args={"author_regex": "^Wodehouse,"})],
        )
        r = execute(plan)
        self.assertEqual(r.kind, "results")
        self.assertEqual(len(r.results), 1)
        self.assertTrue(r.results[0].ok)
        self.assertEqual(r.results[0].data["author_regex"], "^Wodehouse,")

    def test_chained_steps_with_pg_injection(self):
        """find_book → affinity_by_book. v2 find_book uses real wrapper which
        threads first_id through ToolResult.data."""
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(
            intent="book_vocab",
            entities=Entities(book_title="Pride"),
            steps=[
                PlanStep(tool="find_book", args={"title": "Pride"}),
                PlanStep(tool="affinity_by_book", args={"top": 30},
                         depends_on=[0], inject_result_as="pg_id"),
            ],
        )
        r = execute(plan)
        self.assertEqual(r.kind, "results")
        self.assertEqual(len(r.results), 2)
        self.assertTrue(r.results[0].ok)
        self.assertTrue(r.results[1].ok)
        self.assertEqual(r.results[1].data["pg_id"], "PG1342")

    def test_failed_required_step_stops(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(
            intent="author_vocab", entities=Entities(),
            steps=[
                PlanStep(tool="boom", args={}),
                PlanStep(tool="affinity_by_author",
                         args={"author_regex": "^X,"}),
            ],
        )
        r = execute(plan)
        self.assertEqual(r.kind, "results")
        self.assertEqual(len(r.results), 1)  # second step never ran
        self.assertFalse(r.results[0].ok)
        self.assertEqual(r.results[0].error.type, "internal")

    def test_optional_failed_step_continues(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(
            intent="author_vocab", entities=Entities(),
            steps=[
                PlanStep(tool="boom", args={}, optional=True),
                PlanStep(tool="affinity_by_author",
                         args={"author_regex": "^Wodehouse,"}),
            ],
        )
        r = execute(plan)
        self.assertEqual(len(r.results), 2)
        self.assertFalse(r.results[0].ok)
        self.assertTrue(r.results[1].ok)


class RouterStreamEvents(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules["scripts.rag_query"] = _fake_v1_query()
        sys.modules["scripts.rag_tools"] = _fake_v1_tools()
        from scripts.v2 import legacy_dispatch
        legacy_dispatch._LEGACY_DISPATCH_CACHE.clear()
        legacy_dispatch._LEGACY_DISPATCH_CACHE.update({"dispatch": None, "loaded": False})

    def tearDown(self):
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)

    def test_stream_emits_expected_events(self):
        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute_stream
        plan = QueryPlan(
            intent="author_vocab", entities=Entities(author_regex="^X,"),
            steps=[PlanStep(tool="affinity_by_author",
                            args={"author_regex": "^X,"})],
        )
        events = list(execute_stream(plan))
        kinds = [e["event"] for e in events]
        # intent → plan → tool_call → tool_result → done
        self.assertEqual(kinds[0], "intent")
        self.assertEqual(kinds[1], "plan")
        self.assertEqual(kinds[2], "tool_call")
        self.assertEqual(kinds[3], "tool_result")
        self.assertEqual(kinds[-1], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
