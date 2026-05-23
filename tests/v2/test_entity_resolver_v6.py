"""v6 Entity Resolver — comprehensive test suite.

Coverage matrix: 5 mention types × {positive resolve, negative no-clarify,
edge cases} + decision-threshold calibration + scoring components.

Critical: includes the NEGATIVE tests missing in v5/stage3.2 cascade —
«Christopher Marlowe» / «Конан Дойл» / «Doyle, Arthur Conan» / «H.G. Wells»
must resolve WITHOUT clarify. R-22 probe suite E13 motivation.

Test isolation: each test sets up isolated `_metadata_df` mock + resets
prominence index. No shared state between tests.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2 import entity_resolver as er
from scripts.v2.entity_resolver_v6 import (
    Decision,
    MentionType,
    decide,
    detect_mentions,
    generate_candidates,
    resolve_v6,
    score_candidates,
)


# =====================================================================
# Test fixtures — realistic prod-shape metadata
# =====================================================================

def _fixture_df() -> pd.DataFrame:
    """Reproduces real prod by_canonical Wells/Doyle/Marlowe/Tolstoy."""
    return pd.DataFrame([
        # Wells — many canonicals, dominant H.G.
        {"author": "Wells, H. G. (Herbert George)", "downloads": 39000, "id": 1},
        {"author": "Wells, H. G. (Herbert George)", "downloads": 1500, "id": 2},
        {"author": "Wells, Basil", "downloads": 4292, "id": 3},
        {"author": "Wells, Carolyn", "downloads": 500, "id": 4},
        {"author": "Wells, Hal K.", "downloads": 0, "id": 5},
        {"author": "Wells, J. (Joseph)", "downloads": 200, "id": 6},
        {"author": "Wells, Frederic DeWitt", "downloads": 50, "id": 7},
        # Marlowe — Christopher dominant
        {"author": "Marlowe, Christopher", "downloads": 12000, "id": 8},
        {"author": "Marlowe, Stephen", "downloads": 50, "id": 9},
        {"author": "Marlowe, Amy Bell", "downloads": 20, "id": 10},
        # Doyle — Arthur Conan dominant
        {"author": "Doyle, Arthur Conan", "downloads": 65000, "id": 11},
        {"author": "Doyle, Charles", "downloads": 200, "id": 12},
        {"author": "Doyle, William", "downloads": 50, "id": 13},
        # Tolstoy — Leo dominant (3 canonicals)
        {"author": "Tolstoy, Leo graf", "downloads": 35000, "id": 14},
        {"author": "Tolstoy, Lev Lvovich", "downloads": 500, "id": 15},
        {"author": "Tolstoy, Aleksey Konstantinovich graf", "downloads": 300, "id": 16},
        # Dostoyevsky — single canonical
        {"author": "Dostoyevsky, Fyodor", "downloads": 22000, "id": 17},
        # Wodehouse — single canonical
        {"author": "Wodehouse, P. G.", "downloads": 18000, "id": 18},
        # Hardy — Thomas dominant
        {"author": "Hardy, Thomas", "downloads": 12000, "id": 19},
        {"author": "Hardy, E. D.", "downloads": 0, "id": 20},
    ])


class _ResolverTestBase(unittest.TestCase):
    """Base class — sets up fresh metadata mock + prominence index per test."""

    def setUp(self):
        self._patcher = mock.patch(
            "scripts.rag_tools._metadata_df",
            return_value=_fixture_df(),
        )
        self._patcher.start()
        with er._prom_lock:
            er._prom_state["data"] = None
        # Force fresh prominence index build
        er.get_prominence_index(force_reload=True)

    def tearDown(self):
        self._patcher.stop()
        # Clear prominence cache so subsequent tests don't get our mock data
        with er._prom_lock:
            er._prom_state["data"] = None


# =====================================================================
# E13 closure — full_name / canonical_format must NOT clarify
# =====================================================================


class FullNameResolvesDirectly(_ResolverTestBase):
    """B-R17-1/E13 negative tests — when user provides first name, resolve.

    Each query MUST resolve to the specific canonical containing the
    first-name token. NO clarify path should fire.
    """

    def test_christopher_marlowe_resolves_to_christopher(self):
        d = resolve_v6("Christopher Marlowe")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Christopher", d.resolved.display)
        self.assertNotIn("Stephen", d.resolved.display)
        self.assertNotIn("Amy", d.resolved.display)

    def test_christopher_marlowe_in_sentence(self):
        d = resolve_v6("сколько книг написал Christopher Marlowe")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Christopher", d.resolved.display)

    def test_basil_wells_resolves_to_basil(self):
        d = resolve_v6("Basil Wells")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Basil", d.resolved.display)

    def test_carolyn_wells_resolves_to_carolyn(self):
        d = resolve_v6("Carolyn Wells")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Carolyn", d.resolved.display)

    def test_conan_doyle_full_name_resolves_to_arthur(self):
        # "Conan Doyle" is a multi-word alias OR full_name
        d = resolve_v6("Conan Doyle")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)


class CanonicalFormatResolvesDirectly(_ResolverTestBase):
    """When user writes «Surname, FirstName» (canonical-with-comma),
    that's already disambiguated — resolve directly."""

    def test_doyle_arthur_conan_resolves(self):
        d = resolve_v6("Doyle, Arthur Conan")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertEqual(d.resolved.display, "Doyle, Arthur Conan")

    def test_doyle_arthur_conan_in_sentence(self):
        d = resolve_v6("какие книги у Doyle, Arthur Conan")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertEqual(d.resolved.display, "Doyle, Arthur Conan")

    def test_wells_hg_canonical_form(self):
        d = resolve_v6("Wells, H. G.")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("H. G.", d.resolved.display)


