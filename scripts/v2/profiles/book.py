"""BookProfile — cached composite for one PG/U book.

Bundles readability + archaic words + emotion profile + affinity head into a
single blob. Built lazily on first ask; subsequent requests for the same
pg_id within the same corpus_version are served from SQLite cache.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.profiles import store

log = logging.getLogger("wordcracker.v2.profiles.book")


def get_or_build(pg_id: str) -> dict | None:
    """Return cached BookProfile or build by stitching together v1 calls."""
    cached = store.get("book_profile", "pg_id", pg_id)
    if cached is not None:
        return cached

    try:
        from scripts.rag_tools import book_readability as _read
        from scripts.learning_tools import (
            affinity_by_book as _affinity,
            book_archaic_words as _archaic,
        )
        try:
            from scripts.rag_tools import book_emotion_profile as _emotion
        except ImportError:
            _emotion = None
    except ImportError as e:
        log.warning("v1 book tools unavailable: %s", e)
        return None

    out: dict = {"pg_id": pg_id}

    try:
        readability = _read(pg_id=pg_id)
        out["readability"] = readability if isinstance(readability, dict) else None
    except Exception as e:
        out["readability"] = {"error": str(e)}

    try:
        archaic = _archaic(pg_id=pg_id, top=30)
        out["archaic"] = archaic if isinstance(archaic, dict) else None
    except Exception as e:
        out["archaic"] = {"error": str(e)}

    try:
        affinity = _affinity(pg_id=pg_id, top=30,
                             exclude_proper_nouns=True, min_corpus_count=200)
        out["affinity"] = affinity if isinstance(affinity, dict) else None
    except Exception as e:
        out["affinity"] = {"error": str(e)}

    if _emotion is not None:
        try:
            emotion = _emotion(pg_id=pg_id)
            out["emotion"] = emotion if isinstance(emotion, dict) else None
        except Exception as e:
            out["emotion"] = {"error": str(e)}

    # If literally nothing worked → don't poison the cache with an empty blob.
    if not any(isinstance(out.get(k), dict) and "error" not in out.get(k, {})
               for k in ("readability", "archaic", "affinity")):
        return None

    store.put("book_profile", "pg_id", pg_id, out)
    return out
