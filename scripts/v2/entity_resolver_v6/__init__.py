"""v6 Entity Resolver — Layered Entity Linker.

Architecture per docs/v2/entity_resolver_v6.md + architecture_refactor_v6_plan
in Obsidian vault.

5 stages:
  1. Mention Detection — typed mentions (canonical_format, full_name, ...)
  2. Candidate Generation — KB lookup per mention type
  3. Multi-Factor Scoring — string sim, token overlap, prominence prior
  4. Decision Threshold — resolve / clarify_needed / not_found
  5. Session Memory — boost prior user choices (Phase 2)

Main entries:
  - `resolve_v6(query)` → ResolverDecision (typed)
  - `to_resolve_result(decision, query)` → ResolveResult (v5-style adapter)
  - `resolve_author(query)` → ResolveResult (convenience: both calls)

T1 (D-P1-6, 2026-05-23) — shared primitives (Candidate, ResolveResult,
normalize_query, prominence helpers, v5 fuzzy helpers) moved into this
package. The old `scripts.v2.entity_resolver` module is now a thin
re-export shim.
"""
from __future__ import annotations

from scripts.v2.entity_resolver_v6.candidates import generate_candidates
from scripts.v2.entity_resolver_v6.decision import decide
from scripts.v2.entity_resolver_v6.main import (
    resolve_author,
    resolve_v6,
    to_resolve_result,
)
from scripts.v2.entity_resolver_v6.mentions import detect_mentions
from scripts.v2.entity_resolver_v6.scoring import score_candidates
from scripts.v2.entity_resolver_v6.types import (
    Candidate,
    CandidateScore,
    Decision,
    Mention,
    MentionType,
    ResolveDecision,
    ResolveResult,
    ResolverDecision,
)

__all__ = [
    # Mention detection
    "Mention", "MentionType", "detect_mentions",
    # Candidates
    "Candidate", "generate_candidates",
    # Scoring
    "CandidateScore", "score_candidates",
    # Decision
    "Decision", "ResolverDecision", "decide",
    # v5-style adapter shape
    "ResolveDecision", "ResolveResult",
    # Entry points
    "resolve_v6", "to_resolve_result", "resolve_author",
]
