#!/usr/bin/env python3
"""Pre-compute author → total tokens table for fast top_authors_by(tokens).

The v1 top_authors_by(metric="tokens") path opens every per-book counts
file and sums the second column. For 75k books that's a 60s scan even
with warm OS cache. We do it once, cache as JSON, lookup later in 50ms.

Run inside the container (one-off, after corpus updates):

    docker compose exec -T gutenberg-lab python -u \
      /workspace/scripts/v2/build_author_tokens.py

Output: /workspace/spgc/derived/author_tokens.json
        { "Doyle, Arthur Conan": {"tokens": 4900000, "books": 123}, ... }

The Sprint 11.2 top_authors_by wrapper reads this if metric=tokens; falls
back to live scan if file missing.
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

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(message)s")
log = logging.getLogger("build_author_tokens")

OUT_PATH = Path("/workspace/spgc/derived/author_tokens.json")


def main() -> int:
    from rag_tools import _metadata_df, _counts_path

    df = _metadata_df()
    if df is None or not len(df):
        log.error("metadata frame empty")
        return 1
    log.info("metadata: %d rows", len(df))

    tokens: Counter[str] = Counter()
    books: Counter[str] = Counter()
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
        try:
            with open(f, encoding="utf-8") as fh:
                book_total = sum(int(line.split("\t", 1)[1]) for line in fh
                                 if "\t" in line)
        except (OSError, ValueError):
            skipped += 1
            continue
        tokens[author] += book_total
        books[author] += 1
        if i % 5000 == 0:
            elapsed = time.perf_counter() - t0
            log.info("  scanned %d/%d books, %d authors, %.0fs",
                     i, len(df), len(tokens), elapsed)

    out = {
        author: {"tokens": int(tokens[author]),
                 "books": int(books[author])}
        for author in tokens
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    elapsed = time.perf_counter() - t0
    log.info("done: %d authors, %d skipped, %.0fs total → %s",
             len(out), skipped, elapsed, OUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
