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


def decide(
    mention: Mention,
    scored: list[CandidateScore],
) -> ResolverDecision:
    """Apply thresholds; return ResolverDecision.

    Decision priority (first matching rule wins):
      1. Empty candidates → NOT_FOUND
      2. token_overlap ≥ TOKEN_BYPASS → RESOLVED (user disambiguated)
      3. CANONICAL_FORMAT + string_sim ≥ floor → RESOLVED
      4. Single candidate → RESOLVED
      5. combined < CLARIFY_FLOOR → NOT_FOUND
      6. combined ≥ RESOLVE_THRESHOLD AND ratio ≥ RATIO_THRESHOLD → RESOLVED
      7. Otherwise → CLARIFY_NEEDED with top 5
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

    # Rule 3 — CANONICAL_FORMAT direct resolve
    if (mention.type == MentionType.CANONICAL_FORMAT
            and top.string_sim >= CANONICAL_STRING_FLOOR):
        return ResolverDecision(
            decision=Decision.RESOLVED,
            resolved=top.candidate,
            confidence=top.combined,
            reason=f"canonical_format string_sim={top.string_sim:.2f} "
                   f"(user wrote canonical form)",
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
    # User wrote «Уэллса» (RU genitive of Wells) which refers to the
    # canonical Russian referent (H.G. Wells), not the obscure ones.
    # Use raw prominence ratio (not log-normalized) — 5× threshold.
    if mention.type == MentionType.RU_STEM and len(scored) >= 2:
        top_raw = top.candidate.prominence or 0
        runner_raw = runner_up.candidate.prominence or 0
        if runner_raw == 0 or (top_raw / max(runner_raw, 1)) >= 5.0:
            return ResolverDecision(
                decision=Decision.RESOLVED,
                resolved=top.candidate,
                confidence=top.combined,
                reason=f"ru_stem dominant prominence {top_raw}/{runner_raw}",
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