class AliasHitResolvesDirectly(_ResolverTestBase):
    """Multi-word aliases (h. g. wells, конан дойл) point to specific
    canonical. Must resolve, not clarify."""

    def test_hg_wells_multi_word_alias(self):
        # AUTHOR_ALIASES has "h. g. wells" → "^Wells,"
        # Stage 2 finds all Wells; Stage 3 with extra=('h.', 'g.') boosts H.G.
        d = resolve_v6("H.G. Wells")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("H. G.", d.resolved.display)

    def test_конан_дойл_alias(self):
        d = resolve_v6("конан дойл")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)

    def test_конан_дойла_in_sentence(self):
        d = resolve_v6("фирменные слова Конан Дойла")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)


class RuStemResolvesDirectly(_ResolverTestBase):
    """RU declension of surnames — resolve to dominant canonical via
    high prominence weight."""

    def test_толстого_resolves_to_leo(self):
        d = resolve_v6("Толстого")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Leo", d.resolved.display)

    def test_у_толстого_in_sentence(self):
        d = resolve_v6("какие книги у Толстого")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Leo", d.resolved.display)

    def test_достоевский_resolves(self):
        d = resolve_v6("Достоевский")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Fyodor", d.resolved.display)


# =====================================================================
# Positive clarify — when no first-name signal AND multiple canonicals
# =====================================================================


class BareSurnameTriggerClarify(_ResolverTestBase):
    """When user types bare surname AND ≥2 canonicals share it AND no
    disambiguating tokens — clarify with list (R-22 main UX goal)."""

    def test_wells_bare_triggers_clarify(self):
        d = resolve_v6("Wells")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED)
        self.assertGreaterEqual(len(d.clarify_candidates), 2)
        # H.G. should be first (highest prominence)
        names = [c.display for c in d.clarify_candidates]
        self.assertTrue(any("H. G." in n for n in names),
                          "H.G. must appear in clarify list")

    def test_wells_in_query_still_clarifies(self):
        d = resolve_v6("какие книги у Wells")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED)


class SingleCanonicalResolves(_ResolverTestBase):
    """Surnames with only one canonical (Wodehouse) resolve, never clarify."""

    def test_wodehouse_single(self):
        d = resolve_v6("Wodehouse")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Wodehouse", d.resolved.display)


class SpecificAliasResolves(_ResolverTestBase):
    """Aliases that already map to a specific canonical (Hardy → Thomas
    via curated alias) should resolve via dominance — not clarify."""

    def test_hardy_resolves_to_thomas(self):
        d = resolve_v6("Hardy")
        self.assertEqual(d.decision, Decision.RESOLVED)
        # Thomas is dominant (12000 dl) over E.D. (0 dl)
        self.assertIn("Thomas", d.resolved.display)


# =====================================================================
# Mention detection unit tests
# =====================================================================


