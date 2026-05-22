"""v6 entity resolver — typed data classes.

Three core types:
  * Mention — a detected entity reference with surface form + type + context
  * CandidateScore — a KB candidate with multi-factor score breakdown
  * ResolverDecision — final output: resolved / clarify / not_found

These are FROZEN dataclasses where possible — once a Mention is detected,
its surface form / type / span / extra_tokens are immutable. CandidateScore
is built fresh during scoring (not mutated). ResolverDecision aggregates
everything for trace.

The v5 `Candidate` (from scripts.v2.entity_resolver) is reused — we don't
duplicate the KB row shape, only add typed wrappers around it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.v2.entity_resolver import Candidate


class MentionType(str, Enum):
    """How the user referred to the entity.

    Detection precedence (highest wins overlapping spans):
      CANONICAL_FORMAT > ALIAS_HIT > FULL_NAME > RU_STEM > SURNAME_ONLY
    """

    # "Doyle, Arthur Conan" — explicit canonical form with comma
    CANONICAL_FORMAT = "canonical_format"

    # "Christopher Marlowe", "Конан Дойл" — first + surname tokens
    FULL_NAME = "full_name"

    # "h. g. wells", "конан дойл" — multi-word alias in AUTHOR_ALIASES
    # (lowercased, hit on the FULL key, not via surname fallback)
    ALIAS_HIT = "alias_hit"

    # "Толстого", "Достоевского" — Russian declension of surname-only
    RU_STEM = "ru_stem"

    # "Wells", "Doyle" — bare surname (alias hit on single token)
    SURNAME_ONLY = "surname_only"


@dataclass(frozen=True)
class Mention:
    """A detected entity mention.

    Fields:
      surface       — exact substring user typed (preserved case for trace)
      type          — classification (see MentionType)
      span          — (start, end) offsets in NORMALIZED query
      alias_key     — key used in AUTHOR_ALIASES to get regex (lowercase)
      regex         — surname regex from alias lookup, e.g. "^Wells,"
      extra_tokens  — tokens user provided BEYOND the surname/alias key
                      (lowercased). Used for token-overlap scoring.
                      Example: "Christopher Marlowe" → ("christopher",)
                      Example: "Конан Дойл" → ("конан",)
                      Example: "Wells" → ()
    """

    surface: str
    type: MentionType
    span: tuple[int, int]
    alias_key: str
    regex: str
    extra_tokens: tuple[str, ...] = ()


@dataclass
class CandidateScore:
    """A scored candidate from Stage 3 multi-factor scoring.

    Score components (each in [0, 1]):
      string_sim       — Jaccard token overlap between mention.surface
                         and candidate.display (lowercased)
      token_overlap    — fraction of mention.extra_tokens that appear
                         (substring) in candidate.display.lower()
      prominence_prior — log-normalized downloads relative to max
                         candidate in this group

    combined is the weighted sum (weights depend on mention.type).
    """

    candidate: "Candidate"
    string_sim: float
    token_overlap: float
    prominence_prior: float
    combined: float


class Decision(str, Enum):
    RESOLVED = "resolved"
    CLARIFY_NEEDED = "clarify_needed"
    NOT_FOUND = "not_found"


@dataclass
class ResolverDecision:
    """Final output of the v6 pipeline.

    Fields:
      decision           — resolved / clarify_needed / not_found
      resolved           — the single Candidate when decision=resolved
      clarify_candidates — top 5 candidates (sorted by combined score)
                           when decision=clarify_needed
      confidence         — combined score of top-1 (or 0.0 if not_found)
      reason             — human-readable explanation for trace
      mention            — the primary Mention that drove the decision
      all_scores         — full scored list for trace (debug/JSONL)
    """

    decision: Decision
    resolved: "Candidate | None"
    clarify_candidates: list["Candidate"] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    mention: "Mention | None" = None
    all_scores: list[CandidateScore] = field(default_factory=list)
