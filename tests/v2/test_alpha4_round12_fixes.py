"""Sprint 22+ (v3.2.0-alpha4) — Round 12 post-deploy fixes.

Closes:
  - B4 lang_hint NOT actually filtering (Q5 «английская классика» → finnish/etc)
  - v4 LLM-planner clarify → v3 rules fallback (Q11/Q12 translate/export)
  - Per-author copyright OOS (Q17 Hemingway → 3-part refusal)
  - Wilde character names (goring/worthing/chasuble) via blacklist
  - CIA-class anonymous-authors → drop_null_authors

External Claude Round 12 report:
  vault/test_external_claude_2026-05-20_round12_post_alpha3.md
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.planner.entities import extract, Entities
from scripts.v2.planner import plan as plan_mod


class B4_LangHintActuallyFilters(unittest.TestCase):
    """Round 12 Q5: «имени Анна примеры в английской классике» surfaced
    Finnish/Hungarian/Italian books because lang_hint was extracted but
    never passed to hybrid_search. alpha4 fix:
      - hybrid_search now accepts `lang` param + post-filters via v1
        metadata.language lookup
      - _plan_word_contexts (no author scope) passes e.lang_hint to
        hybrid_search step
    """

    def test_word_contexts_plan_passes_lang(self):
        e = extract("имени Анна примеры в английской классике")
        plan = plan_mod._plan_word_contexts(e)
        # First step is hybrid_search
        hs_step = plan.steps[0]
        self.assertEqual(hs_step.tool, "hybrid_search")
        # lang propagated from lang_hint
        self.assertEqual(hs_step.args.get("lang"), "en")

    def test_word_contexts_plan_no_lang_hint_no_arg(self):
        e = extract("примеры использования слова ajar")
        plan = plan_mod._plan_word_contexts(e)
        hs_step = plan.steps[0]
        # No lang arg when no hint
        self.assertNotIn("lang", hs_step.args)

    def test_hybrid_search_post_filters_by_lang(self):
        """When lang='en' is passed, books with metadata.language='fi'
        get dropped at merge time."""
        from scripts.v2.tools.search import hybrid, lexical
        # Both lexical + semantic return matches across languages
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG_EN1", "score": -1.0, "snippet": "x",
                 "title": "English book", "author": "English Author"},
                {"pg_id": "PG_FI1", "score": -1.1, "snippet": "y",
                 "title": "Annan unelmavuodet", "author": "Finnish Author"},
                {"pg_id": "PG_HU1", "score": -1.2, "snippet": "z",
                 "title": "A rablólovag", "author": "Hungarian Author"},
            ]},
            coverage=Coverage(books_matched=3, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        # Stub the v1 metadata lookup to return language per pg_id
        fake_lookup = {
            "PG_EN1": {"language": "en", "title": "English book"},
            "PG_FI1": {"language": "fi", "title": "Annan unelmavuodet"},
            "PG_HU1": {"language": "hu", "title": "A rablólovag"},
        }
        def fake_v2_dispatch(name, args):
            if name == "lexical_search":
                return lex_result
        def fake_dispatch_any(name, args):
            if name == "semantic_search":
                return sem_result
        with mock.patch.object(hybrid, "v2_dispatch", side_effect=fake_v2_dispatch), \
             mock.patch.object(hybrid, "dispatch_any", side_effect=fake_dispatch_any), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = hybrid.hybrid_search("anna", k=10, lang="en")
        # Only EN survives
        pg_ids = {m["pg_id"] for m in r.data["matches"]}
        self.assertIn("PG_EN1", pg_ids)
        self.assertNotIn("PG_FI1", pg_ids)
        self.assertNotIn("PG_HU1", pg_ids)
        # Warning about drop count
        codes = {w.code for w in r.warnings}
        self.assertIn("lang_filtered", codes)

    def test_hybrid_search_no_lang_returns_all_books(self):
        """When lang is NOT set, all languages flow through."""
        from scripts.v2.tools.search import hybrid, lexical
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG_EN1", "score": -1.0, "snippet": "x"},
                {"pg_id": "PG_FI1", "score": -1.1, "snippet": "y"},
            ]},
            coverage=Coverage(books_matched=2, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        fake_lookup = {
            "PG_EN1": {"language": "en"},
            "PG_FI1": {"language": "fi"},
        }
        def fake_v2_dispatch(name, args):
            return lex_result
        def fake_dispatch_any(name, args):
            return sem_result
        with mock.patch.object(hybrid, "v2_dispatch", side_effect=fake_v2_dispatch), \
             mock.patch.object(hybrid, "dispatch_any", side_effect=fake_dispatch_any), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = hybrid.hybrid_search("anna", k=10)  # NO lang
        self.assertEqual(len(r.data["matches"]), 2)

    def test_book_without_language_metadata_kept(self):
        """Defensive: when a book has no language metadata, the filter
        doesn't drop it (better than over-aggressive)."""
        from scripts.v2.tools.search import hybrid, lexical
        lex_result = ToolResult.success(
            tool="lexical_search",
            data={"matches": [
                {"pg_id": "PG_EN1", "score": -1.0, "snippet": "x"},
                {"pg_id": "PG_NOMETA", "score": -1.1, "snippet": "y"},
            ]},
            coverage=Coverage(books_matched=2, books_total=-1),
        )
        sem_result = ToolResult.success(
            tool="semantic_search",
            data={"results": []},
            coverage=Coverage(books_matched=0, books_total=-1),
        )
        fake_lookup = {
            "PG_EN1": {"language": "en"},
            # PG_NOMETA missing — no metadata
        }
        def fake_v2_dispatch(name, args):
            return lex_result
        def fake_dispatch_any(name, args):
            return sem_result
        with mock.patch.object(hybrid, "v2_dispatch", side_effect=fake_v2_dispatch), \
             mock.patch.object(hybrid, "dispatch_any", side_effect=fake_dispatch_any), \
             mock.patch.object(lexical, "_title_lookup", return_value=fake_lookup):
            r = hybrid.hybrid_search("x", k=10, lang="en")
        # Both kept — PG_NOMETA isn't dropped because we can't prove it's non-EN
        self.assertEqual(len(r.data["matches"]), 2)


