"""v2-side dict normalization — Type 2 of T2 (Phase 2).

Single source of truth for fields that historically lived under two
names on the v2 side. Replaces fallback chains across `.get` calls at
every callsite with one helper that owns the choice.

Rules:
  * No helper here exposes a multi-key fallback chain to callers.
    Inside a helper we may consult two historical aliases; outside,
    the caller reads a single canonical value.
  * Helpers never raise on shape drift — they return a safe default
    (empty string / None / 0) so callers can decide whether to skip
    or surface a placeholder.
"""
from __future__ import annotations

from typing import Any


def scope_book_id(scope: Any) -> str | None:
    """Pull the pg_id from a v2 scope dict.

    v2 planner builds `scope={"book": "PG1342", ...}`; legacy callsites
    sometimes use `{"pg_id": "PG1342"}`. Read whichever is present.
    Returns None if neither key is set (e.g. author/all_corpus scope).
    """
    if not isinstance(scope, dict):
        return None
    val = scope.get("book")
    if val:
        return val
    val = scope.get("pg_id")
    if val:
        return val
    return None


def match_id(m: Any) -> str | None:
    """Pull the canonical id of a search/match dict.

    v2 hybrid_search stamps `pg_id` onto its output; legacy v1 lexical /
    semantic responses sometimes carry only `id`. The canonical key is
    `pg_id`; `id` is the historical alias.
    Returns None if neither is set (caller should skip such rows).
    """
    if not isinstance(m, dict):
        return None
    val = m.get("pg_id")
    if val:
        return val
    val = m.get("id")
    if val:
        return val
    return None


def search_snippet(lex_match: Any, sem_match: Any) -> str | None:
    """Best-effort snippet for a hybrid-merged match.

    Priority (matches the historical RRF-merge fallback chain in
    `tools/search/hybrid.py`):
      1. lexical match `snippet` — usually a BM25-highlighted fragment.
      2. semantic match `text`   — full passage from ChromaDB.
      3. semantic match `snippet` — pre-trimmed semantic excerpt.
    Returns None when nothing usable is present.
    """
    if isinstance(lex_match, dict):
        val = lex_match.get("snippet")
        if val:
            return val
    if isinstance(sem_match, dict):
        val = sem_match.get("text")
        if val:
            return val
        val = sem_match.get("snippet")
        if val:
            return val
    return None


__all__ = ["scope_book_id", "match_id", "search_snippet"]
