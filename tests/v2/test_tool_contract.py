"""Unit tests for the v2 tool contract: types, registry, FilterSpec, pilot tool.

Run from repo root:
    PYTHONPATH=. python -m pytest tests/v2/test_tool_contract.py -v
or:
    PYTHONPATH=. python tests/v2/test_tool_contract.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

# Allow `from scripts.v2 import ...` when tests run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tool_registry  # noqa: E402
from scripts.v2.filters import FilterSpec  # noqa: E402
from scripts.v2.tool_registry import REGISTRY, build_tools_spec, dispatch  # noqa: E402
from scripts.v2._types import (  # noqa: E402
    Coverage,
    SourceInfo,
    ToolError,
    ToolResult,
    ToolWarning,
)


class ToolResultSerialization(unittest.TestCase):
    def test_success_minimal(self):
        r = ToolResult.success(tool="t", data={"x": 1})
        self.assertTrue(r.ok)
        self.assertEqual(r.tool, "t")
        self.assertEqual(r.data, {"x": 1})

    def test_to_dict_drops_empty(self):
        r = ToolResult.success(tool="t", data={"x": 1})
        d = r.to_dict()
        self.assertNotIn("warnings", d)
        self.assertNotIn("error", d)

    def test_to_llm_string_truncates(self):
        big = {"items": list(range(1000))}
        r = ToolResult.success(tool="t", data=big)
        s = r.to_llm_string(max_chars=500)
        self.assertLessEqual(len(s), 520)  # 500 + truncation suffix
        self.assertTrue(s.startswith("{"))

    def test_to_llm_string_drops_source_info(self):
        r = ToolResult.success(
            tool="t", data={"x": 1},
            source_info=SourceInfo(corpus_version="v1", analytics_version="2.0"),
        )
        s = r.to_llm_string()
        self.assertNotIn("source_info", s)
        self.assertNotIn("runtime_ms", s)

    def test_fail_carries_error(self):
        r = ToolResult.fail(tool="t", err_type="not_found", message="nope")
        self.assertFalse(r.ok)
        self.assertIsNotNone(r.error)
        self.assertEqual(r.error.type, "not_found")

    def test_from_legacy_error_dict(self):
        legacy = {"error": "bad", "details": "stack"}
        r = ToolResult.from_legacy("t", legacy, runtime_ms=10, source_info=None)
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "internal")
        self.assertEqual(r.error.message, "bad")

    def test_from_legacy_plain_dict(self):
        r = ToolResult.from_legacy("t", {"x": 1}, runtime_ms=10, source_info=None)
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"x": 1})


class FilterSpecLegacy(unittest.TestCase):
    def test_all_corpus(self):
        fs = FilterSpec.from_legacy_scope("all_corpus")
        self.assertIsNone(fs.author_regex)
        self.assertIsNone(fs.pg_id)

    def test_book_scope(self):
        fs = FilterSpec.from_legacy_scope({"book": "PG1342"})
        self.assertEqual(fs.pg_id, "PG1342")

    def test_author_scope(self):
        fs = FilterSpec.from_legacy_scope({"author": "^Wilde,", "country": "GB"})
        self.assertEqual(fs.author_regex, "^Wilde,")
        self.assertEqual(fs.country, "GB")

    def test_pg_string(self):
        fs = FilterSpec.from_legacy_scope("PG345")
        self.assertEqual(fs.pg_id, "PG345")

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            FilterSpec.from_legacy_scope(12345)

    def test_explain_handles_empty(self):
        self.assertIn("корпус", FilterSpec().explain())


class RegistryAndDispatch(unittest.TestCase):
    def setUp(self):
        # Snapshot + reset registry for isolation.
        self._snapshot = dict(REGISTRY)
        REGISTRY.clear()

    def tearDown(self):
        REGISTRY.clear()
        REGISTRY.update(self._snapshot)

    def test_register_and_dispatch(self):
        @tool_registry.tool(
            name="add",
            category="corpus_meta",
            description="adds two numbers",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            cost="cheap",
        )
        def _add(a, b):
            return ToolResult.success(tool="add", data=a + b)

        self.assertIn("add", REGISTRY)
        r = dispatch("add", {"a": 2, "b": 3})
        self.assertTrue(r.ok)
        self.assertEqual(r.data, 5)
        self.assertGreaterEqual(r.runtime_ms, 0)

    def test_duplicate_registration_raises(self):
        @tool_registry.tool(
            name="dup", category="corpus_meta", description="",
            input_schema={"type": "object"}, cost="cheap",
        )
        def _f1():
            return ToolResult.success(tool="dup", data=None)

        with self.assertRaises(ValueError):
            @tool_registry.tool(
                name="dup", category="corpus_meta", description="",
                input_schema={"type": "object"}, cost="cheap",
            )
            def _f2():
                return ToolResult.success(tool="dup", data=None)

    def test_unknown_tool_returns_fail(self):
        r = dispatch("nope", {})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "not_found")

    def test_exception_wrapped(self):
        @tool_registry.tool(
            name="boom", category="corpus_meta", description="",
            input_schema={"type": "object"}, cost="cheap",
        )
        def _boom():
            raise RuntimeError("kaboom")

        r = dispatch("boom", {})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "internal")
        self.assertIn("kaboom", r.error.message)

    def test_bad_args_marked_invalid(self):
        @tool_registry.tool(
            name="need_a", category="corpus_meta", description="",
            input_schema={"type": "object",
                          "properties": {"a": {"type": "integer"}},
                          "required": ["a"]},
            cost="cheap",
        )
        def _need_a(a):
            return ToolResult.success(tool="need_a", data=a)

        r = dispatch("need_a", {})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")

    def test_filter_arg_coerced(self):
        captured = {}

        @tool_registry.tool(
            name="needs_filter", category="corpus_meta", description="",
            input_schema={
                "type": "object",
                "properties": {"filter": {"type": "object"}},
                "required": ["filter"],
            },
            cost="cheap",
        )
        def _needs_filter(filter):  # noqa: A002 — match schema name
            captured["filter"] = filter
            return ToolResult.success(tool="needs_filter", data=None)

        dispatch("needs_filter", {"filter": {"author_regex": "^Wilde,", "country": "GB"}})
        self.assertIsInstance(captured["filter"], FilterSpec)
        self.assertEqual(captured["filter"].author_regex, "^Wilde,")
        self.assertEqual(captured["filter"].country, "GB")

    def test_build_tools_spec(self):
        @tool_registry.tool(
            name="x", category="corpus_meta", description="x",
            input_schema={"type": "object"}, cost="cheap",
        )
        def _x():
            return ToolResult.success(tool="x", data=None)

        spec = build_tools_spec()
        self.assertEqual(len(spec), 1)
        self.assertEqual(spec[0]["function"]["name"], "x")


class CorpusOverviewPilot(unittest.TestCase):
    """Smoke-test pilot tool. Runs on non-container env where paths don't exist —
    we expect ok=True with warnings about missing dirs."""

    def test_runs_without_container_paths(self):
        # Force re-registration: other tests may have snapshot-cleared REGISTRY
        # while leaving scripts.v2.tools modules already imported, so a plain
        # `import` here wouldn't re-run the decorators.
        from scripts.v2.tool_registry import REGISTRY, dispatch
        if "corpus_overview" not in REGISTRY:
            for mod in list(sys.modules):
                if mod.startswith("scripts.v2.tools"):
                    del sys.modules[mod]
            import scripts.v2.tools  # noqa: F401

        self.assertIn("corpus_overview", REGISTRY)
        r = dispatch("corpus_overview", {})
        self.assertTrue(r.ok, msg=f"unexpected fail: {r.error}")
        self.assertEqual(r.tool, "corpus_overview")
        self.assertIsInstance(r.data, dict)
        # At least one warning when /workspace doesn't exist on the dev box.
        if not Path("/workspace/raw_text").exists():
            codes = [w.code for w in r.warnings]
            self.assertIn("raw_dir_missing", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
