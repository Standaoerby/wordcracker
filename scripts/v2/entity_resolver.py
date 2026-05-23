"""EntityResolver — compatibility re-export shim.

T1 (D-P1-6, 2026-05-23) — the implementation moved into
`scripts/v2/entity_resolver_v6/` (author resolution + shared primitives:
types, normalize, prominence, legacy fuzzy helpers) and into
`scripts/v2/book_resolver.py` (book resolution pipeline).

This module is now a thin re-export so existing test code that does
`from scripts.v2 import entity_resolver as er` continues to work without
churn. Internal modules — `entity_resolver_v6/*`, `book_resolver.py`,
`planner/entities.py`, `tools/meta/resolve_entity.py` — import from the
specific new modules directly; this shim is for the test surface only.

Nothing here is load-bearing; everything is `from X import Y`. New
resolver code should land in `entity_resolver_v6/*`, never here.

History:
  - Phase 0 (D-P0-1)         — WC_V6_RESOLVER gate removed; v6 default.
  - Phase 1 (D-P1-2)         — resolve_author shimmed to v6 + adapter.
  - Phase 1 T1 (D-P1-6)      — primitives moved into v6 package + book_resolver.
"""
from __future__ import annotations

from scripts.v2.book_resolver import (
    resolve_book,
    resolve_ru_book_alias,
)
from scripts.v2.entity_resolver_v6.legacy_fuzzy import (
    _candidates_from_alias,
    _candidates_from_corpus_fuzzy,
    _match_canonical_by_tokens,
    _regex_to_display,
    _simple_token_score,
    _specialize_surname_to_dominant,
    _try_rapidfuzz,
)
from scripts.v2.entity_resolver_v6.main import (
    resolve_author,
    resolve_v6,
    to_resolve_result,
)
from scripts.v2.entity_resolver_v6.normalize import (
    NormalizationResult,
    normalize_query,
    ru_lemmatize_author_query,
)
from scripts.v2.entity_resolver_v6.prominence import (
    _fuzz_band,
    _prom_lock,
    _prom_state,
    confidence_from_gap,
    get_prominence_index,
    prominence_for,
    prominence_for_canonical,
    rank_author_candidates,
)
from scripts.v2.entity_resolver_v6.types import (
    Candidate,
    ResolveDecision,
    ResolveResult,
)


# Module-level marker. Preserved for any caller that grepped for the
# legacy version string.
V5_ENTITY_RESOLVER_VERSION = "0.1"


__all__ = [
    "V5_ENTITY_RESOLVER_VERSION",
    # Types
    "NormalizationResult",
    "Candidate",
    "ResolveDecision",
    "ResolveResult",
    # Normalization
    "normalize_query",
    "ru_lemmatize_author_query",
    # Prominence + ranking
    "_prom_lock",
    "_prom_state",
    "get_prominence_index",
    "prominence_for",
    "prominence_for_canonical",
    "_fuzz_band",
    "rank_author_candidates",
    "confidence_from_gap",
    # Legacy v5 fuzzy helpers (test surface)
    "_try_rapidfuzz",
    "_simple_token_score",
    "_regex_to_display",
    "_match_canonical_by_tokens",
    "_specialize_surname_to_dominant",
    "_candidates_from_alias",
    "_candidates_from_corpus_fuzzy",
    # Book resolution
    "resolve_book",
    "resolve_ru_book_alias",
    # Author resolution + v6 entry
    "resolve_author",
    "resolve_v6",
    "to_resolve_result",
]
