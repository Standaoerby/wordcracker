"""LemmaProfile — per-lemma corpus snapshot.

Each profile bundles the cheap stats that learning_words / vocab tooling
reads many times during a session:
  * global_count  — total occurrences across the SPGC corpus
  * book_count    — number of books containing the lemma
  * author_count  — number of distinct authors using it
  * rarity        — log10-based score (lower = more common)
  * pos_modes     — top 3 POS tags from spaCy lemmatization (when known)
  * difficulty    — rough CEFR bucket from corpus_count percentile

Build is offline (scripts/v2/build_lemma_index.py) — one-time pass over
corpus_counts.csv + per-book counts. The runtime lookup is O(1) via the
SQLite profile store, so the planner can call `lemma_profile(word)`
repeatedly without re-scanning the corpus.

If the lemma isn't in the store yet, `get_or_build` falls back to a
lightweight on-the-fly computation using just corpus_counts.csv (no
per-book scan) and persists the result — slow first hit, instant after.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.profiles import store

log = logging.getLogger("wordcracker.v2.profiles.lemma")

CORPUS_COUNTS = Path(os.environ.get(
    "WC_CORPUS_COUNTS",
    "/workspace/spgc/derived/corpus_counts.csv",
))


# CEFR-ish buckets keyed on corpus_count percentile. Tuned to feel right
# for English literature — most basic English words have global_count
# >50k, mid-frequency are 1k-50k, rare are <100, anything below ~10 is
# essentially OOV or proper-noun bleed.
def _difficulty_from_count(count: int) -> str:
    if count >= 50000:
        return "basic"            # A1-A2
    if count >= 5000:
        return "intermediate"     # B1-B2
    if count >= 200:
        return "advanced"         # C1
    if count >= 10:
        return "rare"             # C2+ / specialist
    return "ultra_rare"


def _rarity_from_count(count: int) -> float:
    """log10 inverse — 1.0 for ultra_rare, 0.0 for the very-most-common."""
    if count <= 0:
        return 1.0
    # corpus_counts maxes at ~50M for "the" → log10 ≈ 7.7
    # We map 0-7.7 → 1.0-0.0 so high score = rare = useful for learning.
    return max(0.0, min(1.0, 1.0 - math.log10(max(1, count)) / 8.0))


_corpus_counts_cache: dict[str, int] | None = None


def _load_corpus_counts() -> dict[str, int]:
    global _corpus_counts_cache
    if _corpus_counts_cache is not None:
        return _corpus_counts_cache
    out: dict[str, int] = {}
    if not CORPUS_COUNTS.exists():
        log.warning("corpus_counts.csv not found at %s", CORPUS_COUNTS)
        _corpus_counts_cache = out
        return out
    try:
        with open(CORPUS_COUNTS, encoding="utf-8") as fh:
            rd = csv.reader(fh)
            next(rd, None)  # header
            for row in rd:
                if len(row) < 2:
                    continue
                w, c = row[0], row[1]
                try:
                    out[w] = int(c)
                except ValueError:
                    continue
    except OSError as e:
        log.warning("failed to read %s: %s", CORPUS_COUNTS, e)
    _corpus_counts_cache = out
    return out


def get_or_build(lemma: str) -> dict | None:
    """Return cached LemmaProfile or build a lightweight one from corpus_counts.

    The build path here is the *light* one — no per-book scan. The
    heavyweight builder (offline) fills book_count / author_count /
    pos_modes; until that runs, those fields stay None and the caller
    treats them as best-effort. Difficulty + rarity are reliable from
    just corpus_count alone.
    """
    lemma = (lemma or "").strip().lower()
    if not lemma:
        return None

    cached = store.get("lemma_profile", "lemma", lemma)
    if cached is not None:
        return cached

    corpus = _load_corpus_counts()
    count = corpus.get(lemma, 0)
    if count == 0:
        return None  # don't poison the cache with empty lemmas

    payload = {
        "lemma": lemma,
        "global_count": count,
        "book_count": None,
        "author_count": None,
        "pos_modes": None,
        "rarity": round(_rarity_from_count(count), 4),
        "difficulty": _difficulty_from_count(count),
    }
    store.put("lemma_profile", "lemma", lemma, payload)
    return payload
