#!/usr/bin/env python3
"""
Extract +/-N token context windows for words of interest from SPGC tokens.

SPGC tokens are lowercased and stripped of punctuation, but token order is
preserved per book. That's enough to read a rare word in context. Each
sample line is rendered as: "<-left-> [WORD] <-right->".

Two modes:
  - single word:    --word jeeves
  - batch from CSV: --words-from /workspace/spgc/derived/wodehouse_affinity.csv --column word --limit 50

Usage:
  python spgc_contexts.py \
    --metadata    /workspace/spgc/SPGC-metadata-2018-07-18.csv \
    --tokens-dir  /workspace/spgc/SPGC-tokens-2018-07-18 \
    --author      'Wodehouse' \
    --word        'bally' \
    --window 7 --max-samples 5 \
    --out         /workspace/spgc/derived/contexts/bally.txt
"""
import argparse
import csv
import random
from pathlib import Path

import pandas as pd
from tqdm import tqdm


def filter_books(meta_path: Path, lang: str, author: str | None):
    df = pd.read_csv(meta_path)
    mask = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
    if author:
        mask &= df["author"].fillna("").str.contains(author, case=False, regex=True)
    return df.loc[mask]


def load_tokens(path: Path):
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\n") for line in fh]


def find_contexts(tokens, target, window, max_samples, rng):
    hits = [i for i, t in enumerate(tokens) if t == target]
    if not hits:
        return []
    if len(hits) > max_samples:
        hits = rng.sample(hits, max_samples)
        hits.sort()
    out = []
    for i in hits:
        lo = max(0, i - window)
        hi = min(len(tokens), i + window + 1)
        left = " ".join(tokens[lo:i])
        right = " ".join(tokens[i + 1:hi])
        out.append((i, f"{left}  [{target.upper()}]  {right}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--tokens-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--author", default=None, help="regex against author column; omit for full corpus")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--word", help="single target word (lowercase)")
    grp.add_argument("--words-from", type=Path, help="CSV file with target words")
    ap.add_argument("--column", default="word", help="column name in --words-from CSV")
    ap.add_argument("--limit", type=int, default=50, help="max number of words from CSV")
    ap.add_argument("--window", type=int, default=7)
    ap.add_argument("--max-samples", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    if args.word:
        words = [args.word.lower()]
    else:
        df = pd.read_csv(args.words_from)
        words = [str(w).lower() for w in df[args.column].head(args.limit).tolist()]
    target_set = set(words)
    print(f"[targets] {len(words)} word(s): {words[:10]}{' ...' if len(words) > 10 else ''}")

    sel = filter_books(args.metadata, args.lang, args.author)
    print(f"[books] {len(sel)} matching lang='{args.lang}' author~='{args.author}'")

    # word -> list of (pg_id, title, line)
    samples = {w: [] for w in words}
    book_titles = dict(zip(sel["id"], sel["title"].fillna("")))

    for pg_id in tqdm(sel["id"], desc="scanning books", unit="book"):
        f = args.tokens_dir / f"{pg_id}_tokens.txt"
        if not f.exists():
            continue
        tokens = load_tokens(f)
        # quick scan: do any targets appear at all?
        if not any(t in target_set for t in tokens):
            continue
        for w in words:
            if len(samples[w]) >= args.max_samples:
                continue
            hits = find_contexts(tokens, w, args.window, args.max_samples - len(samples[w]), rng)
            for pos, line in hits:
                samples[w].append((pg_id, book_titles.get(pg_id, ""), pos, line))

    with open(args.out, "w", encoding="utf-8") as fh:
        for w in words:
            fh.write(f"=== {w}  ({len(samples[w])} samples) ===\n")
            if not samples[w]:
                fh.write("  (no occurrences)\n\n")
                continue
            for pg_id, title, pos, line in samples[w]:
                fh.write(f"  [{pg_id} @{pos}] {title}\n    {line}\n")
            fh.write("\n")
    print(f"[write] {args.out}")

    found = sum(1 for w in words if samples[w])
    print(f"  found contexts for {found} / {len(words)} words")


if __name__ == "__main__":
    main()
