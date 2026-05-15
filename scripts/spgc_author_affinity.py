#!/usr/bin/env python3
"""
Per-author affinity vs SPGC baseline corpus.

Selects books whose `author` column matches the given regex,
aggregates author word counts, joins with precomputed corpus counts
and writes <out>/<slug>_affinity.csv.

affinity(word) = (freq(word, author) / total(author))
               / (freq(word, corpus) / total(corpus))

Usage:
  python spgc_author_affinity.py \
    --metadata     /workspace/spgc/SPGC-metadata-2018-07-18.csv \
    --counts-dir   /workspace/spgc/SPGC-counts-2018-07-18 \
    --corpus-counts /workspace/spgc/derived/corpus_counts.csv \
    --author       'Wodehouse' \
    --out          /workspace/spgc/derived \
    --min-author-count 5
"""
import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd
from tqdm import tqdm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--counts-dir", required=True, type=Path)
    ap.add_argument("--corpus-counts", required=True, type=Path)
    ap.add_argument("--author", required=True, help="regex matched against author column (case-insensitive)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--min-author-count", type=int, default=5)
    ap.add_argument("--slug", default=None, help="output filename slug (default: derived from --author)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    slug = args.slug or re.sub(r"[^a-z0-9]+", "_", args.author.lower()).strip("_") or "author"

    print(f"[meta] {args.metadata}")
    df = pd.read_csv(args.metadata)
    mask_lang = df["language"].fillna("").str.contains(f"'{args.lang}'", regex=False)
    mask_author = df["author"].fillna("").str.contains(args.author, case=False, regex=True)
    sel = df[mask_lang & mask_author]
    print(f"  matched {len(sel)} books for author~='{args.author}' lang='{args.lang}'")
    if not len(sel):
        print("No books matched. Aborting.")
        return

    titles_sample = sel[["id", "title"]].head(10).to_dict("records")
    for t in titles_sample:
        print(f"    {t['id']}  {t['title']}")
    if len(sel) > 10:
        print(f"    ... +{len(sel) - 10} more")

    author_counts = Counter()
    author_tokens = 0
    used_ids = []
    for pg_id in tqdm(sel["id"], desc="author counts", unit="book"):
        f = args.counts_dir / f"{pg_id}_counts.txt"
        if not f.exists():
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                w, c = parts[0], int(parts[1])
                author_counts[w] += c
                author_tokens += c
        used_ids.append(pg_id)

    print(f"  author books w/ counts: {len(used_ids)}")
    print(f"  author tokens: {author_tokens:,}")
    print(f"  author vocab:  {len(author_counts):,}")

    print(f"[corpus] {args.corpus_counts}")
    corpus = {}
    corpus_total = 0
    with open(args.corpus_counts, encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd)
        for w, c in rd:
            c = int(c)
            corpus[w] = c
            corpus_total += c
    print(f"  corpus tokens: {corpus_total:,}  vocab: {len(corpus):,}")

    rows = []
    for w, ac in author_counts.items():
        if ac < args.min_author_count:
            continue
        cc = corpus.get(w, 0)
        if cc == 0:
            affinity = None  # word never appears outside author corpus
        else:
            affinity = (ac / author_tokens) / (cc / corpus_total)
        rows.append((w, ac, cc, affinity))

    # sort: None affinity (unique-to-author) at top, then by affinity desc
    rows.sort(key=lambda r: (r[3] is not None, -(r[3] or 0)))

    out_csv = args.out / f"{slug}_affinity.csv"
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["word", "author_count", "corpus_count", "affinity"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], f"{r[3]:.6f}" if r[3] is not None else ""])
    print(f"[write] {out_csv}  ({len(rows)} rows, min_author_count={args.min_author_count})")

    out_meta = args.out / f"{slug}_affinity_meta.json"
    with open(out_meta, "w", encoding="utf-8") as fh:
        json.dump({
            "author_pattern": args.author,
            "slug": slug,
            "lang": args.lang,
            "books_matched": len(sel),
            "books_aggregated": len(used_ids),
            "author_tokens": author_tokens,
            "author_vocab": len(author_counts),
            "corpus_tokens": corpus_total,
            "corpus_vocab": len(corpus),
            "min_author_count": args.min_author_count,
            "rows_written": len(rows),
        }, fh, indent=2)
    print(f"[write] {out_meta}")

    # quick eyeball top-30
    print("\nTop 30 affinity (excluding unique-to-author):")
    print(f"  {'word':<25} {'auth':>6} {'corp':>9} {'aff':>10}")
    eyeball = [r for r in rows if r[3] is not None][:30]
    for w_, ac, cc, aff in eyeball:
        print(f"  {w_:<25} {ac:>6} {cc:>9} {aff:>10.2f}")


if __name__ == "__main__":
    main()
