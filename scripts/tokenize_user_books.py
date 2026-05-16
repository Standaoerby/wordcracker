#!/usr/bin/env python3
"""
tokenize_user_books.py — produce SPGC-compatible tokens + counts files for
user-uploaded books (u*.txt in /data/raw_text/).

For each u<N>.txt:
  - /data/spgc/user_tokens/u<N>_tokens.txt   — one lowercased alpha token per line
  - /data/spgc/user_counts/u<N>_counts.txt   — `word\tcount` lines, sorted desc

After tokenization the book becomes a first-class citizen for top_ngrams_by_author,
affinity_by_author, word_contexts, word_collocates, lexical_diversity and
word_freq_timeline (latter still needs authoryearofbirth in metadata).

CLI:
  # tokenize all u*.txt that don't already have tokens
  python tokenize_user_books.py

  # tokenize a single book (e.g. just uploaded U7)
  python tokenize_user_books.py --book U7

  # force re-tokenize even if output exists
  python tokenize_user_books.py --force
"""
import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path

RAW_DIR    = Path("/workspace/raw_text")
USER_TOKENS_DIR = Path("/workspace/spgc/user_tokens")
USER_COUNTS_DIR = Path("/workspace/spgc/user_counts")

# Same Penn-Treebank-ish tokenizer that SPGC used (lowercased alphabetic + apostrophe).
# We DON'T strip stopwords — SPGC counts keep them, our tools filter at analysis time.
TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _tokenize_text(text: str) -> list[str]:
    """Lowercased word-level tokens. Same regex SPGC tools effectively use."""
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def _process(book_id: str, force: bool = False) -> dict:
    """Tokenize one u<N>.txt → write tokens + counts. Returns stats dict."""
    src = RAW_DIR / f"{book_id.lower()}.txt"
    if not src.exists():
        return {"id": book_id, "error": "raw text not found", "path": str(src)}

    tok_path   = USER_TOKENS_DIR / f"{book_id}_tokens.txt"
    count_path = USER_COUNTS_DIR / f"{book_id}_counts.txt"
    if not force and tok_path.exists() and count_path.exists():
        return {"id": book_id, "skipped": "already tokenized",
                "tokens_file": str(tok_path)}

    t0 = time.perf_counter()
    text = src.read_text(encoding="utf-8", errors="replace")
    tokens = _tokenize_text(text)
    if not tokens:
        return {"id": book_id, "error": "no tokens extracted (empty/non-text?)"}

    USER_TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    USER_COUNTS_DIR.mkdir(parents=True, exist_ok=True)

    # tokens.txt: one token per line (matches SPGC layout)
    tok_path.write_text("\n".join(tokens) + "\n", encoding="utf-8")

    # counts.txt: "word\tcount" sorted by count desc, ties by word
    counter = Counter(tokens)
    with open(count_path, "w", encoding="utf-8") as fh:
        for w, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            fh.write(f"{w}\t{c}\n")

    return {
        "id": book_id,
        "tokens": len(tokens),
        "unique": len(counter),
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "tokens_file": str(tok_path),
        "counts_file": str(count_path),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", help="single book id, e.g. U7. Otherwise all u*.txt")
    ap.add_argument("--force", action="store_true",
                    help="re-tokenize even if output already present")
    args = ap.parse_args()

    if args.book:
        bids = [args.book.upper() if not args.book.upper().startswith("U")
                else args.book.upper()]
    else:
        bids = sorted(p.stem.upper() for p in RAW_DIR.glob("u*.txt"))

    if not bids:
        print("no user books found in", RAW_DIR)
        return

    print(f"[tokenize] {len(bids)} book(s)")
    total_tokens = 0
    for bid in bids:
        r = _process(bid, force=args.force)
        if "error" in r:
            print(f"  {bid:6s}  ERROR: {r['error']}")
        elif "skipped" in r:
            print(f"  {bid:6s}  skipped (already tokenized)")
        else:
            print(f"  {bid:6s}  tokens={r['tokens']:>8d}  unique={r['unique']:>6d}  "
                  f"{r['elapsed_s']:.2f}s")
            total_tokens += r["tokens"]
    print(f"[done] {total_tokens:,} tokens total")


if __name__ == "__main__":
    main()
