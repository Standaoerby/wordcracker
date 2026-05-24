"""v6 Stage 4 — Decision Threshold.

Apply calibrated thresholds to scored candidates → resolved / clarify / not_found.

Key insight (E13 lesson): when user provides disambiguating tokens that match
a candidate strongly (`token_overlap >= TOKEN_BYPASS`), resolve directly
regardless of prominence ratio. That's the user's explicit signal.

Canonical-format rule: when mention.type == CANONICAL_FORMAT and there's a
plausible match, resolve. User wrote the exact form — don't ask back.

Otherwise use combined-score + top1/top2 ratio thresholds.

Thresholds tuned via probe-suite calibration — see SESSION_2026-05-22 §4.4.
"""
from __future__ import annotations

from scripts.v2.entity_resolver_v6.types import (
    CandidateScore,
    Decision,
    Mention,
    MentionType,
    ResolverDecision,
)


# ----- Calibrated thresholds -----

# Top-1 combined score must be ≥ this to resolve (default path)
RESOLVE_THRESHOLD = 0.55

# Top-1 / Top-2 ratio must be ≥ this to resolve (default path)
RATIO_THRESHOLD = 1.5

# token_overlap ≥ this → bypass ratio check, resolve directly
TOKEN_BYPASS = 0.5

# string_sim ≥ this for CANONICAL_FORMAT to resolve directly
CANONICAL_STRING_FLOOR = 0.5

# Top-1 combined < this → not_found
CLARIFY_FLOOR = 0.20

# How many candidates to show in clarify list
CLARIFY_LIST_SIZE = 5

# Dominant-homonym (W-1): when one canonical accounts for ≥ this share
# of total prominence (or books), it's the obvious referent. Resolves
# «Конан Дойл» → Doyle, Arthur Conan even when cross-alphabet string_sim
# is zero. Bare SURNAME_ONLY without first-name context still clarifies
# below this threshold (Wells at 87% → clarify, Doyle at 99% → resolve).
DOMINANCE_SHARE = 0.90

# Top-1 / runner-up prominence ratio above which we also call it
# dominant — guards against the case where total prominence is small
# and percentages flatten out (e.g. {12000, 50, 20} → top share 99%
# anyway, but {100, 5, 5} → top share 91% with a 20x runner gap; both
# resolve).
DOMINANCE_RATIO = 10.0


