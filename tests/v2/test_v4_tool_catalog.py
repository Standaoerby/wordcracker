"""v4 — tool catalog serializer.

The catalog is the LLM planner's view of available tools. It must:
  - Pull every @tool from the registry (single source of truth)
  - Stay compact enough to fit in the prompt (<25 KB)
  - Carry enough metadata for the LLM to pick correctly (name, desc,
    required args, cost class)
  - Be reproducible — same registry → same prompt
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Trigger tool registration before any catalog assertions.
from scripts.v2 import tools as _tools  # noqa: F401
from scripts.v2.planner import tool_catalog as tc
from scripts.v2.tool_registry import REGISTRY


class CatalogBuild(unittest.TestCase):

    def test_catalog_covers_registry(self):
        cat = tc.build_catalog()
        names_in_catalog = {e.name for e in cat}
        # Catalog enumerates every registered tool by default
        self.assertEqual(names_in_catalog, set(REGISTRY.keys()))

    def test_catalog_includes_v4_resolvers(self):
        cat = tc.build_catalog()
        names = {e.name for e in cat}
        self.assertIn("resolve_author_name", names)
        self.assertIn("resolve_book_title", names)

    def test_category_filter(self):
        cat = tc.build_catalog(category_filter=["authors"])
        self.assertTrue(cat)
        self.assertTrue(all(e.category == "authors" for e in cat))

    def test_exclude_filter(self):
        cat = tc.build_catalog(exclude=["resolve_book_title"])
        names = {e.name for e in cat}
        self.assertNotIn("resolve_book_title", names)
        self.assertIn("resolve_author_name", names)

    def test_required_args_extracted(self):
        cat = tc.build_catalog()
        by_name = {e.name: e for e in cat}
        # find_book schema declares title as required
        self.assertIn("title", by_name["find_book"].required)
        # resolve_author_name declares query
        self.assertIn("query", by_name["resolve_author_name"].required)

    def test_optional_args_separated(self):
        cat = tc.build_catalog()
        by_name = {e.name: e for e in cat}
        # find_book input_schema has author/top/lang as optional
        opt_set = set(by_name["find_book"].optional)
        self.assertTrue({"author", "top"} & opt_set)


class CatalogRender(unittest.TestCase):

    def test_render_contains_every_tool(self):
        text = tc.render_catalog(tc.build_catalog())
        for name in REGISTRY:
            self.assertIn(f"`{name}", text, name)

    def test_render_grouped_by_category(self):
        text = tc.render_catalog(tc.build_catalog())
        # Section headers for each category
        cats = sorted({s.category for s in REGISTRY.values()})
        for c in cats:
            self.assertIn(f"### {c}", text)

    def test_render_stays_under_size_cap(self):
        text = tc.render_catalog(tc.build_catalog())
        # Sanity bound — qwen3:14b has 32k ctx, leave room for query +
        # examples + output. Catalog target ≤25KB.
        self.assertLess(len(text), 25_000, len(text))


class FewShotExamples(unittest.TestCase):

    def test_examples_present(self):
        ex = tc.few_shot_examples()
        self.assertGreater(len(ex), 3)

    def test_examples_cover_key_patterns(self):
        ex = tc.few_shot_examples()
        queries = [e["query"].lower() for e in ex]
        # Must include compound multi-book etymology pattern
        self.assertTrue(any("beowulf" in q and "paradise lost" in q
                            for q in queries))
        # Must include triangulation pattern
        self.assertTrue(any("ближе к" in q or "closer to" in q
                            for q in queries))
        # Must include clarify-fallback example
        clarify_examples = [e for e in ex if e["plan"].get("clarify")]
        self.assertGreater(len(clarify_examples), 0)

    def test_example_plans_are_valid_planspec(self):
        from scripts.v2.planner import plan_spec as ps
        ex = tc.few_shot_examples()
        for e in ex:
            plan = ps.from_json(e["plan"])
            rep = ps.validate(plan)
            # Validation MAY warn (e.g. unknown_arg) but should not error
            self.assertTrue(
                rep.ok,
                msg=f"example for query {e['query']!r} validation failed: "
                    f"{[i.message for i in rep.errors()]}"
            )


class PlannerPromptAssembly(unittest.TestCase):

    def test_full_prompt_builds(self):
        prompt = tc.build_planner_prompt()
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 500)
        # Contains the critical-rules section
        self.assertIn("CRITICAL RULES", prompt)
        # Contains at least one tool
        self.assertIn("find_book", prompt)
        # Contains few-shot examples
        self.assertIn("Example", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
