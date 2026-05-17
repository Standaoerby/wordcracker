#!/usr/bin/env python3
"""
build_burrows_vectors.py — pre-compute Burrows Delta author vectors.

Burrows Delta (J. Burrows, 2002) is the classical stylometric method
for author attribution. The idea: function words like 'the', 'of', 'and'
are content-free but their *relative frequencies* are remarkably stable
within an author and remarkably distinct across authors. Train on the
top-N most common words in the corpus, normalize each author's frequencies
to z-scores per word, store as a vector. To attribute an unknown text,
compute its z-vector the same way and pick the author with the smallest
Manhattan distance (= mean of absolute differences per word).

This script does the offline 'train' part. Outputs:
  /data/spgc/derived/burrows_vectors.npz
    top_words    : str[N]    the chosen function-word vocabulary
    authors      : str[A]    SPGC-format author strings, in vector order
    book_counts  : int[A]    how many books per author entered the vector
    means        : float[N]  per-word mean across the author population
    stds         : float[N]  per-word std (Burrows' Z denominator)
    vectors      : float[A,N] z-scored frequency vector per author

Run-once. Recompute when SPGC + orphan + user_counts changes substantially.

CLI:
  python build_burrows_vectors.py --top-words 200 --min-books 3
"""
import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

DERIVED = Path("/workspace/spgc/derived")
SPGC_COUNTS = Path("/workspace/spgc/SPGC-counts-2018-07-18")
USER_COUNTS = Path("/workspace/spgc/user_counts")
CORPUS_COUNTS = DERIVED / "corpus_counts.csv"
OUT_NPZ = DERIVED / "burrows_vectors.npz"


def _top_function_words(n: int) -> list[str]:
    """Top-N words from the global corpus_counts.csv, alpha-only."""
    out = []
    with open(CORPUS_COUNTS, encoding="utf-8") as fh:
        rd = csv.reader(fh); next(rd)
        for w, _c in rd:
            if w.isalpha() and len(w) <= 8:
                out.append(w)
                if len(out) >= n:
                    break
    return out


def _counts_path_for(pg: str) -> Path:
    """Pick SPGC dump first, then user_counts/ for orphan PG and U-uploads."""
    p = SPGC_COUNTS / f"{pg}_counts.txt"
    if p.exists():
        return p
    return USER_COUNTS / f"{pg}_counts.txt"


def _author_counts(pg_ids: list[str]) -> tuple[Counter, int]:
    """Sum all counts files for an author. Returns Counter + total tokens."""
    agg = Counter()
    total = 0
    for pg in pg_ids:
        f = _counts_path_for(pg)
        if not f.exists():
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                try:
                    agg[parts[0]] += int(parts[1])
                except ValueError:
                    continue
        total = sum(agg.values())  # cumulative
    return agg, sum(agg.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-words", type=int, default=200,
                    help="Number of function words in the vector (default 200)")
    ap.add_argument("--min-books", type=int, default=3,
                    help="Drop authors with fewer than this many books (default 3)")
    ap.add_argument("--min-tokens", type=int, default=20_000,
                    help="Drop authors with fewer than this many total tokens "
                         "across all their books (default 20k — small samples "
                         "make Burrows unstable)")
    ap.add_argument("--limit-authors", type=int, default=0,
                    help="cap for quick development runs")
    args = ap.parse_args()

    t_start = time.perf_counter()
    print(f"[burrows] picking top {args.top_words} function words", flush=True)
    fw = _top_function_words(args.top_words)
    print(f"[burrows] first 20: {fw[:20]}", flush=True)
    fw_idx = {w: i for i, w in enumerate(fw)}

    # Pull the merged metadata frame to enumerate authors.
    sys.path.insert(0, "/workspace/scripts")
    from rag_tools import _metadata_df
    df = _metadata_df()
    df = df[df["language"].fillna("").str.contains("'en'", regex=False)]
    df = df.dropna(subset=["author"])
    print(f"[burrows] candidate authors: {df['author'].nunique():,}", flush=True)

    # Group books by author, filter by min_books.
    grouped = df.groupby("author")["id"].apply(list)
    grouped = grouped[grouped.apply(len) >= args.min_books]
    print(f"[burrows] authors with >={args.min_books} books: {len(grouped):,}",
          flush=True)
    if args.limit_authors:
        grouped = grouped.head(args.limit_authors)

    raw_freq: list[np.ndarray] = []
    book_counts: list[int] = []
    authors_kept: list[str] = []
    skipped_short = 0
    t_walk = time.perf_counter()

    for i, (author, pg_ids) in enumerate(grouped.items()):
        counts, total = _author_counts(pg_ids)
        if total < args.min_tokens:
            skipped_short += 1
            continue
        vec = np.zeros(len(fw), dtype=np.float64)
        for w, idx in fw_idx.items():
            vec[idx] = counts.get(w, 0)
        # Normalize: per million tokens (so document size doesn't dominate)
        vec = vec * (1_000_000.0 / total)
        raw_freq.append(vec)
        authors_kept.append(author)
        book_counts.append(len(pg_ids))
        if (i + 1) % 200 == 0:
            rate = (i + 1) / (time.perf_counter() - t_walk)
            print(f"[burrows] {i+1}/{len(grouped)} · {rate:.1f} authors/s · "
                  f"kept {len(authors_kept)} · short-skipped {skipped_short}",
                  flush=True)

    if not raw_freq:
        print("no authors survived the filter", flush=True)
        return

    R = np.vstack(raw_freq)             # (A, N) per-million frequencies
    means = R.mean(axis=0)
    stds = R.std(axis=0)
    stds[stds == 0] = 1.0               # avoid div-by-zero
    Z = (R - means) / stds              # (A, N) z-scored per word

    np.savez_compressed(
        OUT_NPZ,
        top_words=np.array(fw, dtype=object),
        authors=np.array(authors_kept, dtype=object),
        book_counts=np.array(book_counts, dtype=np.int64),
        means=means.astype(np.float32),
        stds=stds.astype(np.float32),
        vectors=Z.astype(np.float32),
    )
    elapsed = time.perf_counter() - t_start
    print(f"[burrows] saved {len(authors_kept):,} authors x "
          f"{len(fw)} words to {OUT_NPZ} in {elapsed:.0f}s "
          f"({skipped_short} skipped for too-few tokens)", flush=True)


if __name__ == "__main__":
    main()
