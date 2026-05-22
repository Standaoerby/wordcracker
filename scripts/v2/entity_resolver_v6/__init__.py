"""v6 Entity Resolver — Layered Entity Linker.

Architecture per docs/v2/entity_resolver_v6.md + architecture_refactor_v6_plan
in Obsidian vault.

5 stages:
  1. Mention Detection — typed mentions (canonical_format, full_name, ...)
  2. Candidate Generation — KB lookup per mention type
  3. Multi-Factor Scoring — string sim, token overlap, prominence prior
  4. Decision Threshold — resolve / clarify_needed / not_found
  5. Session Memory — boost prior user choices (Phase 2)

Main entry: `resolve_v6(query: str, ...) -> ResolverDecision`.

Backwards-compat: v5 `resolve_author(query)` delegates to v6 when
WC_V6_RESOLVER=on. v6 returns the same `ResolveResult` shape via
`to_resolve_result()` adapter so downstream code (rag_v2, plan, view)
doesn't see the new typed objects.

Triggered by R-22 probe suite — E13 (over-eager disambiguation) +
E1 baseline preservation. See SESSION_2026-05-22_systematic_analysis.
"""
from __future__ import annotations

from scripts.v2.entity_resolver_v6.types import (
    Mention,
    MentionType,
    CandidateScore,
    Decision,
    ResolverDecision,
)
from scripts.v2.entity_resolver_v6.mentions import detect_mentions
from scripts.v2.entity_resolver_v6.candidates import generate_candidates
from scripts.v2.entity_resolver_v6.scoring import score_candidates
from scripts.v2.entity_resolver_v6.decision import decide
from scripts.v2.entity_resolver_v6.main import resolve_v6

__all__ = [
    "Mention", "MentionType", "CandidateScore",
    "Decision", "ResolverDecision",
    "detect_mentions", "generate_candidates",
    "score_candidates", "decide", "resolve_v6",
]
