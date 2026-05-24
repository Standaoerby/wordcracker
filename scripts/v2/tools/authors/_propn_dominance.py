"""Generic PROPN-by-affinity dominance filter (W-4, 2026-05-24).

Stan ТЗ tz_claude_code_fixes_2026-05-22.md §W-4:
    «(2) усилить PROPN-фильтр — редкие имена собственные с высокой
        author-affinity не доминируют»

Curated blocklists (`_surname_filter.py`, `_toponym_filter.py`) cover the
KNOWN leaks. But there are always long-tail proper nouns we haven't named
yet — minor Dickens characters, obscure place names, secondary mythological
figures. The chase is endless if it stays purely curated.

This module adds a SIGNAL-based heuristic that catches the *shape* of a
rare proper noun without needing to know its identity:

    a token is a likely PROPN dominator iff
        affinity         >= AFFINITY_THRESHOLD (default 80)
        corpus_count     <= CORPUS_RARITY_CAP   (default 200)
        author/corpus    >= EXCLUSIVITY_RATIO   (default 0.5)

Reasoning
---------
Character names / minor place names share three properties:
    (a) the author uses them disproportionately often → high `affinity`
    (b) almost no one else uses them → corpus_count is small
    (c) of the few corpus occurrences, most come from THIS author →
        author_count/corpus_count is close to 1

Real stylistic markers («blighter», «indubitably», «cheerily») have
LARGE corpus_count (thousands across the corpus) because plenty of
authors use them — the exclusivity ratio drops below 0.1 and the
rarity cap protects them.

Trade-off
---------
Conservatism vs coverage: tightening either threshold drops more noise
but risks dropping a legitimately unique stylistic word (e.g., a hapax-
only neologism). The defaults err toward keeping (high thresholds), and
are tuned against the observed Doyle / Dickens / Wodehouse cases:

    «burger»     affinity=142.0  corpus=60   author=50  → drop  (toponym)
    «wegg»       affinity=95.0   corpus=32   author=30  → drop  (Dickens)
    «challenger» affinity=122.2  corpus=2245 author=385 → KEEP*
    «blighter»   affinity=38.5   corpus=1430 author=65  → keep
    «magnificent» affinity=12.0  corpus=800  author=80  → keep

    * «challenger» has corpus_count=2245 (above the 200 cap because the
      word is also a common adjective). It survives this generic filter
      but is still caught by the curated character-surname blocklist
      (`_CURATED_CHARACTER_SURNAMES`) — defence in depth.

Composability
-------------
Mirrors `filter_surnames` / `filter_toponyms` / `filter_corpus_artifacts`
API: takes a list of row-dicts, returns (kept_rows, dropped_count). Chain
in the same way the other filters do.
"""
from __future__ import annotations

from typing import Tuple

# Defaults tuned against Stan 2026-05-22 cases. See module docstring for
# the observed-value matrix.
DEFAULT_AFFINITY_THRESHOLD = 80.0
DEFAULT_CORPUS_RARITY_CAP = 200
DEFAULT_EXCLUSIVITY_RATIO = 0.5
# Floor on absolute author_count so we don't drop a one-occurrence noise
# token (it's already filtered by other layers; we only act on tokens
# the author meaningfully uses).
DEFAULT_MIN_AUTHOR_COUNT = 5


def is_propn_dominator(row: dict, *,
                       affinity_threshold: float = DEFAULT_AFFINITY_THRESHOLD,
                       corpus_rarity_cap: int = DEFAULT_CORPUS_RARITY_CAP,
                       exclusivity_ratio: float = DEFAULT_EXCLUSIVITY_RATIO,
                       min_author_count: int = DEFAULT_MIN_AUTHOR_COUNT,
                       ) -> bool:
    """True iff this row's shape matches the rare-PROPN-dominator profile.

    A missing or non-numeric `affinity` / `author_count` / `corpus_count`
    means we can't make a call → return False (keep the row).
    """
    if not isinstance(row, dict):
        return False
    affinity = row.get("affinity")
    author_count = row.get("author_count")
    corpus_count = row.get("corpus_count")
    if not isinstance(affinity, (int, float)):
        return False
    if not isinstance(author_count, (int, float)):
        return False
    if not isinstance(corpus_count, (int, float)) or corpus_count <= 0:
        return False
    if affinity < affinity_threshold:
        return False
    if corpus_count > corpus_rarity_cap:
        return False
    if author_count < min_author_count:
        return False
    if (author_count / corpus_count) < exclusivity_ratio:
        return False
    return True


def filter_propn_dominance(rows: list[dict], *,
                           affinity_threshold: float = DEFAULT_AFFINITY_THRESHOLD,
                           corpus_rarity_cap: int = DEFAULT_CORPUS_RARITY_CAP,
                           exclusivity_ratio: float = DEFAULT_EXCLUSIVITY_RATIO,
                           min_author_count: int = DEFAULT_MIN_AUTHOR_COUNT,
                           ) -> Tuple[list[dict], int]:
    """Drop rows that look like rare-PROPN dominators (character names,
    minor place names) per the docstring heuristic.

    Returns (filtered_rows, dropped_count). API parity with the other
    `_*_filter.py` modules so wrappers chain identically.
    """
    if not rows:
        return rows, 0
    kept = [r for r in rows
            if not is_propn_dominator(
                r,
                affinity_threshold=affinity_threshold,
                corpus_rarity_cap=corpus_rarity_cap,
                exclusivity_ratio=exclusivity_ratio,
                min_author_count=min_author_count,
            )]
    return kept, len(rows) - len(kept)


__all__ = [
    "DEFAULT_AFFINITY_THRESHOLD",
    "DEFAULT_CORPUS_RARITY_CAP",
    "DEFAULT_EXCLUSIVITY_RATIO",
    "DEFAULT_MIN_AUTHOR_COUNT",
    "filter_propn_dominance",
    "is_propn_dominator",
]
