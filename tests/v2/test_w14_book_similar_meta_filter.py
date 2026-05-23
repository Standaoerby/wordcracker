"""W-14 tests — book_similar / find_book_by_topic excludes meta-documents
from recommendation results.

Stan 2026-05-23 (Phase 4) test bench:
    «что почитать после Дракулы» → топ-результат «Project Gutenberg
    (1971-2009)» (мета-документ, rerank 1.000), каталог книготорговца
    1866, греческие мифы.

W-14 acceptance:
    1. Из выдачи рекомендаций исключать не-художественные мета-тексты
       (каталоги, библиографии, истории литературы, документы про
       Gutenberg).
    2. «что почитать после Дракулы» → художественные тематически
       близкие книги, без мета-документов в топе.

R5 compliance: positive (legit fiction passes), negative (each kind
of meta-doc dropped, listed explicitly).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.books.find_book_by_topic import _is_meta_title


# ---------------------------------------------------------------------------
# Blocklist precision — fiction passes, meta-docs flagged
# ---------------------------------------------------------------------------


class MetaTitleBlocklistFlagsKnownOffenders(unittest.TestCase):
    """Each title the user observed at the top of «что почитать после
    Дракулы» must be flagged. Adding new entries here is the bug-driven
    regression-test path (R5: query that produced the bug becomes test)."""

    def test_project_gutenberg_meta_doc_flagged(self):
        self.assertTrue(_is_meta_title("Project Gutenberg (1971-2009)"))

    def test_bookseller_catalogue_1866_flagged(self):
        self.assertTrue(_is_meta_title("Catalogue of London Books, 1866"))
        self.assertTrue(_is_meta_title("A Catalogue of Books Published 1850"))

    def test_greek_mythology_meta_doc_flagged(self):
        # Stan's «греческие мифы» surfaced for Dracula similar
        self.assertTrue(_is_meta_title("Greek Mythology Stories"))
        self.assertTrue(_is_meta_title("Old Greek Stories"))
        self.assertTrue(_is_meta_title("Myths of Greece and Rome"))

    def test_history_of_literature_flagged(self):
        # «History of English Literature», «History of the Novel» etc.
        self.assertTrue(_is_meta_title("History of English Literature"))
        self.assertTrue(_is_meta_title("History of the Novel"))
        self.assertTrue(_is_meta_title("History of Fiction"))

    def test_anthology_treasury_flagged(self):
        self.assertTrue(_is_meta_title("A Treasury of English Literature"))
        self.assertTrue(_is_meta_title("Anthology of English Poetry"))
        self.assertTrue(_is_meta_title("Selections from English Verse"))

    def test_studies_lectures_essays_flagged(self):
        self.assertTrue(_is_meta_title("Studies in English Literature"))
        self.assertTrue(_is_meta_title("Lectures on Shakespeare"))
        self.assertTrue(_is_meta_title("Essays on English Literature"))


class MetaTitleBlocklistDoesNotFlagFiction(unittest.TestCase):
    """Negative tests — legit fiction titles must NOT be flagged.
    Stan would surely complain if «Dracula» or «Tales from Shakespeare»
    started disappearing from recommendation lists."""

    def test_dracula_not_flagged(self):
        self.assertFalse(_is_meta_title("Dracula"))

    def test_frankenstein_not_flagged(self):
        self.assertFalse(_is_meta_title("Frankenstein"))

    def test_pride_and_prejudice_not_flagged(self):
        self.assertFalse(_is_meta_title("Pride and Prejudice"))

    def test_tales_from_shakespeare_not_flagged(self):
        # Lamb's children's-fiction adaptation — IS fiction, NOT meta
        self.assertFalse(_is_meta_title("Tales from Shakespeare"))

    def test_war_and_peace_not_flagged(self):
        self.assertFalse(_is_meta_title("War and Peace"))

    def test_oliver_twist_not_flagged(self):
        self.assertFalse(_is_meta_title("Oliver Twist"))

    def test_empty_title_not_flagged(self):
        # Defensive — empty/None title is fine, the dedup layer handles
        # the "no title" case separately.
        self.assertFalse(_is_meta_title(""))
        self.assertFalse(_is_meta_title(None))


# ---------------------------------------------------------------------------
# End-to-end wrapper — meta_blocklist drops surfaced via filter_drops
# ---------------------------------------------------------------------------


class FindBookByTopicAppliesMetaBlocklist(unittest.TestCase):

    def _fake_hybrid_search(self, matches):
        # Build a ToolResult mock that the wrapper accepts.
        from scripts.v2._types import Coverage, ToolResult
        return ToolResult.success(
            tool="hybrid_search",
            data={"matches": matches, "reranked_by": "bge_reranker"},
            coverage=Coverage(books_matched=len(matches), books_total=-1),
            query={"query": "test"},
        )

    def test_meta_docs_dropped_legit_fiction_kept(self):
        # find_book_by_topic imports `dispatch` AS `v2_dispatch` at
        # module load time, so we must patch the alias inside the
        # module's namespace, not the source.
        # Mock chunks where #1, #4, #5 are meta-docs; #2, #3 are fiction.
        fake_chunks = [
            {"pg_id": "PGMETA1", "title": "Project Gutenberg (1971-2009)",
             "author": "anonymous", "rrf_score": 1.0,
             "rerank_score": 1.0, "snippet": "ebook index"},
            {"pg_id": "PG84", "title": "Frankenstein",
             "author": "Shelley, Mary", "rrf_score": 0.8,
             "rerank_score": 0.85, "snippet": "the monster lived"},
            {"pg_id": "PG345", "title": "Dracula",
             "author": "Stoker, Bram", "rrf_score": 0.7,
             "rerank_score": 0.8, "snippet": "the count returned"},
            {"pg_id": "PGMETA2", "title": "Greek Mythology Stories",
             "author": "various", "rrf_score": 0.6,
             "rerank_score": 0.7, "snippet": "Zeus and Hera"},
            {"pg_id": "PGMETA3",
             "title": "Catalogue of London Books, 1866",
             "author": "bookseller", "rrf_score": 0.5,
             "rerank_score": 0.65, "snippet": "list of titles"},
        ]
        hybrid_result = self._fake_hybrid_search(fake_chunks)
        with patch("scripts.v2.tools.books.find_book_by_topic.v2_dispatch",
                   return_value=hybrid_result):
            from scripts.v2.tools.books.find_book_by_topic import (
                find_book_by_topic,
            )
            result = find_book_by_topic(topic="gothic horror like Dracula",
                                         top=10, translate=False)
        self.assertTrue(result.ok, msg=f"wrapper failed: {result.error}")
        books = (result.data or {}).get("matches") or []
        titles = [b.get("title") for b in books]
        # Meta-docs MUST be gone
        self.assertNotIn("Project Gutenberg (1971-2009)", titles)
        self.assertNotIn("Greek Mythology Stories", titles)
        self.assertNotIn("Catalogue of London Books, 1866", titles)
        # Fiction MUST survive
        self.assertIn("Frankenstein", titles)
        self.assertIn("Dracula", titles)
        # filter_drops surfaces the count of meta-blocked entries via
        # the dedup warning (3 meta drops in the mock above)
        codes = {w.code for w in result.warnings}
        self.assertIn("dedup", codes,
                      msg="meta_blocklist drops should surface via dedup warning")


class WrapperVersionBumpedForCacheInvalidation(unittest.TestCase):

    def test_wrapper_version_includes_w14_marker(self):
        # R-23 Tier 0 — bumping wrapper_version is the only thing that
        # invalidates already-cached recommendation results so the
        # extended blocklist actually takes effect.
        from scripts.v2.tools.books.find_book_by_topic import (
            find_book_by_topic,
        )
        # The decorator stamps tool metadata into __wrapped_meta__ via
        # the registry — pull it from REGISTRY entry.
        import scripts.v2.tools  # ensure tools imported
        from scripts.v2.tool_registry import REGISTRY
        entry = REGISTRY.get("find_book_by_topic")
        self.assertIsNotNone(entry)
        version = getattr(entry, "wrapper_version", None) or getattr(
            entry, "version", None) or ""
        # Either «w14» surfaces directly OR the version bumped past v2
        self.assertTrue(
            "w14" in version.lower() or version >= "v3",
            msg=f"wrapper_version must signal W-14; got {version!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