class Q17_PerAuthorCopyrightOOS(unittest.TestCase):
    """Round 12 Q17: «vocab passport Hemingway» — Hemingway in SPGC
    metadata (resolve_author_name finds him) but ZERO books in tokens
    (estate enforces copyright). Renderer wrote «not found»; critic
    caught the contradiction. Now: per-author copyright OOS just like
    book-level B102 — 3-part refusal explaining metadata vs upload.
    """

    def test_hemingway_routes_to_oos(self):
        e = Entities(author_regex="^Hemingway,", author_label="Hemingway, Ernest")
        plan = plan_mod._copyright_refusal_if_author_under_copyright(e)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent, "out_of_scope")
        self.assertIn("copyright", plan.out_of_scope_reason.lower())
        self.assertIn("Мета-информация", plan.out_of_scope_reason)
        self.assertIn("загруженных копий", plan.out_of_scope_reason)
        self.assertIn("Hemingway", plan.out_of_scope_reason)

    def test_steinbeck_also_locked(self):
        e = Entities(author_regex="^Steinbeck,",
                     author_label="Steinbeck, John")
        plan = plan_mod._copyright_refusal_if_author_under_copyright(e)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.intent, "out_of_scope")

    def test_orwell_locked(self):
        e = Entities(author_regex="^Orwell,", author_label="Orwell, George")
        plan = plan_mod._copyright_refusal_if_author_under_copyright(e)
        self.assertIsNotNone(plan)

    def test_dickens_passes_through(self):
        """Public-domain author: helper returns None → normal plan flow."""
        e = Entities(author_regex="^Dickens,", author_label="Dickens, Charles")
        plan = plan_mod._copyright_refusal_if_author_under_copyright(e)
        self.assertIsNone(plan)

    def test_no_author_returns_none(self):
        e = Entities()
        plan = plan_mod._copyright_refusal_if_author_under_copyright(e)
        self.assertIsNone(plan)

    def test_vocab_passport_short_circuits_for_hemingway(self):
        """The decorator integrates with the plan builder so user-facing
        flow gets the OOS, not a useless 'not found' from an empty tool."""
        e = Entities(author_regex="^Hemingway,",
                     author_label="Hemingway, Ernest")
        plan = plan_mod._plan_vocab_passport(e)
        self.assertEqual(plan.intent, "out_of_scope")
        self.assertIn("copyright", plan.out_of_scope_reason.lower())

    def test_author_metadata_short_circuits_for_locked_author(self):
        e = Entities(author_regex="^Salinger,", author_label="Salinger, J.D.")
        plan = plan_mod._plan_author_metadata(e)
        self.assertEqual(plan.intent, "out_of_scope")

    def test_dickens_vocab_passport_works(self):
        """Sanity: public-domain author still gets normal plan."""
        e = Entities(author_regex="^Dickens,", author_label="Dickens, Charles")
        plan = plan_mod._plan_vocab_passport(e)
        self.assertEqual(plan.intent, "vocab_passport")
        self.assertTrue(plan.steps)


