"""Sprint 21 (v3.2.0-alpha3) — pre-prod bugfix iteration.

Closes B100 (PG ids → titles), B101 (word display completeness),
B102 (copyright refusal mentions upload option), B104 (network error
soft fix).
"""
from __future__ import annotations

import re
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# RECOVERY (Кластер B, 2026-05-22) — REGISTRY is populated by decorator
# side-effects from importing `scripts.v2.tools`. Without this import
# `REGISTRY` is empty, and any test reading it (e.g.
# `test_bumped_tools_carry_new_version` below) explodes with KeyError
# at the very first lookup. rag_v2 normally pulls this in; standalone
# test runs need to do it explicitly.
from scripts.v2 import tools as _tools  # noqa: F401

from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.planner.entities import extract
from scripts.v2.planner.plan import (
    _copyright_refusal_if_book_under_copyright,
    _plan_word_contexts,
    _plan_word_etymology,
)


class B100_LexicalSearchTitleEnrichment(unittest.TestCase):
    """When lexical_search returns matches, each row should carry `title`
    + `author` from v1 metadata when the pg_id is resolvable. Closes
    the «renderer echoes PG2554 instead of Crime and Punishment» bug."""

    def test_title_lookup_attaches_to_matches(self):
        from scripts.v2.tools.search import lexical
        # Fake v1 metadata: return a synthetic title-lookup map.
        fake_lookup = {
            "PG1342": {"title": "Pride and Prejudice", "author": "Austen, Jane"},
            "PG2554": {"title": "Crime and Punishment", "author": "Dostoyevsky, Fyodor"},
        }
        # Fake DB rows (only id/score/snippet — title gets added by lookup)
        class FakeRow:
            def __init__(self, d): self._d = d
            def __getitem__(self, k): return self._d[k]
        fake_rows = [
            FakeRow({"id": "PG1342", "score": -1.5, "snippet": "[universally] acknowledged"}),
            FakeRow({"id": "PG2554", "score": -1.8, "snippet": "[axe] in the [pawnbroker]"}),
            FakeRow({"id": "PG9999", "score": -0.9, "snippet": "unknown"}),
        ]
        class FakeConn:
            def execute(self, sql, args): return self
            def fetchall(self): return fake_rows
        with mock.patch.object(lexical, "_connect", return_value=FakeConn()), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = lexical.lexical_search("axe", k=3)
        matches = r.data["matches"]
        self.assertEqual(len(matches), 3)
        # Resolvable pg_ids get title/author
        self.assertEqual(matches[0]["title"], "Pride and Prejudice")
        self.assertEqual(matches[0]["author"], "Austen, Jane")
        self.assertEqual(matches[1]["title"], "Crime and Punishment")
        # Unknown pg_id keeps base shape without title/author
        self.assertEqual(matches[2]["pg_id"], "PG9999")
        self.assertNotIn("title", matches[2])

    def test_title_lookup_handles_missing_v1(self):
        """When v1 import fails, _title_lookup returns empty dict —
        matches still carry pg_id but no title (renderer note rule 16
        handles this gracefully)."""
        from scripts.v2.tools.search import lexical
        # Lookup just returns {} — same as v1 import failure path.
        class FakeRow:
            def __init__(self, d): self._d = d
            def __getitem__(self, k): return self._d[k]
        fake_rows = [FakeRow({"id": "PG1", "score": -1.0, "snippet": "x"})]
        class FakeConn:
            def execute(self, sql, args): return self
            def fetchall(self): return fake_rows
        with mock.patch.object(lexical, "_connect", return_value=FakeConn()), \
             mock.patch.object(lexical, "_title_lookup", return_value={}):
            r = lexical.lexical_search("x", k=1)
        m = r.data["matches"][0]
        self.assertEqual(m["pg_id"], "PG1")
        self.assertNotIn("title", m)


