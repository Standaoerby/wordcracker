"""v6 entity resolver — prominence index + ranker + confidence math.

Moved from `scripts.v2.entity_resolver` (T1 / D-P1-6). Pure move — same
behaviour the v5 file had. v6 imports from here; the legacy entry point
`scripts.v2.entity_resolver` re-exports these names for tests that still
go through `from scripts.v2 import entity_resolver as er`.

Public surface used outside this package:
  - `get_prominence_index()`           — thread-safe two-level index
  - `prominence_for(author_regex)`      — surname-aggregate lookup
  - `prominence_for_canonical(name)`    — per-canonical lookup
  - `rank_author_candidates(cands)`     — sort by (band, prom, books)
  - `confidence_from_gap(top, runner)`  — 0..1 + reason
  - `_fuzz_band(score)`                 — score → discrete band (internal,
                                          but tests reach for it)
  - `_prom_lock`, `_prom_state`         — exposed so test setUps can reset
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.v2.entity_resolver_v6.types import Candidate

log = logging.getLogger("wordcracker.v2.entity_resolver_v6.prominence")


# Lazy-loaded {author_regex: {downloads_sum, books_count}}. Built once
# from `_metadata_df` on first use, mtime-cached.
_prom_lock = threading.Lock()
_prom_state: dict = {
    "data": None,             # dict | None
    "mtime": 0.0,
    "build_ts": 0.0,
}


def _build_prominence_index() -> dict:
    """Aggregate downloads + book counts at TWO granularities.

    Returns:
        {
          "by_surname":   {"^Doyle,": {"downloads": …, "books": …}, …},
          "by_canonical": {"Doyle, Arthur Conan": {"downloads": …, "books": …}, …},
        }

    B-R17-1 stage3.2 fix (2026-05-21): previously the index was keyed
    ONLY by surname regex, so every "Wells, X" author got the same
    aggregated value. When `_candidates_from_corpus_fuzzy` produced 5
    candidates, they all received identical 48,208 downloads and ranking
    by prominence was a no-op. Two-level indexing fixes that.
    """
    try:
        from scripts.rag_tools import _metadata_df
        df = _metadata_df()
    except Exception as e:
        log.warning("prominence index: _metadata_df unavailable: %s", e)
        return {"by_surname": {}, "by_canonical": {}}
    if df is None or "author" not in df.columns:
        return {"by_surname": {}, "by_canonical": {}}

    has_downloads = "downloads" in df.columns

    by_surname: dict[str, dict] = {}
    by_canonical: dict[str, dict] = {}
    # Defensive: _metadata_df concatenates multiple sources (SPGC +
    # user_uploads + orphan_pg). When source dtypes differ (object vs
    # float64), pandas .astype(str) may not coerce NaN — a value can
    # arrive here as float('nan'). Cast + skip.
    authors_raw = df["author"].tolist()
    downloads_raw = (df["downloads"].fillna(0).tolist()
                     if has_downloads else [0] * len(authors_raw))
    for a_raw, dl_raw in zip(authors_raw, downloads_raw):
        if not isinstance(a_raw, str):
            continue
        a = a_raw.strip()
        if not a or a.lower() in ("nan", "none", "<na>"):
            continue
        surname = a.split(",", 1)[0].strip()
        if not surname:
            continue
        try:
            dl = int(float(dl_raw)) if dl_raw is not None else 0
        except (TypeError, ValueError):
            dl = 0
        sn_key = f"^{surname},"
        sn_ent = by_surname.setdefault(sn_key, {"downloads": 0, "books": 0,
                                                 "canonical_first": a})
        sn_ent["downloads"] += dl
        sn_ent["books"] += 1
        can_ent = by_canonical.setdefault(a, {"downloads": 0, "books": 0,
                                               "canonical_name": a})
        can_ent["downloads"] += dl
        can_ent["books"] += 1
    return {"by_surname": by_surname, "by_canonical": by_canonical}


def get_prominence_index(force_reload: bool = False) -> dict:
    """Thread-safe accessor. Rebuilds when metadata file mtime changes."""
    with _prom_lock:
        if force_reload or _prom_state["data"] is None:
            _prom_state["data"] = _build_prominence_index()
            _prom_state["build_ts"] = time.time()
        return _prom_state["data"]


def prominence_for(author_regex: str) -> dict:
    """Get prominence for a single author_regex (surname aggregate)."""
    idx = get_prominence_index()
    if "by_surname" not in idx:
        return idx.get(author_regex, {})
    return idx["by_surname"].get(author_regex, {})


def prominence_for_canonical(canonical_name: str) -> dict:
    """Get prominence for an exact canonical author name (per-author)."""
    idx = get_prominence_index()
    if "by_canonical" not in idx:
        return {}
    return idx["by_canonical"].get(canonical_name, {})


# =====================================================================
# Ranker + confidence math
# =====================================================================

# Banding fuzz scores so we don't over-weight tiny score differences.
# 95-100 = ~exact; 85-94 = strong; 70-84 = okay; below = noisy.
def _fuzz_band(score: int) -> int:
    if score >= 95:
        return 4
    if score >= 85:
        return 3
    if score >= 70:
        return 2
    return 1


def rank_author_candidates(cands: list["Candidate"]) -> list["Candidate"]:
    """Sort by (fuzz_band desc, prominence desc, books_in_corpus desc).

    Structural fix for B-R14-13: «Hugo» matches both Victor Hugo
    (~15K downloads) and obscure Ganz, Hugo (~0). Prominence ranking
    pushes Victor to top-1.
    """
    return sorted(
        cands,
        key=lambda c: (
            _fuzz_band(c.score),
            c.prominence,
            c.books_in_corpus,
            -len(c.display),    # tiebreaker: shorter display = canonical
        ),
        reverse=True,
    )


def confidence_from_gap(top: "Candidate", runner_up: "Candidate | None") -> tuple[float, str]:
    """Score 0..1 based on top-1 vs top-2 gap. Returns (confidence, reason)."""
    if top.source == "alias_curated":
        return 1.0, "curated alias exact match"
    if top.source == "ru_title_alias":
        return 0.95, "RU title alias map"

    if runner_up is None:
        return 0.90, "single candidate"

    fuzz_gap = top.score - runner_up.score
    prom_ratio = (
        top.prominence / runner_up.prominence
        if runner_up.prominence > 0 else 999.0
    )

    if fuzz_gap >= 10 or prom_ratio >= 5.0:
        return 0.88, f"clear winner (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"
    if fuzz_gap >= 5 or prom_ratio >= 2.0:
        return 0.72, f"likely winner (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"
    return 0.55, f"ambiguous (fuzz gap {fuzz_gap}, prom ratio {prom_ratio:.1f}x)"


__all__ = [
    "_prom_lock",
    "_prom_state",
    "get_prominence_index",
    "prominence_for",
    "prominence_for_canonical",
    "_fuzz_band",
    "rank_author_candidates",
    "confidence_from_gap",
]
