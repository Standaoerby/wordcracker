"""AuthorProfile — composite cached snapshot of one author.

Wraps v1 `author_profile` (which runs 6 sub-tools in parallel via
ThreadPoolExecutor — see Sprint 12 D41). Subsequent calls for the same
author within the same corpus_version skip the 11s scan and return the
cached blob in < 5ms.

Public API:
    get_or_build(author_regex) → dict
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.profiles import store

log = logging.getLogger("wordcracker.v2.profiles.author")


def get_or_build(author_regex: str, country: str | None = None) -> dict | None:
    """Return cached AuthorProfile or build fresh via v1 and persist.

    Returns None when v1 has nothing to say (author missing from corpus).
    Cached entries from older corpus_version are treated as misses — they
    get rebuilt automatically.
    """
    key = f"{author_regex}|{country or ''}"
    cached = store.get("author_profile", "author_regex", key)
    if cached is not None:
        return cached

    try:
        from scripts.rag_tools import author_profile as _v1
    except ImportError as e:
        log.warning("v1 author_profile unavailable: %s", e)
        return None

    try:
        raw = _v1(author_regex=author_regex, country=country)
    except Exception as e:
        log.warning("v1 author_profile crashed: %s", e)
        return None

    if isinstance(raw, dict) and raw.get("error"):
        return None

    store.put("author_profile", "author_regex", key, raw)
    return raw