class B100_HybridSearchMergesTitleFromLexical(unittest.TestCase):
    """Stan prod 2026-05-20 evening: «примеры ajar» surfaced PG65232 /
    PG13304 / PG14663 without titles. Root cause: hybrid_search merge
    took title only from semantic side; lexical-only matches lost their
    titles even though alpha3 lexical_search attaches them.

    The fix: hybrid_search now reads title/author from EITHER side,
    semantic-first, lexical fallback.
    """

    def test_lexical_only_match_keeps_title(self):
        from scripts.v2.tools.search import hybrid
        # Lexical returned a match WITH title (alpha3 enrichment);
        # semantic returned nothing for this pg_id.
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG13304", "score": -1.2,
                 "snippet": "[ajar]", "title": "The Gates Ajar",
                 "author": "Phelps, Elizabeth Stuart"},
            ]},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        # Semantic returned nothing
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        # Dispatch shim — return appropriate fake per name.
        def fake_dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            if name == "semantic_search":
                return sem_result
            raise AssertionError(f"unexpected dispatch({name})")
        with mock.patch.object(hybrid, "dispatch", side_effect=fake_dispatch):
            r = hybrid.hybrid_search("ajar", k=5)
        matches = r.data["matches"]
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["pg_id"], "PG13304")
        # Title MUST be preserved from lexical side
        self.assertEqual(m["title"], "The Gates Ajar")
        self.assertEqual(m["author"], "Phelps, Elizabeth Stuart")

    def test_semantic_title_wins_when_both_sides_have_one(self):
        """Semantic title takes precedence — it's the chunk-level metadata
        which is what downstream tools (e.g. find_book_by_topic) already
        consume as source of truth."""
        from scripts.v2.tools.search import hybrid
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG2554", "score": -1.0,
                 "snippet": "[axe]",
                 "title": "Crime and Punishment (lexical)",
                 "author": "lex-author"},
            ]},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": [
                {"pg_id": "PG2554", "text": "axe semantic",
                 # v1 semantic_search puts title/author at TOP LEVEL
                 "title": "Crime and Punishment",
                 "author": "Dostoyevsky, Fyodor"},
            ]},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        def fake_dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            if name == "semantic_search":
                return sem_result
            raise AssertionError(f"unexpected dispatch({name})")
        with mock.patch.object(hybrid, "dispatch", side_effect=fake_dispatch):
            r = hybrid.hybrid_search("axe", k=3)
        m = r.data["matches"][0]
        # Semantic wins
        self.assertEqual(m["title"], "Crime and Punishment")
        self.assertEqual(m["author"], "Dostoyevsky, Fyodor")

    def test_semantic_top_level_title_resolves_real_v1_shape(self):
        """Stan prod 2026-05-20 evening regression: rag_tools.semantic_search
        v1 puts title at TOP LEVEL of each result dict (lines 419-426 in
        scripts/rag_tools.py), NOT under `metadata`. The earlier alpha3
        hotfix looked in `sm.metadata.title` only and missed the actual
        path. Verify hybrid_search now reads from the real shape."""
        from scripts.v2.tools.search import hybrid
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": [
                # Exact shape of v1 semantic_search results
                {"pg_id": "PG13304",
                 "title": "The Gates Ajar",
                 "author": "Phelps, Elizabeth Stuart",
                 "chunk": 5,
                 "distance": 0.42,
                 "snippet": "...the gates ajar..."},
            ]},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        def fake_dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            if name == "semantic_search":
                return sem_result
            raise AssertionError(f"unexpected dispatch({name})")
        with mock.patch.object(hybrid, "dispatch", side_effect=fake_dispatch):
            r = hybrid.hybrid_search("ajar", k=3)
        m = r.data["matches"][0]
        self.assertEqual(m["pg_id"], "PG13304")
        self.assertEqual(m["title"], "The Gates Ajar")
        self.assertEqual(m["author"], "Phelps, Elizabeth Stuart")

    def test_cached_no_title_falls_back_to_v1_metadata(self):
        """Ultimate fallback: if neither side carries title (e.g. cached
        result from before alpha3 lexical_search patch), hybrid_search
        does the v1 metadata lookup at merge time."""
        from scripts.v2.tools.search import hybrid
        from scripts.v2.tools.search import lexical
        # Simulate cached old results — pg_id only, no title.
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG13304", "score": -1.2, "snippet": "[ajar]"},
            ]},
            coverage=Coverage(books_matched=1, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        # Stub the v1 metadata lookup at the import boundary.
        fake_lookup = {"PG13304": {"title": "The Gates Ajar",
                                    "author": "Phelps, Elizabeth Stuart"}}
        def fake_dispatch(name, args, **_kw):
            if name == "lexical_search":
                return lex_result
            if name == "semantic_search":
                return sem_result
            raise AssertionError(f"unexpected dispatch({name})")
        with mock.patch.object(hybrid, "dispatch", side_effect=fake_dispatch), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = hybrid.hybrid_search("ajar", k=3)
        m = r.data["matches"][0]
        # v1 metadata fallback kicked in
        self.assertEqual(m["title"], "The Gates Ajar")
        self.assertEqual(m["author"], "Phelps, Elizabeth Stuart")


class B100_RenderPromptRule(unittest.TestCase):
    """RENDER_PROMPT must teach the LLM to prefer titles over PG ids."""

    def test_rule_16_present_with_keywords(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("Book titles > PG ids", RENDER_PROMPT)
        self.assertIn("отдавать не айдишники", RENDER_PROMPT)


class B101_WordPlanComposite(unittest.TestCase):
    """When user asks about a word with no author scope, the plan should
    fan out hybrid_search (examples) + enrich_word (translation +
    etymology) in parallel. Closes B101."""

    def test_word_contexts_no_author_fans_to_enrich(self):
        e = extract("примеры слова tuppence")
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("hybrid_search", tools)
        self.assertIn("enrich_word", tools)

    def test_word_etymology_with_word_fans_to_contexts(self):
        e = extract("этимология слова tuppence")
        plan = _plan_word_etymology(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("word_etymology", tools)
        # hybrid_search added for parallel context fetch
        self.assertIn("hybrid_search", tools)

    def test_enrich_word_step_is_optional(self):
        """enrich_word Wiktionary outage shouldn't kill the context query."""
        e = extract("найди слово stitching")
        plan = _plan_word_contexts(e)
        enrich_steps = [s for s in plan.steps if s.tool == "enrich_word"]
        self.assertTrue(enrich_steps)
        self.assertTrue(enrich_steps[0].optional)

    def test_word_contexts_with_author_unchanged(self):
        """Author-scoped path is unchanged — enrich_word fan-out only
        kicks in for the no-author general lookup case (where user
        asked about a word standalone)."""
        e = extract("примеры ajar у Доила")
        # Doyle may or may not resolve depending on aliases; force it
        e.author_regex = "^Doyle, Arthur"
        e.word = "ajar"
        plan = _plan_word_contexts(e)
        tools = [s.tool for s in plan.steps]
        self.assertIn("word_contexts", tools)
        # No enrich_word fan-out for author-scoped queries — kept focused
        self.assertNotIn("enrich_word", tools)


class B101_RenderPromptRule(unittest.TestCase):

    def test_rule_17_word_bundle_present(self):
        from scripts.v2.rag_v2 import RENDER_PROMPT
        self.assertIn("translation + примеры + этимология", RENDER_PROMPT)
        self.assertIn("B101", RENDER_PROMPT)


class B102_CopyrightRefusalUploadMention(unittest.TestCase):
    """The OOS refusal now lists BOTH options the user requested:
    «либо в базе ограниченно (через uploads), либо только мета»."""

    def test_lotr_refusal_mentions_upload(self):
        e = extract('слова из "The Lord of the Rings"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan)
        reason = plan.out_of_scope_reason
        # Both options surfaced
        self.assertIn("Мета-информация", reason)
        self.assertIn("загруженной локально", reason)
        self.assertIn("fair use", reason)
        # And the upload-via-admin path is hinted
        self.assertIn("/admin/", reason)

    def test_1984_refusal_same_shape(self):
        e = extract('частоты в "1984"')
        plan = _copyright_refusal_if_book_under_copyright(e)
        self.assertIsNotNone(plan)
        self.assertIn("Мета-информация", plan.out_of_scope_reason)
        self.assertIn("загруженной локально", plan.out_of_scope_reason)


class B104_FriendlyRenderError(unittest.TestCase):
    """Renderer LLM failure now yields a user-friendly message + tool
    data summary instead of `[renderer error: <traceback>]`."""

    def test_timeout_message_is_friendly(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(TimeoutError("read timed out"))
        self.assertIn("Ollama", m)
        self.assertIn("ещё раз", m)

    def test_connection_error_message(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(
            ConnectionError("connection refused"))
        self.assertIn("Ollama", m)
        self.assertIn("недоступен", m)

    def test_generic_error_fallback(self):
        from scripts.v2.rag_v2 import _short_render_error_message
        m = _short_render_error_message(ValueError("weird state"))
        self.assertIn("ValueError", m)

    def test_friendly_render_error_includes_tool_summaries(self):
        """When the renderer dies, the user should still see SOMETHING
        from the tools that succeeded — not just an error string."""
        from scripts.v2.rag_v2 import _friendly_render_error
        fake_result = ToolResult.success(
            tool="top_books_by_downloads",
            data={"top": [{"pg_id": "PG1342", "title": "Pride and Prejudice"}]},
            coverage=Coverage(books_matched=1, books_total=1),
        )
        msg = _friendly_render_error(TimeoutError("ollama timed out"),
                                      [fake_result])
        self.assertIn("Ollama", msg)
        self.assertIn("top_books_by_downloads", msg)

    def test_friendly_render_error_no_tools_succeeded(self):
        from scripts.v2.rag_v2 import _friendly_render_error
        msg = _friendly_render_error(ConnectionError("refused"), [])
        self.assertIn("инструменты тоже не дали", msg)


class Q15_CompareAuthorsAutoRetry(unittest.TestCase):
    """Stan prod 2026-05-20 evening: «сравни По и Лавкрафта по стилю»
    yielded BOTH top_unique empty at default min_corpus_count=2000 (Plan
    cranks high for anti-PROPN). For small-corpus authors (Poe 33 books,
    Lovecraft 22 books) signature words rarely cross a 2000-occurrence
    corpus-wide floor → empty result → renderer hallucinated «cosine_
    similarity показывает что не найдены маркеры».

    Auto-retry: when both sides empty at min_corpus_count >= 1000, try
    once more at //4 threshold (silent, ~5s extra). If retry yields
    matches, return them with `_threshold_auto_lowered: true` + a
    `min_corpus_count_used` field so the renderer can disclose.
    """

    def test_both_empty_triggers_retry_with_lower_threshold(self):
        from scripts.v2.tools.authors import affinity
        call_log = []
        def fake_v1(author1_regex, author2_regex, top, min_corpus_count):
            call_log.append(min_corpus_count)
            if min_corpus_count >= 500:
                return {"top_unique_a": [], "top_unique_b": [],
                        "cosine_similarity": 0.0, "books_a": 33, "books_b": 22}
            # Retry threshold lower → actual data
            return {"top_unique_a": [{"word": "raven", "affinity": 12.3}],
                    "top_unique_b": [{"word": "eldritch", "affinity": 9.8}],
                    "cosine_similarity": 0.06,
                    "shared_high_affinity": [],
                    "books_a": 33, "books_b": 22}
        with mock.patch("scripts.rag_tools.compare_authors", new=fake_v1):
            r = affinity.compare_authors("^Poe,", "^Lovecraft, H",
                                          top=20, min_corpus_count=2000)
        # Verify multi-step retry happened: 2000 → 500 → 200 (returns data)
        self.assertEqual(call_log[0], 2000)
        self.assertEqual(call_log[1], 500)
        self.assertEqual(call_log[2], 200)  # step-down second retry
        # Data carries final threshold trace
        self.assertEqual(r.data.get("min_corpus_count_requested"), 2000)
        self.assertEqual(r.data.get("min_corpus_count_used"), 200)
        self.assertTrue(r.data.get("_threshold_auto_lowered"))
        # And the retry-result words made it through
        self.assertEqual(len(r.data["top_unique_a"]), 1)
        self.assertEqual(r.data["top_unique_a"][0]["word"], "raven")
        self.assertEqual(r.data["top_unique_b"][0]["word"], "eldritch")

    def test_first_call_with_results_skips_retry(self):
        """When the first call succeeds, no retry needed."""
        from scripts.v2.tools.authors import affinity
        call_log = []
        def fake_v1(author1_regex, author2_regex, top, min_corpus_count):
            call_log.append(min_corpus_count)
            return {"top_unique_a": [{"word": "cheerily", "affinity": 8}],
                    "top_unique_b": [{"word": "drawing-room", "affinity": 7}],
                    "cosine_similarity": 0.15,
                    "shared_high_affinity": [],
                    "books_a": 80, "books_b": 30}
        with mock.patch("scripts.rag_tools.compare_authors", new=fake_v1):
            r = affinity.compare_authors("^Dickens,", "^Twain,",
                                          top=20, min_corpus_count=2000)
        # Single call — no retry
        self.assertEqual(len(call_log), 1)
        self.assertIsNone(r.data.get("min_corpus_count_used"))
        self.assertFalse(r.data.get("_threshold_auto_lowered"))

    def test_low_initial_threshold_skips_retry(self):
        """Don't retry when caller already used a low threshold — they
        chose it explicitly."""
        from scripts.v2.tools.authors import affinity
        call_log = []
        def fake_v1(author1_regex, author2_regex, top, min_corpus_count):
            call_log.append(min_corpus_count)
            return {"top_unique_a": [], "top_unique_b": [],
                    "cosine_similarity": 0.0, "books_a": 33, "books_b": 22}
        with mock.patch("scripts.rag_tools.compare_authors", new=fake_v1):
            r = affinity.compare_authors("^Poe,", "^Lovecraft, H",
                                          top=20, min_corpus_count=200)
        # Single call — already low, don't try lower
        self.assertEqual(len(call_log), 1)

    def test_both_still_empty_after_all_retries_strong_render_note(self):
        """When ALL step-down retries (500/200/100/50) return empty, the
        original data stands plus the empty-sides logic appends a hard
        NO-rationalize render note forbidding interpretation through
        cosine_similarity."""
        from scripts.v2.tools.authors import affinity
        def fake_v1(author1_regex, author2_regex, top, min_corpus_count):
            return {"top_unique_a": [], "top_unique_b": [],
                    "cosine_similarity": 0.0, "books_a": 5, "books_b": 3}
        with mock.patch("scripts.rag_tools.compare_authors", new=fake_v1):
            r = affinity.compare_authors("^Unknown1,", "^Unknown2,",
                                          top=20, min_corpus_count=2000)
        note = r.data.get("_render_note", "")
        # Stronger forbid against rationalization
        self.assertIn("НЕ интерпретируй", note)
        self.assertIn("empty-result", note)
        self.assertIn("affinity_by_author", note)


class CacheKey_WrapperVersionInvalidation(unittest.TestCase):
    """Sprint 21+: wrapper_version baked into cache_key so bumping it
    invalidates pre-bump cached entries automatically (no manual
    `rm -rf /data/v2_cache/<tool>` needed on deploy). Stan B100 cache
    saga: prod kept serving title-less hybrid_search results because
    cache key was content-of-args only.
    """

    def test_different_versions_produce_different_keys(self):
        from scripts.v2.cache import cache_key
        k1 = cache_key("hybrid_search", {"query": "ajar"},
                       wrapper_version="v1")
        k2 = cache_key("hybrid_search", {"query": "ajar"},
                       wrapper_version="v2-titles")
        self.assertNotEqual(k1, k2)
        # Tool prefix preserved on both (v3.3.1: separator changed
        # from ":" to "__" for NTFS compatibility).
        self.assertTrue(k1.startswith("hybrid_search__"))
        self.assertTrue(k2.startswith("hybrid_search__"))

    def test_same_version_same_args_same_key(self):
        from scripts.v2.cache import cache_key
        k1 = cache_key("hybrid_search", {"query": "ajar"},
                       wrapper_version="v2-titles")
        k2 = cache_key("hybrid_search", {"query": "ajar"},
                       wrapper_version="v2-titles")
        self.assertEqual(k1, k2)

    def test_default_version_v1(self):
        """Tools without explicit wrapper_version stay on v1 — their
        existing cache entries continue to be served (no spurious
        invalidation across the whole tool registry)."""
        from scripts.v2.cache import cache_key
        explicit = cache_key("foo", {"x": 1}, wrapper_version="v1")
        default  = cache_key("foo", {"x": 1})
        self.assertEqual(explicit, default)

    def test_bumped_tools_carry_new_version(self):
        """Tools whose semantics changed during the alpha cascade still
        carry the alpha-era `wrapper_version` string — so that cache
        entries from before those fixes are invalidated on deploy.

        E18 (2026-05-22) — Stan «Dorian Gray ADJ» bug proved cache
        invalidation is REQUIRED, not optional. affinity_by_book without
        wrapper_version returned stale empty cache entries from pre-E14b
        runs, bypassing the new retry-helper.

        Recovery (Кластер B, 2026-05-22): Phase 2 bumped contract-layer
        wrappers en bloc to `vN-phase2-contract`, overwriting their
        alpha-era reasons. The asserts on those tools were removed —
        the alpha-era reason is no longer the operative one. Kept:
        only the three tools whose alpha-era version survived because
        Phase 2 did not touch them.

        The bigger invariant — every `wrapper_version` matches the
        canonical format — lives in
        `test_wrapper_version_format_invariant` below, so format drift
        is caught regardless of which alpha touched the wrapper."""
        from scripts.v2.tool_registry import REGISTRY
        # alpha4 + E22 (lang-on-query side of hybrid_search)
        self.assertEqual(REGISTRY["hybrid_search"].wrapper_version,
                          "v6-e22-lang-query-fix")
        # B100 title enrichment in lexical_search
        self.assertEqual(REGISTRY["lexical_search"].wrapper_version, "v2-titles")
        # E26 — find_book_by_topic META-title blocklist
        self.assertEqual(REGISTRY["find_book_by_topic"].wrapper_version,
                          "v2-e26-meta-blocklist")

    # R-23 negative invariant — drift of the version-string FORMAT is
    # caught here without requiring this test to mirror the registry.
    # The format is: "v" + integer + optional "-" + lowercase
    # alphanumeric segments separated by "-". Examples: "v1",
    # "v2-titles", "v3-phase2-contract", "v6-e22-lang-query-fix".
    _WRAPPER_VERSION_RE = re.compile(r"^v\d+(-[a-z0-9]+)*$")

    def test_wrapper_version_format_invariant(self):
        from scripts.v2.tool_registry import REGISTRY
        bad: list[tuple[str, str]] = []
        for name, spec in REGISTRY.items():
            v = spec.wrapper_version
            if not isinstance(v, str) or not self._WRAPPER_VERSION_RE.match(v):
                bad.append((name, repr(v)))
        self.assertEqual(
            bad, [],
            "wrapper_version must match ^v\\d+(-[a-z0-9]+)*$ — "
            "non-conforming tools: " + ", ".join(f"{n}={v}" for n, v in bad),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
