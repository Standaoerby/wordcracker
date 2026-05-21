"""EntityResolver v5 unit tests — Phase 1.

Closes R14 entity-resolution regressions as unit tests with mocked
corpus metadata (no live ChromaDB / Ollama needed):

  - B-R14-4  homoglyph fold: «Доcтоевский» (Latin c) → resolves
  - B-R14-9  RU genitive: «Толстого» → Толстой; «Братьев Карамазовых»
             → Brothers Karamazov PG28054
  - B-R14-13 prominence ranking: «Hugo» → Victor Hugo, not obscure
             Ganz, Hugo with similar fuzz score
  - B-R14-14 canonical display: resolved.display is authoritative

Plus pipeline contract tests (normalization steps, RU lemmatize rules,
prominence index thread-safety, confidence-gap math).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import entity_resolver as er


# =====================================================================
# Normalization contract
# =====================================================================


class NormalizationSteps(unittest.TestCase):
    def test_pure_latin_query_lowercased(self):
        r = er.normalize_query("Doyle")
        self.assertEqual(r.output, "doyle")
        self.assertIn("lowercased", r.steps)

    def test_cyrillic_query_lowercased(self):
        r = er.normalize_query("Толстой")
        self.assertEqual(r.output, "толстой")

    def test_nfkc_folds_compat(self):
        # Full-width Latin 'A' → ASCII 'A'
        r = er.normalize_query("Ｄoyle")
        self.assertEqual(r.output, "doyle")
        self.assertTrue(any("NFKC" in s for s in r.steps))

    def test_homoglyph_latin_c_inside_cyrillic_token(self):
        """B-R14-4: «Доcтоевского» where 'c' is Latin must fold to Cyrillic."""
        # Latin c (U+0063), all other chars Cyrillic
        q = "Доcтоевского"
        # Sanity: ensure we built the right test string
        self.assertEqual(q[2], "c")    # ASCII c
        r = er.normalize_query(q)
        # After fold, q has Cyrillic с everywhere
        self.assertNotIn("c", r.output, "Latin 'c' should fold to Cyrillic 'с'")
        self.assertIn("с", r.output)
        self.assertTrue(
            any("homoglyph" in s.lower() for s in r.steps),
            f"steps={r.steps}"
        )

    def test_homoglyph_pure_latin_not_touched(self):
        # «Doyle» is pure Latin — no Cyrillic mixed — must NOT be folded.
        r = er.normalize_query("Doyle")
        self.assertEqual(r.output, "doyle")
        self.assertFalse(any("homoglyph" in s.lower() for s in r.steps))

    def test_dashes_unified(self):
        r = er.normalize_query("найди–ка")
        self.assertIn("-", r.output)
        self.assertTrue(any("dashes" in s.lower() for s in r.steps))

    def test_quotes_stripped(self):
        r = er.normalize_query('"Pride and Prejudice"')
        self.assertEqual(r.output, "pride and prejudice")
        self.assertTrue(any("quote" in s.lower() for s in r.steps))

    def test_idempotent(self):
        once = er.normalize_query("Толстого").output
        twice = er.normalize_query(once).output
        self.assertEqual(once, twice)

    def test_empty_query(self):
        r = er.normalize_query("")
        self.assertEqual(r.output, "")


class RULemmatizeAuthor(unittest.TestCase):
    """B-R14-9: RU genitive author forms must fold to nominative."""

    def test_tolstogo_to_tolstoy(self):
        out, trace = er.ru_lemmatize_author_query("толстого")
        self.assertEqual(out, "толстой")
        self.assertEqual(len(trace), 1)
        self.assertIn("ого → ой", trace[0])

    def test_dostoevskogo_to_dostoevsky(self):
        out, trace = er.ru_lemmatize_author_query("достоевского")
        self.assertEqual(out, "достоевский")

    def test_chekhova_to_chekhov(self):
        out, _ = er.ru_lemmatize_author_query("чехова")
        self.assertEqual(out, "чехов")

    def test_swifta_to_swift(self):
        # Свифта → Свифт (short -а suffix)
        out, _ = er.ru_lemmatize_author_query("свифта")
        self.assertEqual(out, "свифт")

    def test_pure_latin_untouched(self):
        out, trace = er.ru_lemmatize_author_query("doyle")
        self.assertEqual(out, "doyle")
        self.assertEqual(trace, [])

    def test_short_token_not_folded(self):
        """No fold for <4-char tokens — too risky («Анна» = 4 chars, OK)"""
        # 'по' (preposition) should not be folded
        out, _ = er.ru_lemmatize_author_query("по")
        self.assertEqual(out, "по")


class RUBookTitleAlias(unittest.TestCase):
    """B-R14-9 part 2: RU book titles in any case → canonical PG id."""

    def test_brothers_karamazov_genitive_resolves(self):
        hit = er.resolve_ru_book_alias("братьев карамазовых")
        self.assertIsNotNone(hit)
        pg_id, canon_en, trace = hit
        self.assertEqual(pg_id, "PG28054")
        self.assertEqual(canon_en, "The Brothers Karamazov")
        self.assertIn("братья карамазовы", trace)

    def test_brothers_karamazov_nominative_resolves(self):
        hit = er.resolve_ru_book_alias("братья карамазовы")
        self.assertIsNotNone(hit)
        pg_id, canon_en, _ = hit
        self.assertEqual(pg_id, "PG28054")

    def test_anna_karenina_genitive_resolves(self):
        hit = er.resolve_ru_book_alias("анны карениной")
        self.assertIsNotNone(hit)
        pg_id, _, _ = hit
        self.assertEqual(pg_id, "PG1399")

    def test_unknown_title_returns_none(self):
        self.assertIsNone(er.resolve_ru_book_alias("война и мир"))
        # ↑ это IS valid title but in KNOWN_BOOKS path, not in RU alias map.
        # The RU alias map is only for titles we explicitly added because
        # they're missing from KNOWN_BOOKS.


# =====================================================================
# Prominence ranking — fixture mocked metadata
# =====================================================================


def _mock_metadata_df(rows: list[dict]) -> pd.DataFrame:
    """Build a fake _metadata_df with the columns the resolver expects."""
    return pd.DataFrame(rows)


class ProminenceRanking(unittest.TestCase):
    """B-R14-13: «Hugo» must resolve to Victor Hugo (~12K downloads)
    over the obscure «Ganz, Hugo» with similar fuzz score."""

    def setUp(self):
        # Clear prominence cache
        with er._prom_lock:
            er._prom_state["data"] = None

    def test_hugo_resolves_to_victor_not_ganz(self):
        """Two authors match fuzz score similarly on query 'Hugo':
           - 'Hugo, Victor' — well-known, 12000 downloads, 5 books
           - 'Ganz, Hugo'  — obscure, 10 downloads, 1 book
        Without prominence ranking, fuzzy may pick Ganz (his first name
        is exactly 'Hugo'). v5 must rank Victor first."""
        fake_df = _mock_metadata_df([
            {"author": "Hugo, Victor", "downloads": 12000, "id": 100},
            {"author": "Hugo, Victor", "downloads": 12000, "id": 101},
            {"author": "Hugo, Victor", "downloads": 12000, "id": 102},
            {"author": "Hugo, Victor", "downloads": 12000, "id": 103},
            {"author": "Hugo, Victor", "downloads": 12000, "id": 104},
            {"author": "Ganz, Hugo",   "downloads": 10,    "id": 200},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            r = er.resolve_author("Hugo")
        self.assertEqual(r.decision, "resolved",
                         f"Expected resolved, got {r.decision}. "
                         f"Trace: {r.normalization_trace}, candidates: "
                         f"{[c.display for c in r.candidates]}")
        self.assertEqual(r.resolved["display"], "Hugo, Victor")

    def test_clear_prominence_winner_high_confidence(self):
        fake_df = _mock_metadata_df([
            {"author": "Hugo, Victor", "downloads": 12000, "id": 100},
            {"author": "Hugo, Victor", "downloads": 12000, "id": 101},
            {"author": "Ganz, Hugo",   "downloads": 10,    "id": 200},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            r = er.resolve_author("Hugo")
        self.assertGreaterEqual(r.confidence, 0.7,
                                f"prominence ratio should yield ≥0.7 confidence; "
                                f"got {r.confidence} ({r.confidence_reason})")

    def test_ambiguous_authors_yield_clarify(self):
        """Two authors with similar fuzz AND similar prominence → clarify."""
        fake_df = _mock_metadata_df([
            # Two equally-prominent authors named "Smith"
            {"author": "Smith, John",  "downloads": 5000, "id": 100},
            {"author": "Smith, John",  "downloads": 5000, "id": 101},
            {"author": "Smith, Jane",  "downloads": 4800, "id": 200},
            {"author": "Smith, Jane",  "downloads": 4800, "id": 201},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            r = er.resolve_author("Smith")
        # Either resolved with low confidence, or clarify_needed
        self.assertIn(r.decision, {"resolved", "clarify_needed"})
        if r.decision == "resolved":
            self.assertLess(r.confidence, 0.9,
                            "Two equal-prominence Smiths shouldn't yield high confidence")


class RankAuthorCandidatesPureFn(unittest.TestCase):
    """Test the ranker in isolation (no metadata dep)."""

    def test_high_prominence_wins_over_low(self):
        cands = [
            er.Candidate(key="^Ganz,", display="Ganz, Hugo",
                         score=85, source="fuzzy", prominence=10),
            er.Candidate(key="^Hugo,", display="Hugo, Victor",
                         score=85, source="fuzzy", prominence=12000),
        ]
        ranked = er.rank_author_candidates(cands)
        self.assertEqual(ranked[0].display, "Hugo, Victor")

    def test_higher_fuzz_band_beats_higher_prominence(self):
        """If fuzz scores are in different bands, fuzz wins (we don't
        let an obscure exact match lose to a famous near-match)."""
        cands = [
            er.Candidate(key="^Exact,", display="Exact match",
                         score=98, source="fuzzy", prominence=10),
            er.Candidate(key="^Famous,", display="Famous near-match",
                         score=72, source="fuzzy", prominence=99999),
        ]
        ranked = er.rank_author_candidates(cands)
        self.assertEqual(ranked[0].display, "Exact match")


# =====================================================================
# Confidence math
# =====================================================================


class ConfidenceGap(unittest.TestCase):
    def test_alias_curated_is_one(self):
        c = er.Candidate(key="^Doyle,", display="Doyle", score=100,
                          source="alias_curated", prominence=50000)
        conf, _ = er.confidence_from_gap(c, None)
        self.assertEqual(conf, 1.0)

    def test_single_fuzzy_candidate_is_high(self):
        c = er.Candidate(key="^Doyle,", display="Doyle", score=92,
                          source="fuzzy", prominence=50000)
        conf, _ = er.confidence_from_gap(c, None)
        self.assertGreaterEqual(conf, 0.85)

    def test_close_pair_is_ambiguous(self):
        a = er.Candidate(key="^A,", display="A", score=85,
                          source="fuzzy", prominence=1000)
        b = er.Candidate(key="^B,", display="B", score=84,
                          source="fuzzy", prominence=900)
        conf, _ = er.confidence_from_gap(a, b)
        self.assertLess(conf, 0.7)

    def test_clear_fuzz_gap_is_high(self):
        a = er.Candidate(key="^A,", display="A", score=95,
                          source="fuzzy", prominence=1000)
        b = er.Candidate(key="^B,", display="B", score=70,
                          source="fuzzy", prominence=900)
        conf, _ = er.confidence_from_gap(a, b)
        self.assertGreaterEqual(conf, 0.85)


# =====================================================================
# resolve_author — end-to-end with mocked metadata
# =====================================================================


class ResolveAuthorE2E(unittest.TestCase):
    def setUp(self):
        with er._prom_lock:
            er._prom_state["data"] = None

    def test_curated_alias_doyle(self):
        """Curated alias path — no metadata needed."""
        r = er.resolve_author("Doyle")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["author_regex"], "^Doyle,")
        self.assertEqual(r.confidence, 1.0)

    def test_curated_alias_russian(self):
        """RU input via alias dict — «Достоевский» exact."""
        r = er.resolve_author("Достоевский")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["author_regex"], "^Dostoyevsky,")

    def test_homoglyph_dostoevsky_resolves(self):
        """B-R14-4: «Доcтоевский» with Latin c — through normalize +
        alias lookup, should resolve."""
        # NOTE: «Достоевский» (nom) is in alias as exact + stem 'достоевск'.
        # With homoglyph fold, Latin c → с, then alias lookup hits stem.
        r = er.resolve_author("Доcтоевский")
        # Either resolves cleanly, or — if alias is keyed on stem fragment —
        # falls through to fuzzy. Either way, NOT 'not_found'.
        self.assertNotEqual(r.decision, "not_found",
                            f"Expected resolved/clarify, got not_found. "
                            f"normalization_trace={r.normalization_trace}, "
                            f"q_norm={r.query_normalized}")
        if r.decision == "resolved":
            self.assertEqual(r.resolved["author_regex"], "^Dostoyevsky,")
        # Trace must include homoglyph fold
        self.assertTrue(
            any("homoglyph" in s.lower() for s in r.normalization_trace),
            f"Homoglyph trace missing: {r.normalization_trace}"
        )

    def test_russian_genitive_tolstogo_resolves(self):
        """B-R14-9: «Толстого» — RU lemmatize folds to «Толстой» → alias."""
        r = er.resolve_author("Толстого")
        self.assertEqual(r.decision, "resolved",
                         f"Expected resolved, got {r.decision} "
                         f"(trace={r.normalization_trace}, q={r.query_normalized})")
        self.assertEqual(r.resolved["author_regex"], "^Tolstoy,")
        # Trace must include the RU lemma step
        self.assertTrue(
            any("lemma" in s.lower() for s in r.normalization_trace),
            f"RU lemma trace missing: {r.normalization_trace}"
        )

    def test_empty_returns_not_found(self):
        r = er.resolve_author("")
        self.assertEqual(r.decision, "not_found")

    def test_unknown_author_returns_not_found(self):
        """Author not in alias and no corpus metadata → not_found."""
        fake_df = _mock_metadata_df([
            {"author": "Doyle, Arthur Conan", "downloads": 5000, "id": 1},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            r = er.resolve_author("Xyzfantasy")
        self.assertEqual(r.decision, "not_found")


# =====================================================================
# resolve_book — end-to-end
# =====================================================================


class ResolveBookE2E(unittest.TestCase):
    def test_known_book_pride_and_prejudice(self):
        r = er.resolve_book("Pride and Prejudice")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["pg_id"], "PG1342")
        self.assertEqual(r.resolved["title"], "Pride and Prejudice")
        self.assertEqual(r.confidence, 1.0)

    def test_russian_genitive_pride(self):
        # «Гордости и предубеждения» is in KNOWN_BOOKS via existing
        # declension coverage.
        r = er.resolve_book("Гордости и предубеждения")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["pg_id"], "PG1342")

    def test_brothers_karamazov_via_ru_title_alias(self):
        """B-R14-9 part 2: «Братьев Карамазовых» — gen case, was missing
        from KNOWN_BOOKS in R14 → not found. v5 adds it via RU title alias map."""
        r = er.resolve_book("Братьев Карамазовых")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["pg_id"], "PG28054")
        self.assertEqual(r.resolved["title"], "The Brothers Karamazov")
        self.assertGreaterEqual(r.confidence, 0.9)
        # Trace must show the alias step
        self.assertTrue(
            any("RU title" in s for s in r.normalization_trace),
            f"Trace: {r.normalization_trace}"
        )

    def test_brothers_karamazov_nominative_also_resolves(self):
        r = er.resolve_book("Братья Карамазовы")
        self.assertEqual(r.decision, "resolved")
        self.assertEqual(r.resolved["pg_id"], "PG28054")


# =====================================================================
# Prominence index thread-safety + caching
# =====================================================================


class ProminenceIndexCache(unittest.TestCase):
    def setUp(self):
        with er._prom_lock:
            er._prom_state["data"] = None

    def test_first_call_builds_second_call_cached(self):
        fake_df = _mock_metadata_df([
            {"author": "Doyle, Arthur Conan", "downloads": 5000, "id": 1},
            {"author": "Doyle, Arthur Conan", "downloads": 5000, "id": 2},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df) as m:
            idx1 = er.get_prominence_index()
            idx2 = er.get_prominence_index()
        # Both same dict instance
        self.assertIs(idx1, idx2)
        # _metadata_df was called only once
        self.assertEqual(m.call_count, 1)

    def test_force_reload_rebuilds(self):
        fake_df = _mock_metadata_df([
            {"author": "Doyle, Arthur Conan", "downloads": 5000, "id": 1},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df) as m:
            er.get_prominence_index()
            er.get_prominence_index(force_reload=True)
        self.assertEqual(m.call_count, 2)

    def test_aggregates_downloads_and_books(self):
        fake_df = _mock_metadata_df([
            {"author": "Doyle, Arthur Conan", "downloads": 5000, "id": 1},
            {"author": "Doyle, Arthur Conan", "downloads": 3000, "id": 2},
            {"author": "Doyle, Arthur Conan", "downloads": 1000, "id": 3},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            idx = er.get_prominence_index()
        prom = idx["^Doyle,"]
        self.assertEqual(prom["downloads"], 9000)
        self.assertEqual(prom["books"], 3)

    def test_handles_missing_downloads_column(self):
        fake_df = pd.DataFrame([
            {"author": "Doyle, Arthur Conan", "id": 1},
        ])
        with mock.patch("scripts.rag_tools._metadata_df", return_value=fake_df):
            idx = er.get_prominence_index()
        self.assertEqual(idx["^Doyle,"]["downloads"], 0)
        self.assertEqual(idx["^Doyle,"]["books"], 1)

    def test_handles_missing_metadata(self):
        with mock.patch("scripts.rag_tools._metadata_df", return_value=None):
            idx = er.get_prominence_index()
        self.assertEqual(idx, {})


# =====================================================================
# Integration with RequestTrace (P9 wiring sanity)
# =====================================================================


class TraceIntegration(unittest.TestCase):
    """ResolveResult should be easy to feed into RequestTrace."""

    def test_trace_accepts_resolve_result_via_add_entity_resolve(self):
        from scripts.v2 import observability as obs
        obs._reset()
        t = obs.start_trace("Толстого")
        r = er.resolve_author("Толстого")
        t.add_entity_resolve(
            entity_type="author",
            query=r.query_raw,
            decision=r.decision,
            resolved=(r.resolved.get("display") if r.resolved else None),
            confidence=r.confidence,
            candidates=[c.to_dict() for c in r.candidates],
            normalization_trace=r.normalization_trace,
        )
        flat = t.finalize()
        self.assertEqual(len(flat["v5_entity_resolves"]), 1)
        ev = flat["v5_entity_resolves"][0]
        self.assertEqual(ev["decision"], "resolved")
        self.assertEqual(ev["resolved"], "Tolstoy")
        self.assertIn("RU author lemma", " ".join(ev["normalization"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
