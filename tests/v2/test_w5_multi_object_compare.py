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


# ---------------------------------------------------------------------------
# Phase 5 W-5 (2026-05-24) — fan-out invariant stamps a render_note so the
# renderer doesn't collapse multi-author results to a single author.
# ---------------------------------------------------------------------------


class FanOutInvariantStampsPerAuthorRenderNote(unittest.TestCase):

    def test_word_emotion_multi_author_gets_per_author_render_note(self):
        # W-5 root: «слова страха у По и Лавкрафта» fan-out worked (2 steps)
        # but renderer collapsed to one author because no instruction said
        # «show both». The invariant now stamps the instruction.
        e = extract("слова страха у По и Лавкрафта одновременно")
        plan = build("word_emotion", e)
        self.assertGreaterEqual(len(plan.steps), 2)
        self.assertTrue(plan.render_notes,
                        "expected per-author render note from fan-out invariant")
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("per-author", joined,
                      msg=f"render_note must spell out per-author; got: {joined!r}")
        # And it must name BOTH authors explicitly so the LLM can verify
        self.assertIn("poe", joined)
        self.assertIn("lovecraft", joined)

    def test_author_vocab_multi_author_gets_per_author_render_note(self):
        # «слова По и Лавкрафта» — sibling case via author_vocab fan-out
        e = extract("слова По и Лавкрафта")
        plan = build("author_vocab", e)
        self.assertGreaterEqual(len(plan.steps), 2)
        self.assertTrue(plan.render_notes)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("per-author", joined)

    def test_single_author_no_fanout_no_invariant_note(self):
        # Negative: single-author query gets no per-author note added by
        # the invariant (it might still have OTHER notes from the builder).
        e = extract("слова страха у По")
        plan = build("word_emotion", e)
        # Plan has only ONE step — no fan-out, no per-author note.
        self.assertEqual(len(plan.steps), 1)
        joined = " ".join(plan.render_notes).lower()
        self.assertNotIn("per-author", joined)


# ---------------------------------------------------------------------------
# Phase 5 W-5 (2026-05-24) — honest "composite not supported" disclosure
# when user names ≥2 authors/books but the intent has no fan-out path.
# ---------------------------------------------------------------------------


class HonestDropDisclosureForUnsupportedComposite(unittest.TestCase):

    def test_author_top_words_with_two_authors_discloses_drop(self):
        # «топ-5 биграмм у По и Лавкрафта» — author_top_words tool takes
        # one author_regex. Pre-W-5, second author silently dropped.
        # Now the invariant stamps a disclosure so the renderer admits it.
        e = extract("топ-5 биграмм у По и Лавкрафта")
        plan = build("author_top_words", e)
        # Single step (tool can't fan out)
        self.assertEqual(len(plan.steps), 1)
        self.assertTrue(plan.render_notes,
                        "expected dropped-author honesty disclosure")
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("не поддерж", joined,
                      msg=f"disclosure must say composite isn't supported; "
                          f"got: {joined!r}")

    def test_author_metadata_with_two_authors_discloses_drop(self):
        # «биография По и Лавкрафта» — author_metadata is per-author.
        e = extract("биография По и Лавкрафта")
        plan = build("author_metadata", e)
        self.assertEqual(len(plan.steps), 1)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("не поддерж", joined)

    def test_book_archaic_with_two_books_discloses_drop(self):
        # «архаизмы в Dracula и Frankenstein» — book_archaic_words is
        # per-book. Disclose that second book wasn't queried.
        e = extract("архаизмы в Dracula и Frankenstein")
        plan = build("book_archaic", e)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("несколько книг", joined,
                      msg=f"disclosure must mention multi-book gap; got: {joined!r}")

    def test_book_emotion_with_two_books_discloses_drop(self):
        # «эмоции в Dracula и Frankenstein» — same gap as book_archaic.
        e = extract("эмоции в Dracula и Frankenstein")
        plan = build("book_emotion", e)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("несколько книг", joined)

    def test_author_compare_with_two_authors_no_drop_disclosure(self):
        # Negative: author_compare natively handles 2 authors via the
        # compare_authors tool — must NOT emit the drop disclosure.
        e = extract("сравни По и Лавкрафта")
        plan = build("author_compare", e)
        joined = " ".join(plan.render_notes).lower()
        self.assertNotIn("не поддерж", joined)
        self.assertNotIn("несколько книг", joined)

    def test_book_compare_with_two_books_no_drop_disclosure(self):
        # Negative: book_compare natively fans out per book. No drop note.
        e = extract("сравни Dracula и Frankenstein")
        plan = build("book_compare", e)
        joined = " ".join(plan.render_notes).lower()
        self.assertNotIn("несколько книг, но интент", joined)


# ---------------------------------------------------------------------------
# Phase 5 W-5 — English / mixed-language country_compare detection.
# ---------------------------------------------------------------------------


