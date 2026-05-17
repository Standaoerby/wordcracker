"""Corpus version snapshot for ToolResult.source_info.

Contract: docs/v2/SPECS.md §5.

Reads on first call:
- /workspace/spgc/derived/corpus_meta.json — produced by spgc_corpus_stats.py
- /data/raw_text/ directory listing (count)
- scripts/v2/__version__.py for analytics_version

The lookup is cheap and cached in-process; bust by importing _reset() (used in
tests). We do *not* hit ChromaDB here — chunks_total can be filled later when
status_server already knows it."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from scripts.v2.__version__ import ANALYTICS_VERSION
from scripts.v2.types import SourceInfo

_CORPUS_META_PATH = Path(os.environ.get(
    "WC_CORPUS_META", "/workspace/spgc/derived/corpus_meta.json"
))
_RAW_TEXT_DIR = Path(os.environ.get("WC_RAW_TEXT_DIR", "/workspace/raw_text"))
_SPGC_BASELINE = "SPGC-2018-07-18"


@dataclass
class CorpusVersion:
    timestamp: str
    books_total: int
    chunks_total: int | None
    analytics_version: str
    spgc_baseline: str
    user_uploads: int
    orphan_pg: int

    def to_source_info(self) -> SourceInfo:
        return SourceInfo(
            corpus_version=self.timestamp,
            analytics_version=self.analytics_version,
            spgc_baseline=self.spgc_baseline,
        )


_cache: dict[str, CorpusVersion | None] = {"v": None}


def current() -> CorpusVersion:
    if _cache["v"] is not None:
        return _cache["v"]

    timestamp = "unknown"
    books_total = -1
    if _CORPUS_META_PATH.exists():
        try:
            meta = json.loads(_CORPUS_META_PATH.read_text())
            timestamp = meta.get("built_at") or meta.get("timestamp", "unknown")
            books_total = int(meta.get("books_aggregated") or meta.get("books_total") or -1)
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    # raw_text gives a fresher headcount than the frozen 2018 SPGC meta.
    if _RAW_TEXT_DIR.exists():
        try:
            books_total = max(books_total, sum(1 for _ in _RAW_TEXT_DIR.glob("*.txt")))
        except OSError:
            pass

    cv = CorpusVersion(
        timestamp=timestamp,
        books_total=books_total,
        chunks_total=None,
        analytics_version=ANALYTICS_VERSION,
        spgc_baseline=_SPGC_BASELINE,
        user_uploads=0,
        orphan_pg=0,
    )
    _cache["v"] = cv
    return cv


def current_source_info() -> SourceInfo:
    return current().to_source_info()


def _reset() -> None:
    """For tests."""
    _cache["v"] = None