class MentionDetection(unittest.TestCase):
    """Stage 1 isolation — without metadata mock."""

    def test_canonical_format_simple(self):
        ms = detect_mentions("doyle, arthur conan")
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].type, MentionType.CANONICAL_FORMAT)
        self.assertEqual(ms[0].alias_key, "doyle")
        self.assertIn("arthur", ms[0].extra_tokens)

    def test_canonical_format_in_query(self):
        ms = detect_mentions("какие книги у doyle, arthur conan")
        # Should detect canonical_format, not split «у doyle» as surname
        canonical = [m for m in ms if m.type == MentionType.CANONICAL_FORMAT]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0].alias_key, "doyle")

    def test_full_name_detection(self):
        ms = detect_mentions("christopher marlowe")
        # alias_hit takes precedence over full_name since "christopher marlowe"
        # is a multi-word alias key
        self.assertEqual(ms[0].type, MentionType.ALIAS_HIT)
        self.assertEqual(ms[0].alias_key, "christopher marlowe")
        self.assertIn("christopher", ms[0].extra_tokens)

    def test_full_name_for_unaliased_first(self):
        # "basil wells" — "basil wells" is NOT a multi-word alias, but
        # "wells" is a single-word alias. So full_name detection wins.
        ms = detect_mentions("basil wells")
        self.assertTrue(any(m.type == MentionType.FULL_NAME for m in ms))
        fn = next(m for m in ms if m.type == MentionType.FULL_NAME)
        self.assertEqual(fn.alias_key, "wells")
        self.assertIn("basil", fn.extra_tokens)

    def test_surname_only_bare(self):
        ms = detect_mentions("wells")
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0].type, MentionType.SURNAME_ONLY)

    def test_ru_stem_cyrillic(self):
        # "толстой" is in alias dict (RU stem)
        ms = detect_mentions("толстой")
        self.assertEqual(ms[0].type, MentionType.RU_STEM)

    def test_no_mention_empty(self):
        self.assertEqual(detect_mentions(""), [])
        self.assertEqual(detect_mentions("   "), [])

    def test_no_mention_no_alias(self):
        # "xenomorph" is not in any alias dict — but the regex
        # might still detect canonical-format. Test with random tokens.
        ms = detect_mentions("hello world goodbye")
        self.assertEqual(ms, [])


# =====================================================================
# Decision threshold + scoring unit tests
# =====================================================================


class DecisionThresholds(_ResolverTestBase):
    """Verify the calibrated thresholds work for boundary cases."""

    def test_token_bypass_resolves_even_low_prominence(self):
        """Even if user picks Basil Wells (low prominence), token_overlap
        bypass should resolve."""
        d = resolve_v6("Basil Wells")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Basil", d.resolved.display)

    def test_canonical_format_resolves_with_low_prominence(self):
        d = resolve_v6("Wells, Carolyn")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Carolyn", d.resolved.display)


# =====================================================================
# Integration with v5 ResolveResult adapter
# =====================================================================


class V5BackwardsCompat(_ResolverTestBase):
    """Verify to_resolve_result() adapter returns v5-compatible shape."""

    def test_adapter_resolved(self):
        from scripts.v2.entity_resolver_v6 import resolve_v6
        from scripts.v2.entity_resolver_v6.main import to_resolve_result

        d = resolve_v6("Christopher Marlowe")
        rr = to_resolve_result(d, "Christopher Marlowe")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.decision, "resolved")
        self.assertIsNotNone(rr.resolved)
        self.assertIn("Christopher", rr.resolved["display"])

    def test_adapter_clarify(self):
        from scripts.v2.entity_resolver_v6.main import to_resolve_result

        d = resolve_v6("Wells")
        rr = to_resolve_result(d, "Wells")
        self.assertEqual(rr.decision, "clarify_needed")
        self.assertGreaterEqual(len(rr.candidates), 2)


# =====================================================================
# W-1 (2026-05-23) — over-eager disambiguation regression suite.
#
# These exercise the path entities.extract() actually uses
# (resolve_v6_for_alias), not just the top-level resolve_v6(). The W-1
# bug was that resolve_v6_for_alias didn't extract first-name tokens
# from a multi-word alias_key — so «конан дойл» (ALIAS_HIT to bare
# «^Doyle,» regex) lost the "конан" signal and fell through to
# CLARIFY despite Arthur Conan being 99% dominant.
# =====================================================================


class W1ResolveViaForAliasEntryPoint(_ResolverTestBase):
    """resolve_v6_for_alias — the path entities.extract() actually uses
    when v5 _find_authors has already matched a multi-word curated alias.
    """

    def test_konan_doyl_alias_key_resolves(self):
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias("Конан Дойл", "конан дойл", "^Doyle,")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)

    def test_konan_doyla_in_sentence_via_alias(self):
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias(
            "фирменные слова Конан Дойла",
            "конан дойл",
            "^Doyle,",
        )
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)

    def test_christopher_marlowe_via_alias_with_verb(self):
        # Original W-1 reproducer: "сколько книг написал Christopher
        # Marlowe" — verb "написал" is in NON_FIRST_NAME_TOKENS so
        # query-context extraction yielded no extras. The first-name
        # token must come from the alias_key itself.
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias(
            "сколько книг написал Christopher Marlowe",
            "christopher marlowe",
            "^Marlowe,",
        )
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Christopher", d.resolved.display)

    def test_canonical_format_suggested_back_resolves_first_try(self):
        # The clarify-loop case from the W-1 spec: after a clarify,
        # the user picks «Doyle, Arthur Conan» (the canonical form
        # the service suggested). It MUST resolve on the first try,
        # not bounce back to clarify.
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias("Doyle, Arthur Conan", "doyle", "^Doyle,")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertEqual(d.resolved.display, "Doyle, Arthur Conan")

    def test_bare_wells_still_clarifies(self):
        # Acceptance from spec: «какие книги у Wells» (no first name,
        # H.G. share <90%) MUST still clarify so user can pick.
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias("какие книги у Wells", "wells", "^Wells,")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED)


