"""v4 — entity resolver tools.

resolve_author_name and resolve_book_title turn free-text author / title
phrasings into the canonical `author_regex` / `pg_id` shape downstream
tools expect. They're the v4 LLM planner's first-step go-to for any
query that mentions an entity by name.

Phase 1 (2026-05-22) — both tools now delegate to entity_resolver:
authors go through the v6 layered linker (mention detection +
multi-factor scoring + decision thresholds); books go through the v5
KNOWN_BOOKS → RU title aliases → v1 find_book pipeline. The legacy
off-flag fuzzy-only branch in this module was deleted.

The author path's `source` is `v6/...` and confidence depends on
v6 scoring (prominence index lookup). When the SPGC metadata file is
absent (CI), v6 falls back to a synthetic candidate with a moderate
confidence — these tests verify the canonical author_regex is still
correct, but do NOT assert specific confidence numbers because v6
scoring varies by environment.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import tools as _tools  # noqa: F401


class ResolveAuthorNameCurated(unittest.TestCase):

    def test_direct_alias_hit(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("Doyle")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["author_regex"], "^Doyle,")
        # Source is now from v6 layered linker (Phase 1) — exact label
        # depends on KB hit vs. alias fallback; just verify it's v6.
        self.assertTrue(r.data["source"].startswith("v6"),
                        f"expected v6/* source, got {r.data['source']}")
        self.assertGreater(r.data["confidence"], 0)

    def test_full_name_matches(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("Arthur Conan Doyle")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["author_regex"], "^Doyle,")

    def test_partial_name_via_last_word_fallback(self):
        """`Conan Doyle` — v6 detects «Doyle» as the alias with «Conan»
        as first-name context. Resolves to canonical Doyle."""
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("Conan Doyle")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["author_regex"], "^Doyle,")

    def test_russian_alias(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("Конан Дойл")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["author_regex"], "^Doyle,")

    def test_milton_v311_alias(self):
        """v3.1.1 added Milton aliases — ensure they're picked up."""
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("Milton")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["author_regex"], "^Milton, John")

    def test_unknown_author_returns_not_found(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        # Use a name not in aliases AND not in metadata (local — fuzzy
        # layer has no data without metadata.csv).
        r = resolve_author_name("xyz_definitely_not_an_author_qqq")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "not_found")

    def test_empty_query_returns_invalid_args(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_author_name
        r = resolve_author_name("")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


class ResolveBookTitleCurated(unittest.TestCase):

    def test_known_book_hit(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("Pride and Prejudice")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["pg_id"], "PG1342")
        self.assertEqual(r.data["title"], "Pride and Prejudice")
        # Phase 1 — tool now wraps source with the v5/v6 namespace.
        self.assertTrue(r.data["source"].endswith("known_books"),
                        f"expected */known_books, got {r.data['source']}")
        self.assertEqual(r.data["confidence"], 1.0)

    def test_beowulf_v311(self):
        """Beowulf was added to KNOWN_BOOKS in v3.1.1 — resolver picks
        it up automatically."""
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("Beowulf")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["pg_id"], "PG16328")

    def test_paradise_lost_v311(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("Paradise Lost")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["pg_id"], "PG26")

    def test_russian_title_resolves(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("Преступление и наказание")
        self.assertTrue(r.ok)
        self.assertEqual(r.data["pg_id"], "PG2554")

    def test_copyright_sentinel_warns(self):
        """Books with empty PG sentinel (HP, LOTR, 1984) have
        confidence 0.8 and a copyright warning."""
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("Harry Potter")
        self.assertTrue(r.ok)
        self.assertIsNone(r.data["pg_id"])  # empty PG sentinel → None
        self.assertEqual(r.data["title"], "Harry Potter")
        warning_codes = [w.code for w in r.warnings]
        self.assertIn("copyright", warning_codes)

    def test_empty_query_returns_invalid_args(self):
        from scripts.v2.tools.meta.resolve_entity import resolve_book_title
        r = resolve_book_title("")
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")


class ToolRegistryWiring(unittest.TestCase):
    """Resolvers must be registered in REGISTRY so the LLM planner
    catalog includes them and dispatch can find them."""

    def test_both_in_registry(self):
        from scripts.v2.tool_registry import REGISTRY
        self.assertIn("resolve_author_name", REGISTRY)
        self.assertIn("resolve_book_title", REGISTRY)

    def test_resolvers_show_up_in_catalog(self):
        from scripts.v2.planner import tool_catalog as tc
        names = {e.name for e in tc.build_catalog()}
        self.assertIn("resolve_author_name", names)
        self.assertIn("resolve_book_title", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
