"""Tool Router tests — verifies the deterministic execute() loop.

Uses monkeypatched v1 + v2 tools so we don't touch the real corpus. The router
is pure orchestration: dispatch → thread result → continue."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


"""Phase 2 (REFACTOR_BRIEF R4): mocks are GENERATED from declared schemas,
not hand-written. A wrapper that reads a key not in the schema fails at
import time (`@v1_contract` AST gate). A v1 function that drops a key
fails the contract sweep. Either way, mocks cannot drift away from the
real v1 shape — closes E8/E14/E15/E33/E34/B-R14-7 root cause.

Schemas live in scripts.v2.contracts.schemas; the factory below threads
a `mock_from_schema(...)` call per tool with a row override that yields
deterministic test data."""

from scripts.v2.contracts import mock_from_schema
from scripts.v2.contracts.schemas import (
    V1AffinityByAuthor, V1AuthorAttribution, V1AuthorInfluences,
    V1AuthorMetadata, V1AuthorProfile, V1BookEmotionProfile,
    V1BookReadability, V1CompareAuthors, V1CorpusStatsByAuthor,
    V1EmotionCollocates, V1FindBook, V1FindWordsByEtymology,
    V1LexicalDiversity, V1SemanticSearch,
    V1TopAuthorsBy, V1TopAuthorsByCountry,
    V1TopBooksByDownloads, V1TopBooksByRecency, V1TopNgramsByAuthor,
    V1WordCollocates, V1WordContexts, V1WordContextsGlobal,
    V1WordEtymology, V1WordFreqTimeline, V1WordPosDistribution,
    V1WordsDisappearingAfter,
    V1AffinityByBook, V1BookArchaicWords, V1EnrichWord,
    V1ExportWordList, V1LearningWords,
)


def _stub_find_book(**kw):
    # V1FindBook schema-driven mock — matches row_keys exactly.
    return mock_from_schema(
        V1FindBook,
        title_query=kw.get("title", ""),
        matches=[{"id": "PG1342", "title": "Pride and Prejudice",
                  "author": "Austen, Jane", "downloads": 100}],
        total_matches=1,
    )


def _stub_affinity_book(**kw):
    return mock_from_schema(
        V1AffinityByBook,
        pg_id=kw.get("pg_id", "?"),
        top=[{"word": "civility", "book_count": 1,
              "corpus_count": 10, "affinity": 5.0}],
    )


def _fake_v1_query():
    """Stub of scripts.rag_query — provides TOOL_DISPATCH for legacy_dispatch."""
    m = types.ModuleType("scripts.rag_query")
    m.TOOL_DISPATCH = {
        "affinity_by_author": lambda **kw: mock_from_schema(
            V1AffinityByAuthor,
            author_regex=kw["author_regex"],
            top=[{"word": "wicket", "author_count": 5,
                  "corpus_count": 100, "affinity": 22.1}],
        ),
        "compare_authors": lambda **kw: mock_from_schema(
            V1CompareAuthors,
            author1={"regex": kw["author1_regex"], "slug": "a",
                     "top_unique": [{"word": "blighter"}]},
            author2={"regex": kw["author2_regex"], "slug": "b",
                     "top_unique": [{"word": "fish"}]},
        ),
        "affinity_by_book": _stub_affinity_book,
        "book_archaic_words": lambda **kw: mock_from_schema(
            V1BookArchaicWords,
            id=kw["pg_id"],
            top=[{"word": "ye", "book_count": 12, "source": "seed",
                  "note": ""}],
        ),
        "find_book": _stub_find_book,
        "boom": lambda **_: (_ for _ in ()).throw(RuntimeError("kaboom")),
    }
    return m


def _fake_v1_tools():
    """Stub of scripts.rag_tools — every stub generated from V1Schema."""
    m = types.ModuleType("scripts.rag_tools")
    m.find_book = _stub_find_book
    m.author_metadata = lambda author_regex: mock_from_schema(
        V1AuthorMetadata, author_regex=author_regex, books_matched=1,
    )
    m.top_authors_by = lambda **kw: mock_from_schema(
        V1TopAuthorsBy, metric=kw.get("metric", "books"), top=[],
    )
    m.top_authors_by_country = lambda **kw: mock_from_schema(
        V1TopAuthorsByCountry, country=kw["country"], top=[],
    )
    m.affinity_by_author = lambda **kw: mock_from_schema(
        V1AffinityByAuthor,
        author_regex=kw["author_regex"],
        top=[{"word": "wicket", "author_count": 5,
              "corpus_count": 100, "affinity": 22.1}],
    )
    m.compare_authors = lambda **kw: mock_from_schema(
        V1CompareAuthors,
        author1={"regex": kw["author1_regex"], "slug": "a",
                 "top_unique": [{"word": "blighter"}]},
        author2={"regex": kw["author2_regex"], "slug": "b",
                 "top_unique": [{"word": "fish"}]},
    )
    m.word_contexts = lambda **kw: mock_from_schema(
        V1WordContexts, word=kw.get("word", "x"),
        samples=[{"pg_id": "PG1", "title": "t", "context": "hello"}],
    )
    m.word_contexts_global = lambda **kw: mock_from_schema(
        V1WordContextsGlobal, word=kw.get("word", "x"),
        samples=[{"author": "A", "title": "t", "pg_id": "PG1",
                  "distance": 0.1, "snippet": "hi"}],
    )
    m.word_collocates = lambda **kw: mock_from_schema(
        V1WordCollocates, word=kw.get("word", "x"),
        top_collocates=[{"word": "x", "count": 1}],
    )
    m.word_freq_timeline = lambda **kw: mock_from_schema(
        V1WordFreqTimeline, word=kw.get("word", "x"),
        timeline=[{"period": "1900-1925", "books": 5,
                   "total_tokens": 100, "occurrences": 1,
                   "per_million": 10.0}],
    )
    m.words_disappearing_after = lambda **kw: mock_from_schema(
        V1WordsDisappearingAfter,
        year_cutoff=kw.get("year", 1920),
        top=[{"word": "ye", "pre_per_million": 100.0,
              "post_per_million": 1.0, "drop_ratio": 100.0,
              "pre_count": 100, "post_count": 1}],
        pre_bucket={"books": 100, "total_tokens": 1000},
        post_bucket={"books": 100, "total_tokens": 1000},
    )
    m.word_pos_distribution = lambda **kw: mock_from_schema(
        V1WordPosDistribution, word=kw.get("word", "x"),
        pos_distribution=[
            {"pos": "NOUN", "count": 1, "share": 0.5, "samples": []},
            {"pos": "VERB", "count": 1, "share": 0.5, "samples": []},
        ],
    )
    m.word_etymology = lambda **kw: mock_from_schema(
        V1WordEtymology, word=kw["word"],
        family_chain=["latin"], primary_family="latin",
    )
    m.find_words_by_etymology = lambda **kw: mock_from_schema(
        V1FindWordsByEtymology, family=kw.get("family", "latin"),
        matched=[{"word": "quid", "affinity": 1.0, "occurrences": 5,
                  "corpus_count": 100, "family_chain": ["latin"],
                  "raw_codes": ["la"]}],
    )
    m.emotion_collocates = lambda **kw: mock_from_schema(
        V1EmotionCollocates, emotion=kw.get("emotion", "fear"),
        top_collocates=[{"word": "darkness", "count": 5}],
    )
    m.book_readability = lambda **kw: mock_from_schema(
        V1BookReadability, pg_id=kw["pg_id"],
        flesch_reading_ease=60.0,
        flesch_kincaid_grade=8.0,
        cefr_heuristic="B2",
    )
    m.author_profile = lambda **kw: mock_from_schema(
        V1AuthorProfile, author_regex=kw["author_regex"],
        metadata={"books_matched": 5, "year_of_birth_min": 1800},
        signature={"top": []},
        diversity={"ttr_aggregate": 0.5},
        influences={"top": []},
    )
    m.author_influences = lambda **kw: mock_from_schema(
        V1AuthorInfluences, pivot_author="X",
        top=[{"author": "Y", "delta": 0.5, "books_in_training": 5}],
    )
    m.author_attribution = lambda **kw: mock_from_schema(
        V1AuthorAttribution,
        top=[{"author": "X", "delta": 0.5, "books_in_training": 10}],
    )
    # Top-level imports in wrappers expect EVERY rag_tools function to
    # exist on the stub. Cover the rest with minimal schema-driven stubs.
    m.corpus_stats_by_author = lambda **kw: mock_from_schema(
        V1CorpusStatsByAuthor,
        author_regex=kw.get("author_regex", "^X,"),
        books_matched=1,
    )
    m.top_ngrams_by_author = lambda **kw: mock_from_schema(
        V1TopNgramsByAuthor,
        author_regex=kw.get("author_regex", "^X,"), top=[],
    )
    m.lexical_diversity = lambda **kw: mock_from_schema(
        V1LexicalDiversity, scope=str(kw.get("scope", "all_corpus")),
        ttr=0.5,
    )
    m.book_emotion_profile = lambda **kw: mock_from_schema(
        V1BookEmotionProfile,
        id=kw.get("pg_id", "PG0"),
        share_among_primary_emotions={"fear": 0.5, "joy": 0.5},
    )
    m.top_books_by_downloads = lambda **kw: mock_from_schema(
        V1TopBooksByDownloads, top=[],
    )
    m.top_books_by_recency = lambda **kw: mock_from_schema(
        V1TopBooksByRecency, top=[],
    )
    m.semantic_search = lambda **kw: mock_from_schema(
        V1SemanticSearch, query=kw.get("query", ""), results=[],
    )
    # Helpers the v2 wrappers occasionally pull (find_book_by_topic
    # imports `_maybe_translate`; word_collocates pulls `_counts_path`
    # / `_select_books`).
    m._maybe_translate = lambda s: s
    m._counts_path = lambda pg: Path("/no/such/path")
    m._select_books = lambda *a, **kw: []
    return m


def _fake_v1_learning():
    """Stub of scripts.learning_tools — every stub generated from V1Schema."""
    m = types.ModuleType("scripts.learning_tools")
    m.affinity_by_book = _stub_affinity_book
    m.learning_words = lambda **kw: mock_from_schema(
        V1LearningWords,
        results=[{"word": "civility", "scope_count": 5,
                  "corpus_count": 50, "affinity": 1.0,
                  "score": 1.0, "lemma": "civility", "pos": "NOUN"}],
    )
    m.book_archaic_words = lambda **kw: mock_from_schema(
        V1BookArchaicWords, id=kw["pg_id"],
        top=[{"word": "ye", "book_count": 12, "source": "seed",
              "note": ""}],
    )
    m.enrich_word = lambda **kw: mock_from_schema(
        V1EnrichWord, word=kw.get("word", "x"),
    )
    m.export_word_list = lambda **kw: mock_from_schema(
        V1ExportWordList, format=kw.get("format", "anki_csv"),
    )
    m.LEARNING_TOOLS_SPEC = []
    m.LEARNING_TOOL_DISPATCH = {}
    return m


class RouterClarifyAndOutOfScope(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules.pop("scripts.rag_tools", None)
        sys.modules["scripts.rag_query"] = _fake_v1_query()
        _stub_rag_tools = _fake_v1_tools()
        _stub_learning = _fake_v1_learning()
        sys.modules["scripts.rag_tools"] = _stub_rag_tools
        sys.modules["scripts.learning_tools"] = _stub_learning
        # Phase 2 — wrappers do top-level `from rag_tools / learning_tools
        # import …`. Stub both name variants (with and without `scripts.`
        # prefix) so the wrapper re-imports pick up the test stubs.
        sys.modules["rag_tools"] = _stub_rag_tools
        sys.modules["learning_tools"] = _stub_learning
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
        _stub_rag_tools = _fake_v1_tools()
        _stub_learning = _fake_v1_learning()
        sys.modules["scripts.rag_tools"] = _stub_rag_tools
        sys.modules["scripts.learning_tools"] = _stub_learning
        # Phase 2 — wrappers do top-level `from rag_tools / learning_tools
        # import …`. Stub both name variants (with and without `scripts.`
        # prefix) so the wrapper re-imports pick up the test stubs.
        sys.modules["rag_tools"] = _stub_rag_tools
        sys.modules["learning_tools"] = _stub_learning
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
        # v3.3.1 — isolate disk cache. Previously test_pipeline_e2e wrote
        # stale `_stub_tool` payloads under the shared `/data/v2_cache`
        # which then leaked into these router tests (cache_get returns
        # the stub before the patched wrapper ever runs). Each test gets
        # its own tmp cache dir; CACHE_ROOT is restored in tearDown.
        import tempfile
        from pathlib import Path
        from scripts.v2 import cache as _cache
        self._cache_root_saved = _cache.CACHE_ROOT
        self._cache_tmp = tempfile.TemporaryDirectory()
        _cache.CACHE_ROOT = Path(self._cache_tmp.name)
        _cache.cache_clear()

    def tearDown(self):
        from scripts.v2.tool_registry import REGISTRY
        REGISTRY.clear(); REGISTRY.update(self._snap)
        sys.modules.pop("scripts.rag_query", None)
        sys.modules.pop("scripts.rag_tools", None)
        from scripts.v2 import cache as _cache
        _cache.CACHE_ROOT = self._cache_root_saved
        _cache.cache_clear()
        self._cache_tmp.cleanup()

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

    def test_chained_steps_with_author_regex_injection(self):
        """Sprint 11.4 — composite_compare plan threads top_authors_by_country's
        top[0].author into affinity_by_author.author_regex. Verify the
        reshape "Surname, First" → "^Surname,".

        Phase 2 — wrappers do late imports of v1 functions, so overriding
        `sys.modules['scripts.rag_tools'].X = ...` reaches the call site.
        Mocks still go through `mock_from_schema` per R4.
        """
        sys.modules["scripts.rag_tools"].top_authors_by_country = lambda **kw: mock_from_schema(
            V1TopAuthorsByCountry,
            country=kw["country"],
            top=[{"author": "Dickens, Charles", "books": 50,
                  "downloads": 1000, "country_code": kw["country"]}],
        )
        sys.modules["scripts.rag_tools"].affinity_by_author = lambda **kw: mock_from_schema(
            V1AffinityByAuthor,
            author_regex=kw.get("author_regex"),
            top=[{"word": "wegg", "author_count": 1, "corpus_count": 10,
                  "affinity": 0.9}],
        )

        from scripts.v2.planner.entities import Entities
        from scripts.v2.planner.plan import PlanStep, QueryPlan
        from scripts.v2.planner.router import execute
        plan = QueryPlan(
            intent="composite_compare", entities=Entities(),
            steps=[
                PlanStep(tool="top_authors_by_country",
                         args={"country": "GB", "metric": "tokens", "top": 10}),
                PlanStep(tool="affinity_by_author",
                         args={"top": 30, "min_corpus_count": 500},
                         depends_on=[0], inject_result_as="author_regex",
                         optional=True),
            ],
        )
        r = execute(plan)
        self.assertEqual(r.kind, "results")
        self.assertEqual(len(r.results), 2)
        self.assertTrue(r.results[0].ok)
        self.assertTrue(r.results[1].ok)
        self.assertEqual(r.results[1].data["author_regex"], "^Dickens,")

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
        _stub_rag_tools = _fake_v1_tools()
        _stub_learning = _fake_v1_learning()
        sys.modules["scripts.rag_tools"] = _stub_rag_tools
        sys.modules["scripts.learning_tools"] = _stub_learning
        # Phase 2 — wrappers do top-level `from rag_tools / learning_tools
        # import …`. Stub both name variants (with and without `scripts.`
        # prefix) so the wrapper re-imports pick up the test stubs.
        sys.modules["rag_tools"] = _stub_rag_tools
        sys.modules["learning_tools"] = _stub_learning
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
