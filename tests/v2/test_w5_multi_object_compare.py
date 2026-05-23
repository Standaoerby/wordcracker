"""W-5 tests — multi-object compare queries actually compare both objects.

Stan 2026-05-23 (Phase 4) test bench:
    «что сложнее — Дракула или Франкенштейн» → ответ про одну книгу.
    country_compare → односторонний список.
    «слова страха у По и Лавкрафта одновременно» → только По, 1 call.

W-5 acceptance:
    1. Запрос с N объектами → план с N (или N-парными) вызовами
       и composite-view, кладущий результаты рядом.
    2. Если composite реально не поддержан — честно сказать, не
       отвечать молча про один объект.
    3. Сравнение двух книг → обе в одной таблице.
    4. «По и Лавкрафта» → оба автора, ≥2 calls.

R5 compliance: каждый кейс — позитивный (multi-object plan
fans out) и негативный (single-object → single-step path
сохранён без регрессии).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import extract, KNOWN_BOOKS
from scripts.v2.planner.intent import classify
from scripts.v2.planner.plan import build
from scripts.v2.planner.builders.book import _plan_book_compare
from scripts.v2.planner.builders.composite import _plan_country_compare


# ---------------------------------------------------------------------------
# Book compare — N books → N affinity_by_book steps
# ---------------------------------------------------------------------------


class BookCompareMultiBookFanOut(unittest.TestCase):

    def test_two_books_in_english_fan_out_to_two_affinity_calls(self):
        e = extract("compare Dracula and Frankenstein")
        plan = _plan_book_compare(e)
        self.assertEqual(plan.intent, "book_compare")
        tools = [s.tool for s in plan.steps]
        # Must fan out to BOTH books, not just primary
        self.assertEqual(tools, ["affinity_by_book", "affinity_by_book"])
        pgs = {s.args["pg_id"] for s in plan.steps}
        self.assertEqual(pgs, {"PG84", "PG345"})

    def test_two_books_in_russian_fan_out_to_two_affinity_calls(self):
        # W-5 critical case from Stan's test bench
        e = extract("что сложнее — Дракула или Франкенштейн")
        # readability_compare is the natural intent here, but if user
        # asks compare in book_compare flavor («сравни Дракулу и
        # Франкенштейна»), book_compare must also fan out.
        e2 = extract("сравни Дракулу и Франкенштейна")
        plan = _plan_book_compare(e2)
        tools = [s.tool for s in plan.steps]
        # both PG84 + PG345 must be present in pg_id args
        pgs = {s.args.get("pg_id") for s in plan.steps if "pg_id" in s.args}
        self.assertIn("PG84", pgs)
        self.assertIn("PG345", pgs)
        # Sanity: ≥2 affinity_by_book steps
        self.assertGreaterEqual(
            sum(1 for t in tools if t == "affinity_by_book"), 2,
        )

    def test_single_book_keeps_single_step_plan(self):
        # Negative: single-book compare keeps the legacy single-step
        # plan (no regression).
        e = extract("сравни words в Dracula с другими готическими")
        e.book_id = "PG345"  # force single-book
        e.multi_book_ids = []
        e.multi_book_titles = []
        plan = _plan_book_compare(e)
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["affinity_by_book"])

    def test_composite_render_note_present_on_multi_book(self):
        # Multi-book plan MUST stamp a render note telling the renderer
        # to show both side-by-side. Without it the renderer routinely
        # drops second book («here's first; ask again for second»).
        e = extract("compare Dracula and Frankenstein")
        plan = _plan_book_compare(e)
        self.assertTrue(plan.render_notes, "expected render_notes for composite")
        joined = " ".join(plan.render_notes).lower()
        # Must mention both books AND mention side-by-side / composite intent
        self.assertTrue(
            "composite" in joined or "обе" in joined or "всех книг" in joined,
            msg=f"render_note must instruct composite layout; got: {joined!r}",
        )

    def test_three_books_capped_at_three_affinity_calls(self):
        # Cap at 3 books — W-5 says «N or N-paired calls», implementation
        # caps at 3 to bound wall-clock.
        e = extract("compare Dracula and Frankenstein and Treasure Island")
        plan = _plan_book_compare(e)
        affinity_count = sum(1 for s in plan.steps
                             if s.tool == "affinity_by_book")
        self.assertEqual(affinity_count, 3)

    def test_secondary_book_steps_are_optional(self):
        # Primary book step must run; secondaries must be optional so
        # one slow / failing book doesn't kill the whole plan.
        e = extract("compare Dracula and Frankenstein")
        plan = _plan_book_compare(e)
        affinity_steps = [s for s in plan.steps if s.tool == "affinity_by_book"]
        self.assertFalse(affinity_steps[0].optional,
                         msg="primary affinity_by_book must be required")
        self.assertTrue(affinity_steps[1].optional,
                        msg="secondary affinity_by_book must be optional")


# ---------------------------------------------------------------------------
# Russian Frankenstein in KNOWN_BOOKS
# ---------------------------------------------------------------------------


class RussianFrankensteinResolves(unittest.TestCase):

    def test_known_books_has_russian_nominative(self):
        self.assertIn("франкенштейн", KNOWN_BOOKS)
        pg, canonical = KNOWN_BOOKS["франкенштейн"]
        self.assertEqual(pg, "PG84")
        self.assertEqual(canonical, "Frankenstein")

    def test_known_books_has_russian_case_forms(self):
        # «Дракула или Франкенштейна» — accusative
        # «о Франкенштейне» — prepositional
        # «к Франкенштейну» — dative
        for form in ("франкенштейна", "франкенштейну",
                     "франкенштейне", "франкенштейном"):
            self.assertIn(form, KNOWN_BOOKS,
                          msg=f"case form {form!r} missing from KNOWN_BOOKS")

    def test_extractor_finds_both_books_in_russian_query(self):
        e = extract("что сложнее — Дракула или Франкенштейн")
        # Primary or multi must include both
        all_ids = {e.book_id, *e.multi_book_ids}
        self.assertIn("PG84", all_ids)
        self.assertIn("PG345", all_ids)


# ---------------------------------------------------------------------------
# Intent: «что сложнее X или Y» without «читать»
# ---------------------------------------------------------------------------


class WhatIsHarderIntentRoutes(unittest.TestCase):

    def test_chto_slozhnee_routes_to_readability_compare(self):
        # W-5: without the explicit «читать» / «для чтения», original
        # rules missed this and fell to clarify.
        intent = classify("что сложнее — Дракула или Франкенштейн")
        self.assertEqual(intent.label, "book_readability_compare",
                         msg=f"got {intent.label} (conf {intent.confidence})")

    def test_chto_slozhnee_english_or_pattern(self):
        intent = classify("what is harder, Dracula or Frankenstein")
        self.assertEqual(intent.label, "book_readability_compare")

    def test_chto_slozhnee_word_keyword_routes_away_from_book_compare(self):
        # Negative: «что сложнее запомнить — слово X или слово Y» —
        # the «запомнить» / explicit word-question is a memorization
        # query, not book readability. The rule's negative lookahead
        # on «\s+слов» catches the closest-attached "слов" case so
        # «что сложнее слов X или Y» (без —/тире) avoids book bucket.
        intent = classify("что сложнее слов")
        self.assertNotEqual(intent.label, "book_readability_compare",
                            msg="bare word-question must not bucket as book compare")


# ---------------------------------------------------------------------------
# country_compare — both sides present
# ---------------------------------------------------------------------------


class CountryCompareReturnsBothSides(unittest.TestCase):

    def test_country_compare_plan_has_two_steps(self):
        e = extract("BrE vs AmE")
        plan = _plan_country_compare(e)
        tools = [s.tool for s in plan.steps]
        self.assertEqual(tools, ["top_authors_by_country",
                                  "top_authors_by_country"])

    def test_country_compare_plan_covers_GB_and_US(self):
        e = extract("BrE vs AmE")
        plan = _plan_country_compare(e)
        countries = {s.args.get("country") for s in plan.steps}
        self.assertEqual(countries, {"GB", "US"})

    def test_country_compare_has_composite_render_note(self):
        # W-5: renderer was reportedly returning «односторонний список».
        # The plan now stamps an explicit render note forbidding that.
        e = extract("BrE vs AmE")
        plan = _plan_country_compare(e)
        self.assertTrue(plan.render_notes)
        joined = " ".join(plan.render_notes).lower()
        self.assertTrue(
            "side-by-side" in joined or "обе" in joined,
            msg=f"render_note must demand both-sides layout; got: {joined!r}",
        )

    def test_compare_british_and_american_authors_routes_to_country_compare(self):
        # «сравни британских и американских авторов» used to hit
        # author_compare and return an empty plan (no author_regex).
        intent = classify("сравни британских и американских авторов")
        self.assertEqual(intent.label, "country_compare",
                         msg=f"got {intent.label} (conf {intent.confidence})")


# ---------------------------------------------------------------------------
# author fan-out for «По и Лавкрафта одновременно»
# ---------------------------------------------------------------------------


class AuthorFanOutTwoAuthorsBothCalled(unittest.TestCase):

    def test_words_strakha_po_lovecraft_fans_out(self):
        # E5 / W-5: this is the canonical multi-author test case.
        e = extract("слова страха у По и Лавкрафта одновременно")
        self.assertEqual(e.author_regex, "^Poe,")
        self.assertIn("^Lovecraft,", e.multi_author_regex)
        plan = build("word_emotion", e)
        tools = [s.tool for s in plan.steps]
        # Both authors must produce their own emotion_collocates step
        self.assertEqual(tools, ["emotion_collocates", "emotion_collocates"])
        scopes = [s.args["scope"]["author"] for s in plan.steps]
        self.assertEqual(scopes, ["^Poe,", "^Lovecraft,"])


# ---------------------------------------------------------------------------
# author_compare → book_compare redirect when entities have books
# ---------------------------------------------------------------------------


class AuthorCompareRedirectsToBookCompareForTwoBooks(unittest.TestCase):

    def test_sravni_two_books_redirects_to_book_compare(self):
        # «сравни Dracula и Frankenstein» classifies as author_compare
        # (the generic «сравни X и Y» rule), but has no authors. The
        # author_compare builder now redirects to book_compare when ≥2
        # books are present, avoiding the old «Нужны два автора» bounce.
        e = extract("сравни Dracula и Frankenstein")
        intent = classify("сравни Dracula и Frankenstein")
        self.assertEqual(intent.label, "author_compare")
        plan = build(intent.label, e)
        self.assertEqual(plan.intent, "book_compare",
                         msg="author_compare must redirect to book_compare "
                             "when 2 books were extracted")
        # And the redirect produces a multi-book fan-out
        affinity_count = sum(1 for s in plan.steps
                             if s.tool == "affinity_by_book")
        self.assertGreaterEqual(affinity_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