class W1ExtractExtraTokensFromAliasKey(unittest.TestCase):
    """Unit-level: multi-word alias_key carries first-name as an
    extra_token even when the surrounding query context provides none.
    """

    def test_konan_dojl_alias_yields_konan(self):
        from scripts.v2.entity_resolver_v6.main import _extract_extra_tokens
        extras = _extract_extra_tokens("конан дойл", "конан дойл")
        self.assertIn("конан", extras)

    def test_christopher_marlowe_alias_yields_christopher(self):
        from scripts.v2.entity_resolver_v6.main import _extract_extra_tokens
        extras = _extract_extra_tokens(
            "christopher marlowe", "christopher marlowe",
        )
        self.assertIn("christopher", extras)

    def test_hg_wells_alias_yields_initials(self):
        from scripts.v2.entity_resolver_v6.main import _extract_extra_tokens
        extras = _extract_extra_tokens("h. g. wells", "h. g. wells")
        # Both initials should land in extras (after dot-stripping)
        self.assertIn("h", extras)
        self.assertIn("g", extras)

    def test_single_word_alias_yields_nothing_when_isolated(self):
        from scripts.v2.entity_resolver_v6.main import _extract_extra_tokens
        # Plain "wells" — no surrounding context, no comma — empty.
        self.assertEqual(_extract_extra_tokens("wells", "wells"), ())

    def test_canonical_format_query_yields_following_tokens(self):
        # After-comma path still works alongside the alias-key path
        from scripts.v2.entity_resolver_v6.main import _extract_extra_tokens
        extras = _extract_extra_tokens("doyle, arthur conan", "doyle")
        self.assertIn("arthur", extras)
        self.assertIn("conan", extras)


class W1DominantHomonymRule(_ResolverTestBase):
    """Rule 4.45 — when one canonical accounts for ≥90% of total
    prominence (or ≥10× the runner-up), resolve to it regardless of
    mention type. Fixture: Doyle Arthur Conan = 65k vs Charles = 200
    vs William = 50 → 99.6% share, 325× ratio.
    """

    def test_dominant_doyle_resolves_for_alias_hit(self):
        from scripts.v2.entity_resolver_v6.main import resolve_v6_for_alias
        d = resolve_v6_for_alias("Конан Дойл", "конан дойл", "^Doyle,")
        self.assertEqual(d.decision, Decision.RESOLVED)
        # Either Rule 2 (token bypass) or Rule 4.45 (dominance) is fine
        self.assertIn("Arthur Conan", d.resolved.display)

    def test_non_dominant_wells_still_clarifies(self):
        # Wells fixture is by design 88.93% (just under 90%) and 9.4×
        # (just under 10×) — neither dominance threshold trips.
        d = resolve_v6("Wells")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED)


class W1TZAcceptance(_ResolverTestBase):
    """TZ tz_claude_code_fixes_2026-05-22.md §W-1 acceptance pins.

    Lines 49-52 of the TZ:
      - «сколько книг написал Christopher Marlowe» → direct
      - «фирменные слова Конан Дойла» → without clarify
      - «какие книги у Уэллса» → CLARIFY (имя не задано — сохранить)
      - suggested format «Doyle, Arthur Conan» → first-try resolve
    """

    def test_tz_marlowe_direct_in_sentence(self):
        d = resolve_v6("сколько книг написал Christopher Marlowe")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Christopher", d.resolved.display)

    def test_tz_konan_dojla_resolves_for_arthur_conan(self):
        d = resolve_v6("фирменные слова Конан Дойла")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Arthur Conan", d.resolved.display)

    def test_tz_uelsa_no_first_name_clarifies(self):
        # Critical regression: Wells H.G. dominance is 88.93% / 9.4×
        # — BELOW the new dominance thresholds (90% / 10×). When the
        # user typed only the Russian declension «Уэллса» with no
        # first name, the resolver MUST clarify per TZ acceptance.
        d = resolve_v6("какие книги у Уэллса")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED,
                          msg="W-1 TZ line 51: name not given → clarify")

    def test_tz_canonical_format_suggested_resolves_first_try(self):
        d = resolve_v6("фирменные слова Doyle, Arthur Conan")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertEqual(d.resolved.display, "Doyle, Arthur Conan")

    def test_tz_tolstoy_ru_stem_still_resolves_when_truly_dominant(self):
        # Sanity: tightening Rule 4.6 from 5× to 10× must not break
        # «у Толстого» (Leo at 70× over runners-up) — still resolves.
        d = resolve_v6("какие книги у Толстого")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIn("Leo", d.resolved.display)


if __name__ == "__main__":
    unittest.main(verbosity=2)
