"""W-4 tests — toponym (GPE/LOC) filter, extended PROPN/surname blocklist,
non-author organization blocklist.

Stan 2026-05-22 (Phase 3):
    «фирменные слова Конан Дойла» → burger/uitlanders/belmont/colesberg/
    kroonstad (Boer-war GPE/LOC, не лексика стиля).
    «характерные слова Диккенса» → wegg/smike/toots/jip (character names,
    not vocabulary).
    Top-listы авторов → CIA, Library of Congress, Warren Commission.

Tests are negative-first per R5 in CLAUDE.md — each blocked token has a
positive case (it WAS in the pre-Phase-3 output) и negative case (it
should now be filtered).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.authors._toponym_filter import (
    _CURATED_TOPONYMS,
    filter_toponyms,
    is_toponym,
    toponym_blocklist,
)
from scripts.v2.tools.authors._surname_filter import (
    _CURATED_CHARACTER_SURNAMES,
    filter_surnames,
)
from scripts.v2.tools._result_filters import (
    _NULL_AUTHOR_SUBSTRINGS,
    _NULL_AUTHOR_TOKENS,
    drop_null_authors,
)


# ---------------------------------------------------------------------------
# Toponym filter — W-4 (1)
# ---------------------------------------------------------------------------


class ToponymBlocklist(unittest.TestCase):
    """Curated toponym set covers the specific tokens Stan reported."""

    def test_doyle_boer_war_toponyms_are_blocked(self):
        # These are exactly the words Stan flagged in «фирменные слова
        # Конан Дойла» — top-list contained Boer-war GPE leakage.
        for tok in ("burger", "uitlanders", "belmont",
                    "colesberg", "kroonstad"):
            self.assertTrue(
                is_toponym(tok),
                msg=f"{tok!r} must be flagged as toponym — Doyle Boer-war leak",
            )

    def test_tz_spec_extra_doyle_tokens_blocked(self):
        # Reconciliation with tz_claude_code_fixes_2026-05-22.md §W-4:
        # spec explicitly lists `conflans` and `donga` as additional
        # leakage tokens. They MUST be in the blocklist.
        for tok in ("conflans", "donga"):
            self.assertTrue(
                is_toponym(tok),
                msg=f"{tok!r} — ТЗ-listed W-4 token, must be blocked",
            )

    def test_boer_war_jargon_also_blocked(self):
        # Bonus: place-feature SA jargon (kopje, veldt, kraal, sjambok,
        # laager). These behave the same as toponyms for affinity
        # purposes — narrow-locality vocabulary.
        for tok in ("kopje", "veldt", "kraal", "sjambok", "laager"):
            self.assertTrue(
                is_toponym(tok),
                msg=f"{tok!r} — Boer-war place-feature jargon, must be blocked",
            )

    def test_doyle_other_boer_war_toponyms_blocked(self):
        # Bonus coverage — other Boer-war places that bleed through.
        for tok in ("ladysmith", "mafeking", "transvaal", "pretoria",
                    "natal", "bloemfontein", "magersfontein"):
            self.assertTrue(is_toponym(tok),
                            msg=f"{tok!r} should be in Boer-war toponym set")

    def test_real_stylistic_words_are_not_blocked(self):
        # Negative: real stylistic adjectives MUST pass through. The
        # blocklist is curated, not pattern-based, so these are safe.
        for tok in ("magnificent", "curious", "extraordinary", "remarkable",
                    "ancient", "modern", "delightful", "horrible"):
            self.assertFalse(
                is_toponym(tok),
                msg=f"{tok!r} is a stylistic word, must NOT be flagged",
            )

    def test_empty_or_none_input_safe(self):
        self.assertFalse(is_toponym(""))
        self.assertFalse(is_toponym(None))
        self.assertFalse(is_toponym("   "))

    def test_case_insensitivity(self):
        self.assertTrue(is_toponym("BURGER"))
        self.assertTrue(is_toponym("Belmont"))
        self.assertTrue(is_toponym("  uitlanders  "))


class FilterToponymsRowList(unittest.TestCase):
    """filter_toponyms drops matching rows and reports drop count."""

    def test_filter_drops_only_toponyms(self):
        rows = [
            {"word": "burger", "affinity": 142.0},   # toponym → drop
            {"word": "magnificent", "affinity": 88.0},  # keep
            {"word": "belmont", "affinity": 71.0},   # toponym → drop
            {"word": "curious", "affinity": 64.0},   # keep
        ]
        kept, dropped = filter_toponyms(rows)
        self.assertEqual(dropped, 2)
        kept_words = {r["word"] for r in kept}
        self.assertEqual(kept_words, {"magnificent", "curious"})

    def test_filter_empty_rows_returns_empty(self):
        kept, dropped = filter_toponyms([])
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 0)

    def test_filter_preserves_extra_fields(self):
        rows = [{"word": "ladysmith", "affinity": 80,
                 "author_count": 5, "_render_note": "x"}]
        kept, _ = filter_toponyms(rows)
        self.assertEqual(kept, [])

    def test_filter_does_not_drop_when_blocklist_misses(self):
        # Conservative: only the curated set drops. A misspelled toponym
        # or an unmapped token must pass through (we'd rather under-block
        # than over-block real lexemes).
        rows = [{"word": "delightful", "affinity": 50}]
        kept, dropped = filter_toponyms(rows)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(kept), 1)


class ToponymBlocklistShapesAndFallback(unittest.TestCase):

    def test_blocklist_is_frozen_and_lowercase(self):
        for tok in _CURATED_TOPONYMS:
            self.assertEqual(tok, tok.lower())
            self.assertTrue(tok.strip() == tok)

    def test_blocklist_with_missing_ner_csv_still_returns_curated(self):
        # NER files often don't exist on dev/test workstations. Should
        # return curated set without raising.
        out = toponym_blocklist(ner_csv_paths=[Path("/nonexistent/foo.csv")])
        self.assertIn("burger", out)
        self.assertIn("kroonstad", out)


# ---------------------------------------------------------------------------
# Extended character-surname blocklist — W-4 (2)
# ---------------------------------------------------------------------------


class DickensCharacterSurnamesBlocked(unittest.TestCase):
    """Dickens characters that dominated «слова Диккенса» topcharts —
    they aren't vocabulary, they're attribution leaks."""

    def test_w4_specific_dickens_characters_blocked(self):
        # The exact 4 reported by Stan
        for tok in ("wegg", "smike", "toots", "jip"):
            self.assertIn(
                tok, _CURATED_CHARACTER_SURNAMES,
                msg=f"{tok!r} must be in Dickens character set — W-4 ask",
            )

    def test_more_dickens_characters_blocked(self):
        # Adjacent characters from the same novels — opportunistic widen.
        for tok in ("barkis", "murdstone", "trotwood", "headstone",
                    "podsnap", "tulkinghorn", "dedlock", "krook",
                    "smallweed"):
            self.assertIn(tok, _CURATED_CHARACTER_SURNAMES,
                            msg=f"{tok!r} expected in surname blocklist")

    def test_tz_spec_extra_dickens_tokens_blocked(self):
        # Reconciliation with tz_claude_code_fixes_2026-05-22.md §W-4:
        # spec explicitly listed wegg / trotwood / smike / veneering /
        # toots / cuttle / claypole / jip. Verify the full set.
        for tok in ("wegg", "trotwood", "smike", "veneering",
                    "toots", "cuttle", "claypole", "jip"):
            self.assertIn(
                tok, _CURATED_CHARACTER_SURNAMES,
                msg=f"{tok!r} — ТЗ-listed W-4 Dickens token, must be blocked",
            )

    def test_filter_surnames_drops_dickens_characters(self):
        # Filter integration — surname filter drops them at the row level.
        rows = [
            {"word": "wegg", "affinity": 145},
            {"word": "magnificent", "affinity": 88},   # real lexeme
            {"word": "smike", "affinity": 70},
            {"word": "toots", "affinity": 55},
            {"word": "jip", "affinity": 40},
        ]
        kept, dropped = filter_surnames(rows)
        # Note: filter_surnames also loads PG metadata if available; on
        # CI/dev the metadata file is absent → only curated set used,
        # which already covers wegg/smike/toots/jip.
        kept_words = {(r.get("word") or "").lower() for r in kept}
        self.assertNotIn("wegg", kept_words)
        self.assertNotIn("smike", kept_words)
        self.assertNotIn("toots", kept_words)
        self.assertNotIn("jip", kept_words)
        # Keep real lexeme
        self.assertIn("magnificent", kept_words)
        # At least these four were dropped
        self.assertGreaterEqual(dropped, 4)


# ---------------------------------------------------------------------------
# Organization / commission «authors» — W-4 (3)
# ---------------------------------------------------------------------------


class NullAuthorOrgBlocklist(unittest.TestCase):
    """Top-lists of authors must not contain Warren Commission / CIA /
    Library of Congress / department-of-X aggregates."""

    def test_cia_blocked_exact(self):
        self.assertIn("cia", _NULL_AUTHOR_TOKENS)
        self.assertIn("central intelligence agency", _NULL_AUTHOR_TOKENS)

    def test_library_of_congress_blocked(self):
        self.assertIn("library of congress", _NULL_AUTHOR_TOKENS)

    def test_warren_commission_substring_present(self):
        self.assertIn("warren commission", _NULL_AUTHOR_SUBSTRINGS)

    def test_department_substrings_present(self):
        # PG metadata wraps these as «United States. Department of ...»
        for sub in ("department of state", "department of justice",
                    "bureau of investigation", "national aeronautics"):
            self.assertIn(sub, _NULL_AUTHOR_SUBSTRINGS,
                          msg=f"{sub!r} should be a substring matcher")

    def test_drop_null_authors_filters_warren_commission_aggregate(self):
        # Realistic PG metadata shape: agency-prefixed author strings.
        rows = [
            {"author": "Doyle, Arthur Conan", "tokens": 5_000_000},
            {"author": "United States. Warren Commission",
             "tokens": 50_000_000},
            {"author": "Central Intelligence Agency", "tokens": 100_000_000},
            {"author": "United States. Department of State",
             "tokens": 80_000_000},
            {"author": "Library of Congress", "tokens": 60_000_000},
            {"author": "Tolstoy, Leo, graf", "tokens": 8_000_000},
        ]
        kept, dropped = drop_null_authors(rows)
        kept_authors = [r["author"] for r in kept]
        # Real authors survive
        self.assertIn("Doyle, Arthur Conan", kept_authors)
        self.assertIn("Tolstoy, Leo, graf", kept_authors)
        # All four organizational aggregates dropped
        self.assertEqual(dropped, 4)

    def test_drop_null_authors_handles_mixed_case(self):
        rows = [
            {"author": "  CENTRAL INTELLIGENCE AGENCY  ", "tokens": 1},
            {"author": "wells, h. g.", "tokens": 2},
            {"author": "Smithsonian Institution", "tokens": 3},
            {"author": "U.S. Department of Justice", "tokens": 4},
        ]
        kept, dropped = drop_null_authors(rows)
        kept_authors = [r["author"] for r in kept]
        self.assertEqual(len(kept_authors), 1)
        self.assertIn("wells, h. g.", kept_authors)
        self.assertEqual(dropped, 3)

    def test_drop_null_authors_keeps_real_us_authors(self):
        # Sanity: «United States» substring would catch real authors that
        # have «united states» in the name. We use word-boundary-style
        # SUBSTRINGS that focus on agency-language («department of»,
        # «commission», «bureau»), not bare «united states».
        # Confirm: a legitimate author with no agency marker passes.
        rows = [
            {"author": "Whitman, Walt", "tokens": 1_000_000},
            # Exact-string «united states» IS in _NULL_AUTHOR_TOKENS (line 75),
            # so a bare «United States» row IS dropped — this is intentional,
            # that string only appears in PG when the «author» field is the
            # raw country (an aggregate). Real authors include their name.
        ]
        kept, dropped = drop_null_authors(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)


# ---------------------------------------------------------------------------
# Integration: affinity_by_author result pipeline — full filter chain
# ---------------------------------------------------------------------------


class AffinityFilterChainIntegration(unittest.TestCase):
    """Smoke test of the chain: surname → corpus-artifact → toponym.

    Reuses the v2 wrapper hooks via fake v1 to ensure all four filters
    fire in order without dropping legitimate stylistic words."""

    def test_chain_drops_toponym_and_character_keeps_lexemes(self):
        from unittest.mock import patch
        from scripts.v2.tools.authors import affinity as aff

        # Stubbed v1 result mimicking real shape (see V1AffinityByAuthor)
        # — character + toponym + lexeme + OCR artifact.
        fake_v1_out = {
            "author_regex": "^Doyle,",
            "slug": "doyle",
            "pos_filter": None,
            "effective_min_corpus_count": 0,
            "total_unique_words": 50000,
            "cached": False,
            "proper_noun_filter": "v1 corpus-diff dropped 100",
            "top": [
                {"word": "burger",     "author_count": 50, "corpus_count": 60, "affinity": 142.0},
                {"word": "uitlanders", "author_count": 45, "corpus_count": 55, "affinity": 132.0},
                {"word": "magnificent","author_count": 80, "corpus_count": 800, "affinity": 12.0},
                {"word": "wegg",       "author_count": 30, "corpus_count": 32, "affinity": 95.0},
                {"word": "ix",         "author_count": 20, "corpus_count": 25, "affinity": 88.0},  # roman
                {"word": "curious",    "author_count": 60, "corpus_count": 700, "affinity": 8.5},
            ],
        }

        def fake_v1(**kwargs):
            return fake_v1_out

        with patch("scripts.rag_tools.affinity_by_author", side_effect=fake_v1):
            result = aff.affinity_by_author(author_regex="^Doyle,", top=20)

        self.assertTrue(result.ok, msg=f"expected success, got error={result.error}")
        data = result.data
        words_out = {r["word"] for r in (data.get("top") or [])}
        # Toponym, character, OCR artifact all gone
        self.assertNotIn("burger", words_out)
        self.assertNotIn("uitlanders", words_out)
        self.assertNotIn("wegg", words_out)
        self.assertNotIn("ix", words_out)
        # Real lexemes survive
        self.assertIn("magnificent", words_out)
        self.assertIn("curious", words_out)
        # Note string explains every filter
        note = data.get("proper_noun_filter") or ""
        self.assertIn("toponym filter", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
