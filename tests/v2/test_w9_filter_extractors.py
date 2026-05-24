"""W-9 per-extractor unit tests + integration «filter reaches tool call».

Sister file to `test_w9_filter_enforcement.py`. That file tested
behavior at the plan-builder level (disclosure stamping, motion-verb
fallback). This one targets the gap below it:

  1. **Unit per extractor** — every filter extractor in
     `scripts/v2/planner/entities.py` has positive + negative cases.
     Negatives matter because regressions historically slipped in via
     paths that *did* fire on the positive but also fired on something
     they shouldn't (e.g. `german` matched inside `germanic`, E20).

  2. **Integration — filter reaches the tool call** — start from a raw
     query string, run the full `extract()` → builder pipeline, and
     assert the right value lands in `PlanStep.args` (or in
     `render_notes` when the tool doesn't accept the filter and must
     disclose). No silent drops.

R5 compliance: every extractor here has at least one positive AND one
negative case. R2 acceptance for W-9: «что почитать на B2 без
архаизмов» either applies B2 or surfaces a stable disclosure — and
the disclosure is now uniform across every run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import (
    extract,
    _find_country,
    _find_etymology,
    _find_exclude_archaic,
    _find_lang_hint,
    _find_level,
    _find_year_range,
)
from scripts.v2.planner.builders.book import (
    _plan_book_recommendation,
)
from scripts.v2.planner.builders.learning import _plan_learning


# ---------------------------------------------------------------------------
# Unit per extractor — positives + negatives (R5)
# ---------------------------------------------------------------------------


class FindLevelExtractor(unittest.TestCase):

    def test_b2_extracts_intermediate(self):
        self.assertEqual(_find_level("книги уровня B2"), "intermediate")

    def test_b1_extracts_intermediate(self):
        self.assertEqual(_find_level("recommend a book for B1"), "intermediate")

    def test_a2_extracts_basic(self):
        self.assertEqual(_find_level("A2 vocab"), "basic")

    def test_c1_extracts_advanced(self):
        self.assertEqual(_find_level("C1 reading"), "advanced")

    def test_word_basic_extracts_basic(self):
        self.assertEqual(_find_level("базовый словарь"), "basic")

    def test_word_intermediate_extracts(self):
        self.assertEqual(_find_level("средний уровень"), "intermediate")

    def test_word_advanced_extracts(self):
        self.assertEqual(_find_level("продвинутый словарь"), "advanced")

    def test_no_level_returns_none(self):
        self.assertIsNone(_find_level("что почитать"))

    def test_a2_substring_inside_word_does_not_match(self):
        # «а2» as part of a longer alphanumeric (e.g. ID или filename
        # ab2cd) must not trigger — word-boundary regex protects this.
        self.assertIsNone(_find_level("ab2cd is a code"))

    def test_b2_substring_inside_word_does_not_match(self):
        self.assertIsNone(_find_level("xb2y is a code"))


class FindCountryExtractor(unittest.TestCase):

    def test_british_extracts_gb(self):
        self.assertEqual(_find_country("британская классика"), "GB")

    def test_english_extracts_gb(self):
        self.assertEqual(_find_country("English literature"), "GB")

    def test_american_extracts_us(self):
        self.assertEqual(_find_country("American authors"), "US")

    def test_russian_extracts_ru(self):
        self.assertEqual(_find_country("русская литература"), "RU")

    def test_french_extracts_fr(self):
        self.assertEqual(_find_country("French novel"), "FR")

    def test_german_extracts_de(self):
        self.assertEqual(_find_country("German poetry"), "DE")

    def test_germanic_does_NOT_extract_de(self):
        # E20 regression — Latin-script aliases require word boundaries
        # so `german` doesn't match inside `germanic` (etymology family).
        self.assertIsNone(_find_country("germanic vs latinate ratio"))

    def test_amen_does_NOT_extract_us(self):
        # «amen» / «ame» are common Russian/English tokens; the «ame»
        # alias is Latin-script so it MUST require word boundary.
        # Inside the word «amen» (no boundary after) it must not match.
        self.assertIsNone(_find_country("сказал amen и пошёл"))

    def test_no_country_returns_none(self):
        self.assertIsNone(_find_country("что почитать"))


class FindYearRangeExtractor(unittest.TestCase):

    def test_explicit_range_extracts(self):
        self.assertEqual(_find_year_range("1840-1860 английская проза"),
                         (1840, 1860))

    def test_after_year_extracts(self):
        self.assertEqual(_find_year_range("книги после 1900 года"),
                         (1901, None))

    def test_before_year_extracts(self):
        self.assertEqual(_find_year_range("books before 1850"),
                         (None, 1849))

    def test_victorian_extracts_period(self):
        self.assertEqual(_find_year_range("викторианская эпоха"),
                         (1837, 1901))

    def test_xix_century_extracts_period(self):
        self.assertEqual(_find_year_range("XIX век"), (1800, 1899))

    def test_no_year_returns_pair_of_none(self):
        self.assertEqual(_find_year_range("что почитать"), (None, None))

    def test_modern_year_2099_does_not_match(self):
        # _YEAR pattern: 1500-2099. Year 2150 (3-digit prefix beyond
        # 20\d\d) does not match — defensive against accidental
        # phone-number / id substrings.
        self.assertEqual(_find_year_range("номер 2150"), (None, None))


class FindLangHintExtractor(unittest.TestCase):

    def test_english_literature_extracts_en(self):
        self.assertEqual(_find_lang_hint("English literature"), "en")

    def test_english_classics_ru_extracts_en(self):
        self.assertEqual(_find_lang_hint("английская классика"), "en")

    def test_french_novel_extracts_fr(self):
        self.assertEqual(_find_lang_hint("French novel"), "fr")

    def test_russian_classics_extracts_ru(self):
        self.assertEqual(_find_lang_hint("русская литература"), "ru")

    def test_german_literature_extracts_de(self):
        self.assertEqual(_find_lang_hint("German literature"), "de")

    def test_no_hint_returns_none(self):
        self.assertIsNone(_find_lang_hint("что почитать"))

    def test_neutral_topic_query_returns_none(self):
        # Negative: a query with no language-anchor token at all must
        # leave lang_hint unset. (We deliberately do NOT enforce
        # absence on bare `english` — the production pattern accepts
        # it as a language identifier even outside a literature
        # context, and that's the documented behavior.)
        self.assertIsNone(_find_lang_hint("recommend popular novels"))

    def test_partial_substring_inside_unrelated_word_returns_none(self):
        # Negative regression — `english` is the bare alternative in
        # the pattern, so it's guarded by `\b`. Word-internal substring
        # like «frenchify» / «germanize» must not flip the hint.
        self.assertIsNone(_find_lang_hint("frenchify the sauce"))
        self.assertIsNone(_find_lang_hint("germanize the spelling"))


class FindExcludeArchaicExtractor(unittest.TestCase):

    def test_bez_arxaizmov_returns_true(self):
        self.assertTrue(_find_exclude_archaic("без архаизмов"))

    def test_no_archaic_en_returns_true(self):
        self.assertTrue(_find_exclude_archaic("no archaic words please"))

    def test_modern_english_returns_true(self):
        self.assertTrue(_find_exclude_archaic("modern english only"))

    def test_modern_language_ru_returns_true(self):
        self.assertTrue(_find_exclude_archaic("на современном языке"))

    def test_neutral_query_returns_false(self):
        self.assertFalse(_find_exclude_archaic("что почитать"))

    def test_about_archaisms_does_NOT_return_true(self):
        # «расскажи про архаизмы у Walpole» is ABOUT archaisms, not a
        # request to EXCLUDE them. Pattern is anchored on «без X» /
        # «no archaic» / «exclude X», never on the bare topic word.
        self.assertFalse(_find_exclude_archaic("архаизмы у Walpole"))


class FindEtymologyExtractor(unittest.TestCase):
    # Negative regression for E20 — etymology vs country collision.

    def test_germanic_extracts_germanic(self):
        self.assertEqual(_find_etymology("germanic words"), "germanic")

    def test_latin_extracts_latin(self):
        self.assertEqual(_find_etymology("latin roots"), "latin")

    def test_french_extracts_french(self):
        self.assertEqual(_find_etymology("french loanwords"), "french")

    def test_no_etymology_returns_none(self):
        self.assertIsNone(_find_etymology("что почитать"))


# ---------------------------------------------------------------------------
# Integration — filter reaches the tool call (or is loudly disclosed)
# ---------------------------------------------------------------------------


class FilterReachesToolOrIsDisclosed(unittest.TestCase):
    """End-to-end: raw query → extract() → builder → PlanStep.args.

    The W-9 acceptance: every declared filter is either really applied
    (a real key/value lands in args) or surfaced via render_notes with
    an explicit DISCLOSE instruction. Silent drops are forbidden.
    """

    # -------- book_recommendation: the canonical Stan query --------

    def test_b2_bez_arxaizmov_discloses_both_level_and_archaic(self):
        # The exact phrasing from the W-9 ticket.
        e = extract("что почитать на B2 без архаизмов")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.intent, "book_recommendation")
        self.assertEqual(plan.steps[0].tool, "top_books_by_downloads")
        joined = " ".join(plan.render_notes).lower()
        # Both filters must be disclosed every time — no silent ignore.
        self.assertIn("архаизм", joined,
                      msg="exclude_archaic disclosure dropped")
        # «intermediate» (CEFR B2) must be acknowledged — pre-W-9 this
        # was silently dropped because top_books_by_downloads has no
        # CEFR arg and the builder didn't add a render note.
        self.assertIn("intermediate", joined,
                      msg="level disclosure dropped — silent ignore")
        # Both notes must carry the DISCLOSE instruction so the LLM
        # cannot quietly skip the disclosure.
        self.assertIn("disclose", joined)

    def test_b2_bez_arxaizmov_disclosure_is_stable_across_runs(self):
        # Repeat 5×, render_notes must be byte-identical — no flapping
        # disclosure (the «нестабильно» complaint from the W-9 ticket).
        notes_runs: list[tuple[str, ...]] = []
        for _ in range(5):
            e = extract("что почитать на B2 без архаизмов")
            plan = _plan_book_recommendation(e)
            notes_runs.append(tuple(plan.render_notes))
        self.assertEqual(len(set(notes_runs)), 1,
                         msg="render_notes drift between runs — disclosure unstable")

    def test_country_filter_disclosed_in_book_recommendation(self):
        # «британскую классику» — country=GB extracts, but
        # top_books_by_downloads has no country arg. Must disclose.
        e = extract("посоветуй британскую классику A2")
        plan = _plan_book_recommendation(e)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("gb", joined,
                      msg="country=GB silently dropped, no disclosure")
        self.assertIn("disclose", joined)

    def test_year_filter_disclosed_in_book_recommendation(self):
        e = extract("recommend a B2 English book published after 1900")
        plan = _plan_book_recommendation(e)
        joined = " ".join(plan.render_notes).lower()
        self.assertIn("1901", joined,
                      msg="year_from=1901 silently dropped, no disclosure")

    def test_top_n_propagates_to_book_recommendation_tool_args(self):
        # Positive: top_books_by_downloads accepts `top`, so user's
        # numeric hint must flow into args (not silently use default 20).
        # «top 50» matches `_TOPN_RE` directly — the `recommend N`
        # phrasing is intentionally not in the regex (would over-
        # match phone numbers / dates in free prose).
        e = extract("top 50 popular English books")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.steps[0].args["top"], 50)

    def test_lang_hint_propagates_to_book_recommendation_tool_args(self):
        # Positive: lang_hint actually applied (real filter, not disclosure).
        e = extract("recommend a French novel for B1")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.steps[0].args.get("lang"), "fr")

    def test_no_filter_query_has_zero_render_notes(self):
        # Negative: bare «что почитать» has no filter signals, so the
        # builder must NOT stamp any render_notes (otherwise we'd be
        # disclosing filters the user never asked for — noise).
        e = extract("что почитать")
        plan = _plan_book_recommendation(e)
        self.assertEqual(plan.render_notes, [])

    # -------- learning_words: level + top_n + scope actually applied --------

    def test_learning_words_actually_passes_level_to_tool_args(self):
        # Positive: level is real on this tool (learning_words has CEFR).
        e = extract("дай B2 слова из Pride and Prejudice")
        plan = _plan_learning(e)
        self.assertEqual(plan.steps[0].tool, "learning_words")
        self.assertEqual(plan.steps[0].args["level"], "intermediate")

    def test_learning_words_actually_passes_top_n_to_tool_args(self):
        e = extract("дай 25 B2 слов из Pride and Prejudice")
        plan = _plan_learning(e)
        self.assertEqual(plan.steps[0].args["top"], 25)

    def test_learning_words_caps_top_n_at_30(self):
        # Negative: top_n above the cap is clamped (this is honest —
        # the wrapper enforces a 30-word per-call budget, and the user
        # is told about it via the _capped_from marker).
        e = extract("дай 300 слов B2 из Pride and Prejudice")
        plan = _plan_learning(e)
        self.assertEqual(plan.steps[0].args["top"], 30)
        self.assertEqual(plan.steps[0].args.get("_capped_from"), 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
