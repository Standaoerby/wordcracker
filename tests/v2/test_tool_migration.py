"""Verify v2-migrated tools register correctly and v1 adapters wire up.

We don't try to hit /workspace metadata here — those tests are integration
and gated on RUN_INTEGRATION=1. Instead we monkeypatch the v1 module so the
v2 wrapper can be exercised on the dev box."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _fake_v1_module() -> types.ModuleType:
    """Create a fake scripts.rag_tools with stubs of the v1 functions our
    v2 tools delegate to. Inserted into sys.modules in setUp."""
    m = types.ModuleType("scripts.rag_tools")

    def find_book(title, author="", top=5, lang="en"):
        if title == "":
            return {"error": "title required"}
        if title == "nonexistent":
            return {"title_query": "nonexistent", "author_filter": author or None,
                    "total_matches": 0, "matches": []}
        if title == "ambiguous":
            return {"title_query": "ambiguous", "author_filter": None,
                    "total_matches": 47,
                    "matches": [{"id": f"PG{i}", "title": f"Ambig {i}",
                                 "author": "Foo, Bar", "downloads": i}
                                for i in range(top)]}
        return {"title_query": title, "author_filter": author or None,
                "total_matches": 1,
                "matches": [{"id": "PG1342", "title": "Pride and Prejudice",
                             "author": "Austen, Jane", "downloads": 50000}]}

    def author_metadata(author_regex):
        if author_regex == "^Nobody,":
            return {"error": "no books matched", "author_regex": author_regex}
        if author_regex == ".*":
            return {"error": "regex too broad; use '^Surname,' format"}
        return {
            "author_regex": author_regex,
            "books_matched": 123,
            "authors_matched": ["Doyle, Arthur Conan"],
            "year_of_birth_min": 1859,
            "year_of_death_max": 1930,
            "total_downloads": 999_999,
            "languages": ["en"],
            "sample_titles": ["A Study in Scarlet", "The Hound of the Baskervilles"],
        }

    def top_authors_by(metric="books", top=10, lang="en", include_generic=False):
        if metric not in ("books", "downloads", "tokens"):
            return {"error": f"unknown metric: {metric!r}"}
        return {"metric": metric, "top_n": top, "lang": lang,
                "top": [{"author": f"Author{i}", "books": top - i}
                        for i in range(top)]}

    def top_authors_by_country(country, metric="books", top=20):
        if country == "ZZ":
            return {"error": "no authors for country ZZ"}
        return {"country": country, "metric": metric, "top_n": top,
                "top": [{"author": f"{country} Author {i}", "books": top - i}
                        for i in range(min(top, 5))]}

    m.find_book = find_book
    m.author_metadata = author_metadata
    m.top_authors_by = top_authors_by
    m.top_authors_by_country = top_authors_by_country
    return m


class V2MigratedTools(unittest.TestCase):
    def setUp(self):
        # Inject stub v1 module before importing v2 tools.
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules["scripts.rag_tools"] = _fake_v1_module()
        # Reset registry — v2 tools register at import-time.
        from scripts.v2.tool_registry import REGISTRY
        self._snapshot = dict(REGISTRY)
        REGISTRY.clear()
        # Force re-import to re-register decorators on fresh registry.
        for mod in list(sys.modules):
            if mod.startswith("scripts.v2.tools"):
                del sys.modules[mod]
        import scripts.v2.tools  # noqa: F401

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear()
        REGISTRY.update(self._snapshot)
        sys.modules.pop("scripts.rag_tools", None)

    # --- registration ---

    def test_all_pilot_tools_registered(self):
        from scripts.v2.tool_registry import REGISTRY
        for name in ("corpus_overview", "find_book", "author_metadata",
                     "top_authors_by", "top_authors_by_country"):
            self.assertIn(name, REGISTRY, msg=f"{name} not registered")

    def test_tools_spec_emits_for_llm(self):
        from scripts.v2.tool_registry import build_tools_spec
        spec = build_tools_spec()
        names = {s["function"]["name"] for s in spec}
        self.assertGreaterEqual(len(names), 5)
        self.assertIn("find_book", names)

    # --- find_book contract ---

    def test_find_book_happy(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("find_book", {"title": "Pride"})
        self.assertTrue(r.ok)
        self.assertEqual(r.data["first_id"], "PG1342")
        self.assertEqual(r.data["matches"][0]["author"], "Austen, Jane")
        self.assertEqual(r.coverage.books_matched, 1)

    def test_find_book_not_found_returns_warning_not_error_dict(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("find_book", {"title": "nonexistent"})
        self.assertFalse(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("not_found", codes)
        # Planner needs this shape: matches list always present.
        self.assertEqual(r.data["matches"], [])

    def test_find_book_more_matches_warning(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("find_book", {"title": "ambiguous", "top": 5})
        self.assertTrue(r.ok)
        codes = [w.code for w in r.warnings]
        self.assertIn("more_matches", codes)
        self.assertEqual(r.coverage.books_matched, 47)

    def test_find_book_empty_title(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("find_book", {"title": ""})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")

    # --- author_metadata contract ---

    def test_author_metadata_happy(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("author_metadata", {"author_regex": "^Doyle,"})
        self.assertTrue(r.ok)
        self.assertEqual(r.data["authors_matched"][0], "Doyle, Arthur Conan")
        self.assertEqual(r.coverage.books_matched, 123)

    def test_author_metadata_no_books_marks_not_found(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("author_metadata", {"author_regex": "^Nobody,"})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "not_found")

    def test_author_metadata_broad_regex_invalid(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("author_metadata", {"author_regex": ".*"})
        self.assertFalse(r.ok)
        # current v1 message phrases it as "too broad" → bucketed as internal
        # which planner treats as retryable=False — that's fine.

    # --- top_authors_by contract ---

    def test_top_authors_books_default(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("top_authors_by", {})
        self.assertTrue(r.ok)
        self.assertEqual(r.data["metric"], "books")
        self.assertEqual(len(r.data["top"]), 10)

    def test_top_authors_bad_metric(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("top_authors_by", {"metric": "zzz"})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "invalid_args")

    # --- top_authors_by_country contract ---

    def test_top_authors_country_happy(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("top_authors_by_country", {"country": "GB"})
        self.assertTrue(r.ok)
        self.assertEqual(r.data["country"], "GB")

    def test_top_authors_country_empty(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("top_authors_by_country", {"country": "ZZ"})
        self.assertFalse(r.ok)
        self.assertEqual(r.error.type, "not_found")

    def test_top_authors_country_missing_arg(self):
        from scripts.v2.tool_registry import dispatch
        r = dispatch("top_authors_by_country", {})
        self.assertFalse(r.ok)
        self.assertIn(r.error.type, ("invalid_args",))


if __name__ == "__main__":
    unittest.main(verbosity=2)