class CountryCompareEnglishAndMixed(unittest.TestCase):

    def test_compare_british_and_american_authors_english_routes_country_compare(self):
        # Pure English «compare British and American authors» used to
        # fall through to author_compare with 0 steps. Country2
        # alternation missed plain `american\w*` (Russian-only); also
        # nominal alternation missed plain English «authors».
        intent = classify("compare British and American authors")
        self.assertEqual(intent.label, "country_compare",
                         msg=f"got {intent.label} (conf {intent.confidence})")

    def test_mixed_russian_verb_english_adjective_routes_country_compare(self):
        # «сравни British и American авторов» — Russian verb + English
        # adjective + Russian nominal. Same alternation bug.
        intent = classify("сравни British и American авторов")
        self.assertEqual(intent.label, "country_compare")

    def test_country_codes_only_routes_country_compare(self):
        # «compare GB and US writers» — bare country codes only.
        intent = classify("compare GB and US writers")
        self.assertEqual(intent.label, "country_compare")

    def test_or_connector_works(self):
        # «compare British or American novels» — «или / or» connector
        # instead of «и / and». Was missing from the conjunction
        # alternation pre-W-5.
        intent = classify("compare British or American novels")
        self.assertEqual(intent.label, "country_compare")

    def test_author_compare_redirects_to_country_compare_with_two_countries(self):
        # When the intent classifier still misses but the entity
        # extractor has country=GB AND the raw text mentions another
        # country, _plan_author_compare should redirect to country_compare
        # instead of bouncing to clarify.
        e = extract("сравни British и Russian книги")
        plan = build("author_compare", e)
        self.assertEqual(plan.intent, "country_compare",
                         msg=f"expected country_compare redirect; got {plan.intent}")
        # Must have ≥1 top_authors_by_country step
        self.assertTrue(any(s.tool == "top_authors_by_country"
                            for s in plan.steps))


# ---------------------------------------------------------------------------
# Phase 5 W-5 — natural Russian multi-author phrasings routing.
# ---------------------------------------------------------------------------


class NaturalMultiAuthorPhrasingsRoute(unittest.TestCase):

    def test_u_x_i_y_what_words_routes_to_author_vocab(self):
        # «у По и Лавкрафта какие слова» — used to fall to clarify.
        intent = classify("у По и Лавкрафта какие слова")
        self.assertEqual(intent.label, "author_vocab")
        e = extract("у По и Лавкрафта какие слова")
        plan = build(intent.label, e)
        # Fan-out invariant clones to 2 steps
        self.assertEqual(len(plan.steps), 2)

    def test_what_words_u_x_i_y_routes_to_author_vocab(self):
        # «какие слова у По и Лавкрафта» — word-order flip
        intent = classify("какие слова у По и Лавкрафта")
        self.assertEqual(intent.label, "author_vocab")

    def test_u_x_i_y_what_scarier_routes_to_word_emotion(self):
        # «у Лавкрафта и По что страшнее» — multi-author emotion contrast.
        intent = classify("у Лавкрафта и По что страшнее")
        self.assertEqual(intent.label, "word_emotion")
        e = extract("у Лавкрафта и По что страшнее")
        plan = build(intent.label, e)
        self.assertEqual(len(plan.steps), 2)

    def test_kto_pishet_proshche_x_or_y_routes_to_author_compare(self):
        # «кто пишет проще: Дойл или Кристи»
        intent = classify("кто пишет проще: Дойл или Кристи")
        self.assertEqual(intent.label, "author_compare")
        e = extract("кто пишет проще: Дойл или Кристи")
        plan = build(intent.label, e)
        # author_compare uses compare_authors tool with both regex's
        self.assertIn("compare_authors", [s.tool for s in plan.steps])


# ---------------------------------------------------------------------------
# Phase 5 W-5 — book_readability redirect to book_readability_compare
# when ≥2 books are surfaced even without the «сложнее ... или» pattern.
# ---------------------------------------------------------------------------


class BookReadabilityRedirectsForMultiBook(unittest.TestCase):

    def test_uroven_slozhnosti_dracula_i_frankenstein_redirects(self):
        # «уровень сложности Dracula и Frankenstein» — no «или», but the
        # presence of 2 books is itself a compare signal.
        e = extract("уровень сложности Dracula и Frankenstein")
        plan = build("book_readability", e)
        self.assertEqual(plan.intent, "book_readability_compare")
        self.assertEqual(len(plan.steps), 2)
        pgs = {s.args.get("pg_id") for s in plan.steps if "pg_id" in s.args}
        self.assertEqual(pgs, {"PG84", "PG345"})

    def test_single_book_readability_keeps_single_step(self):
        # Negative: single-book readability stays single-step (no regression).
        e = extract("уровень сложности Dracula")
        plan = build("book_readability", e)
        self.assertEqual(plan.intent, "book_readability")
        self.assertEqual(len(plan.steps), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
