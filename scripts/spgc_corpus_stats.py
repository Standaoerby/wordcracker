#!/usr/bin/env python3
"""
SPGC baseline corpus aggregator.

Reads all per-book SPGC counts files filtered by language and writes
a single global counts CSV plus a metadata JSON.

Outputs:
  <out>/corpus_counts.csv  -- columns: word, count (sorted by count desc)
  <out>/corpus_meta.json   -- aggregation summary

Usage:
  python spgc_corpus_stats.py \
    --metadata    /workspace/spgc/SPGC-metadata-2018-07-18.csv \
    --counts-dir  /workspace/spgc/SPGC-counts-2018-07-18 \
    --out         /workspace/spgc/derived \
    --lang        en
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import pandas as pd
from tqdm import tqdm


def select_ids(meta_path: Path, lang: str):
    df = pd.read_csv(meta_path)
    # language column is stored as a literal Python list repr, e.g. "['en']"
    mask = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
    return df.loc[mask, "id"].tolist(), len(df)


def aggregate(ids, counts_dir: Path):
    total = Counter()
    seen = missing = total_tokens = 0
    for pg_id in tqdm(ids, desc="aggregating", unit="book"):
        f = counts_dir / f"{pg_id}_counts.txt"
        if not f.exists():
            missing += 1
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c = parts[0], int(parts[1])
                    total[w] += c
                    total_tokens += c
            seen += 1
        except (UnicodeDecodeError, ValueError):
            missing += 1
    return total, seen, missing, total_tokens


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--counts-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--lang", default="en")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    print(f"[meta] {args.metadata}")
    ids, total_books = select_ids(args.metadata, args.lang)
    print(f"  matched {len(ids)} / {total_books} books for lang='{args.lang}'")

    print(f"[counts] {args.counts_dir}")
    counts, seen, missing, total_tokens = aggregate(ids, args.counts_dir)
    print(f"  books aggregated: {seen}, missing files: {missing}")
    print(f"  total tokens:     {total_tokens:,}")
    print(f"  vocab size:       {len(counts):,}")

    out_csv = args.out / "corpus_counts.csv"
    print(f"[write] {out_csv}")
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["word", "count"])
        for word, count in counts.most_common():
            w.writerow([word, count])

    out_meta = args.out / "corpus_meta.json"
    summary = {
        "source_metadata": args.metadata.name,
        "counts_dir": args.counts_dir.name,
        "lang": args.lang,
        "books_matched": len(ids),
        "books_aggregated": seen,
        "books_missing_counts": missing,
        "total_tokens": total_tokens,
        "vocab_size": len(counts),
    }
    with open(out_meta, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[write] {out_meta}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