def decide(
    mention: Mention,
    scored: list[CandidateScore],
) -> ResolverDecision:
    """Apply thresholds; return ResolverDecision.

    Decision priority (first matching rule wins):
      1.  Empty candidates → NOT_FOUND
      1.5 First-name filter (W-1 v2): when user supplied disambiguating
          tokens and mention isn't bare SURNAME_ONLY, restrict candidates
          to those whose canonical name actually contains the extras.
          One matcher → RESOLVED. Several matchers → continue with only
          matchers. No matcher → fall through unchanged.
      2.  token_overlap ≥ TOKEN_BYPASS → RESOLVED (user disambiguated)
      3.  CANONICAL_FORMAT + (extras OR string_sim ≥ floor) → RESOLVED
      4.  Single candidate → RESOLVED
      4.45 Dominant homonym (≥90% share or ≥10× ratio) → RESOLVED
      4.5 SURNAME_ONLY w/ multiple cands → CLARIFY_NEEDED
      4.6 RU_STEM with dominant prominence → RESOLVED
      5.  combined < CLARIFY_FLOOR → NOT_FOUND
      6.  combined ≥ RESOLVE_THRESHOLD AND ratio ≥ RATIO_THRESHOLD → RESOLVED
      7.  Otherwise → CLARIFY_NEEDED with top 5
    """
    if not scored:
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            confidence=0.0,
            reason="no candidates generated",
            mention=mention,
            all_scores=[],
        )

    # Rule 1.5 — First-name filter (W-1 v2, 2026-05-24).
    # When user provided disambiguating tokens (mention.extra_tokens)
    # AND mention is not a bare SURNAME_ONLY, RESTRICT candidates to
    # those that actually contain at least one extra. This is an
    # explicit filter (not just up-weight) per TZ §W-1 direction (1):
    # «резолв учитывает заданное имя/инициалы и фильтрует кандидатов».
    #
    # SURNAME_ONLY is excluded because by definition it has no extras
    # — leave it to Rule 4.5 (UX clarify on bare surname).
    #
    # Behaviour:
    #   - exactly 1 matcher → RESOLVED (the user uniquely identified
    #     one canonical), even when string_sim is low (cross-alphabet)
    #   - 2+ matchers      → continue with matchers as `scored`, so
    #                        every downstream rule (Rule 2 bypass, Rule
    #                        4.45 dominance, Rule 7 clarify) operates
    #                        on the relevant subset; clarify list will
    #                        no longer include candidates the user
    #                        explicitly excluded by giving a first name
    #   - 0 matchers       → fall through unchanged (corpus may not
    #                        contain the named author; let downstream
    #                        decide clarify vs not_found)
    filter_applied = False
    if (mention.extra_tokens
            and mention.type != MentionType.SURNAME_ONLY):
        matchers = [s for s in scored if s.token_overlap > 0]
        if len(matchers) == 1:
            top_m = matchers[0]
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top_m.candidate,
                confidence=top_m.combined,
                reason=(
                    f"first-name filter unique match "
                    f"extras={list(mention.extra_tokens)} "
                    f"(combined={top_m.combined:.2f})"
                ),
                mention=mention,
                all_scores=scored,
            )
        if len(matchers) >= 2:
            scored = matchers
            filter_applied = True

    top = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    # Rule 2 — token_overlap bypass
    if top.token_overlap >= TOKEN_BYPASS:
        return ResolverDecision(
            decision=Decision.RESOLVED,
            resolved=top.candidate,
            confidence=top.combined,
            reason=f"token_overlap={top.token_overlap:.2f} bypass "
                   f"(user gave disambiguating tokens)",
            mention=mention,
            all_scores=scored,
        )

    # Rule 3 — CANONICAL_FORMAT direct resolve.
    # User wrote «Surname, FirstName» — the canonical form is the
    # strongest possible signal. Resolve when EITHER:
    #   (a) string_sim ≥ floor (same-alphabet match), or
    #   (b) extras present AND top.token_overlap > 0 (cross-alphabet
    #       transliteration covers the gap, e.g. «Дойл, Артур Конан»
    #       where Cyrillic surface tokens never Jaccard-match Latin
    #       canonicals but the translit map lifts token_overlap).
    # Per TZ §W-1 direction (3): «формат точное совпадение и НЕ
    # вызывает повторную дизамбигуацию».
    if mention.type == MentionType.CANONICAL_FORMAT:
        if top.string_sim >= CANONICAL_STRING_FLOOR:
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=f"canonical_format string_sim={top.string_sim:.2f} "
                       f"(user wrote canonical form)",
                mention=mention,
                all_scores=scored,
            )
        if mention.extra_tokens and top.token_overlap > 0:
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=(
                    f"canonical_format with extras="
                    f"{list(mention.extra_tokens)} "
                    f"(token_overlap={top.token_overlap:.2f}, "
                    f"string_sim={top.string_sim:.2f} translit gap)"
                ),
                mention=mention,
                all_scores=scored,
            )

    # Rule 4 — single candidate
    if runner_up is None:
        return ResolverDecision(
            decision=Decision.RESOLVED,
            resolved=top.candidate,
            confidence=top.combined,
            reason="single candidate",
            mention=mention,
            all_scores=scored,
        )

    # Rule 4.45 — Dominant homonym (W-1, 2026-05-23).
    # When user provided a real first-name signal that scored a partial
    # token match (0 < top.token_overlap < TOKEN_BYPASS) AND one
    # canonical overwhelmingly dominates (≥90% of total prominence OR
    # ≥10× the runner-up), resolve. Fires only when:
    #   1. mention type is not SURNAME_ONLY (R-22 UX: ask on bare),
    #   2. top.token_overlap > 0 (user's hint actually matched something
    #      — guards against noise tokens like "биография"/"writings of"
    #      being mis-classified as FULL_NAME first-name context),
    #   3. one canonical dominates the group.
    #
    # RU_STEM is also excluded — it has its own 5× ratio rule (4.6)
    # below and partial-translit matches can stick on the wrong
    # canonical without enough corroborating signal.
    if (len(scored) >= 2
            and mention.type not in (MentionType.SURNAME_ONLY,
                                      MentionType.RU_STEM)
            and top.token_overlap > 0):
        total_prom = sum(max(s.candidate.prominence, 0) for s in scored)
        top_prom = max(top.candidate.prominence, 0)
        runner_prom = max(runner_up.candidate.prominence, 0)
        top_share = top_prom / total_prom if total_prom > 0 else 0.0
        prom_ratio = (top_prom / runner_prom) if runner_prom > 0 else (
            999.0 if top_prom > 0 else 0.0
        )
        if (top_share >= DOMINANCE_SHARE
                or prom_ratio >= DOMINANCE_RATIO) and top_prom > 0:
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=(
                    f"dominant homonym share={top_share:.2f} "
                    f"ratio={prom_ratio:.1f}x"
                ),
                mention=mention,
                all_scores=scored,
            )

    # Rule 4.5 — UX preference: bare SURNAME_ONLY with multiple
    # candidates → CLARIFY, regardless of prominence dominance.
    # User typed only a surname — give them the choice.
    # (Stan R-22 UX: «Wells» should show 5 Wells options, not
    # auto-pick H.G.)
    if mention.type == MentionType.SURNAME_ONLY and len(scored) >= 2:
        return ResolverDecision(
            decision=Decision.CLARIFY_NEEDED,
            resolved=None,
            clarify_candidates=[s.candidate for s in scored[:CLARIFY_LIST_SIZE]],
            confidence=top.combined,
            reason=f"bare surname with {len(scored)} canonicals — clarify",
            mention=mention,
            all_scores=scored,
        )

    # Rule 4.6 — RU_STEM with dominant prominence: resolve to dominant.
    # User wrote «Толстого» (RU genitive of Tolstoy) — refers to the
    # canonical Russian referent (Leo Tolstoy at 70× over runners-up).
    # Threshold aligned with W-1 spec (≥10× ratio or ≥90% share) — 5×
    # was too eager and made «у Уэллса» (Wells H.G. 9.4× over Basil)
    # resolve instead of clarify, contradicting the spec acceptance
    # «имя не задано → корректно дизамбигуирует».
    if mention.type == MentionType.RU_STEM and len(scored) >= 2:
        total_raw = sum(max(s.candidate.prominence, 0) for s in scored)
        top_raw = max(top.candidate.prominence, 0)
        runner_raw = max(runner_up.candidate.prominence, 0)
        top_share = top_raw / total_raw if total_raw > 0 else 0.0
        prom_ratio = (top_raw / runner_raw) if runner_raw > 0 else (
            999.0 if top_raw > 0 else 0.0
        )
        if (top_share >= DOMINANCE_SHARE
                or prom_ratio >= DOMINANCE_RATIO) and top_raw > 0:
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=(
                    f"ru_stem dominant prominence share={top_share:.2f} "
                    f"ratio={prom_ratio:.1f}x ({top_raw}/{runner_raw})"
                ),
                mention=mention,
                all_scores=scored,
            )

    # Rule 4.7 — Implicit CANONICAL_FORMAT detected in query text.
    # When v5 _find_authors extracts a single-token alias («doyle»)
    # but the original query had «Doyle, Arthur Conan» — that's
    # already user-disambiguated. Detect by checking if Stage 2
    # candidates include one whose canonical name matches a prefix
    # the query also contains.
    if len(scored) >= 2:
        # Look at extra_tokens — if any candidate has a name token
        # that uniquely identifies it AND that token appears in query
        # context, resolve.
        # This handles «Doyle, Arthur Conan» where extra_tokens may
        # contain «arthur» from canonical_format extraction context.
        # If extra_tokens are present AND top has full token_overlap,
        # we already handled in Rule 2. Here check for canonical-format
        # signal via runner_up gap on token_overlap.
        if (top.token_overlap > 0 and runner_up.token_overlap == 0
                and top.token_overlap >= 0.3):
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=f"top has unique token_overlap "
                       f"({top.token_overlap:.2f} vs runner 0.0)",
                mention=mention,
                all_scores=scored,
            )

    # Rule 5 — below floor → not_found
    if top.combined < CLARIFY_FLOOR:
        return ResolverDecision(
            decision=Decision.NOT_FOUND,
            resolved=None,
            confidence=top.combined,
            reason=f"top combined {top.combined:.2f} below floor "
                   f"{CLARIFY_FLOOR}",
            mention=mention,
            all_scores=scored,
        )

    # Rule 6 — clear winner by threshold + ratio
    ratio = (top.combined / runner_up.combined
             if runner_up.combined > 0 else 999.0)
    if top.combined >= RESOLVE_THRESHOLD and ratio >= RATIO_THRESHOLD:
        return ResolverDecision(
            decision=Decision.RESOLVED,
            resolved=top.candidate,
            confidence=top.combined,
            reason=f"clear winner combined={top.combined:.2f} ratio={ratio:.1f}",
            mention=mention,
            all_scores=scored,
        )

    # Rule 7 — ambiguous → clarify
    return ResolverDecision(
        decision=Decision.CLARIFY_NEEDED,
        resolved=None,
        clarify_candidates=[s.candidate for s in scored[:CLARIFY_LIST_SIZE]],
        confidence=top.combined,
        reason=f"ambiguous: top={top.combined:.2f} runner-up={runner_up.combined:.2f} "
               f"ratio={ratio:.1f}",
        mention=mention,
        all_scores=scored,
    )
