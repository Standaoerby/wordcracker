#!/usr/bin/env python3
"""Pre-compute per-author vocabulary richness metrics.

Phase 3 W-8 (Stan 2026-05-22) — companion to build_author_tokens.py.
Where author_tokens captures TEXT VOLUME, this captures TEXT RICHNESS
(types / hapax / TTR / Guiraud R / Yule K).

Run inside the container after corpus updates:

    docker compose exec -T gutenberg-lab python -u \
      /workspace/scripts/v2/build_author_richness.py

Output: /workspace/spgc/derived/author_richness.json
        {
          "Doyle, Arthur Conan": {
            "tokens": 4900000, "types": 88123, "hapax": 23450,
            "ttr": 0.018, "hapax_ratio": 0.266,
            "guiraud_r": 39.81, "yule_k": 121.4,
            "books_with_counts": 123
          },
          ...
        }

The v2 lexical_richness_authors wrapper reads this if present; falls back
to live scan (heavy, ~3 min) when the file is missing.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.authors.lexical_richness import (
    compute_richness_from_counts,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s")
log = logging.getLogger("build_author_richness")

OUT_PATH = Path("/workspace/spgc/derived/author_richness.json")


def main() -> int:
    from rag_tools import _metadata_df, _counts_path

    df = _metadata_df()
    if df is None or not len(df):
        log.error("metadata frame empty")
        return 1
    log.info("metadata: %d rows", len(df))

    per_author: dict[str, Counter[str]] = {}
    books_count: Counter[str] = Counter()
    skipped = 0
    t0 = time.perf_counter()
    for i, row in enumerate(df.itertuples(index=False), 1):
        author = getattr(row, "author", None)
        pg = getattr(row, "id", None)
        if not author or not pg:
            continue
        f = _counts_path(pg)
        if not f.exists():
            skipped += 1
            continue
        c = per_author.setdefault(author, Counter())
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    try:
                        c[parts[0]] += int(parts[1])
                    except ValueError:
                        continue
        except OSError:
            skipped += 1
            continue
        books_count[author] += 1
        if i % 5000 == 0:
            elapsed = time.perf_counter() - t0
            log.info("  scanned %d/%d books, %d authors, %.0fs",
                     i, len(df), len(per_author), elapsed)

    out: dict[str, dict] = {}
    for author, counts in per_author.items():
        m = compute_richness_from_counts(counts)
        m["books_with_counts"] = int(books_count[author])
        out[author] = m
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    elapsed = time.perf_counter() - t0
    log.info("done: %d authors, %d skipped, %.0fs total → %s",
             len(out), skipped, elapsed, OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