class Q11_Q12_V4FollowupV3Fallback(unittest.TestCase):
    """Round 12 Q11/Q12: v4 LLM-planner returned clarify on translate/
    export followup → fell straight to clarify in 38.92s. The v3 rules
    path (translate_word_list / export_word_list with markdown prior-
    words extraction) would have worked.

    Fix: when v4 returns clarify on a followup, try v3 rules first. If
    v3 produces a non-clarify plan with steps, prefer it.

    These tests cover the helpers; the actual rag_v2.ask integration
    is exercised end-to-end manually after deploy.
    """

    def test_infer_translate_followup_returns_translate_word_list(self):
        from scripts.v2.planner.history import infer_followup_intent
        # Prior must classify as one of the word-list intents
        history = [
            {"role": "user", "content": "топ-10 фирменных слов Доила"},
            {"role": "assistant", "content":
             "| word | freq |\n|------|------|\n| burger | 47 |"},
        ]
        result = infer_followup_intent("переведи эти слова на русский",
                                        history)
        self.assertEqual(result, "translate_word_list")

    def test_infer_export_followup_returns_export_word_list(self):
        from scripts.v2.planner.history import infer_followup_intent
        history = [
            {"role": "user", "content": "топ-10 фирменных слов Доила"},
            {"role": "assistant", "content":
             "| word | freq |\n|------|------|\n| burger | 47 |"},
        ]
        result = infer_followup_intent("выгрузи эти слова в anki",
                                        history)
        self.assertEqual(result, "export_word_list")


class WildeCharacterNamesBlacklist(unittest.TestCase):
    """Round 12 Q10: surname blocklist let goring/worthing/chasuble
    through as Wilde signature words. Extend _LITERARY_PROPN_BLACKLIST."""

    def test_blacklist_contains_wilde_characters(self):
        from scripts.v2.tools.authors.affinity import _LITERARY_PROPN_BLACKLIST
        self.assertIn("goring", _LITERARY_PROPN_BLACKLIST)
        self.assertIn("worthing", _LITERARY_PROPN_BLACKLIST)
        self.assertIn("chasuble", _LITERARY_PROPN_BLACKLIST)
        # Existing ones still present
        self.assertIn("ernest", _LITERARY_PROPN_BLACKLIST)


class CIA_AnonymousAuthorsDropped(unittest.TestCase):
    """Round 12 Q1: CIA appeared as #3 in top_authors_by(metric=tokens).
    Institutional aggregate, not a literary author. Drop via filter."""

    def test_cia_in_null_author_tokens(self):
        from scripts.v2.tools._result_filters import _NULL_AUTHOR_TOKENS
        self.assertIn("cia", _NULL_AUTHOR_TOKENS)
        self.assertIn("central intelligence agency", _NULL_AUTHOR_TOKENS)

    def test_drop_null_authors_drops_cia(self):
        from scripts.v2.tools._result_filters import drop_null_authors
        rows = [
            {"author": "Dumas, Alexandre", "tokens": 16_827_912},
            {"author": "Various", "tokens": 12_000_000},
            {"author": "Central Intelligence Agency", "tokens": 11_000_000},
            {"author": "Doyle, Arthur Conan", "tokens": 9_000_000},
        ]
        filtered, dropped = drop_null_authors(rows)
        authors = [r["author"] for r in filtered]
        self.assertIn("Dumas, Alexandre", authors)
        self.assertIn("Doyle, Arthur Conan", authors)
        self.assertNotIn("Various", authors)
        self.assertNotIn("Central Intelligence Agency", authors)
        self.assertEqual(dropped, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
