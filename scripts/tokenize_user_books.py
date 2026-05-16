#!/usr/bin/env python3
"""
tokenize_user_books.py — produce SPGC-compatible tokens + counts files for
locally-stored books that aren't in the SPGC 2018 dump:

  • U<N>.txt — user uploads via admin (D17). Tokens land in user_*/U<N>_*.
  • PG<N>.txt — post-2018 PG raws pulled by rsync from ibiblio (Sprint 9.5).
    These are PG IDs > ~57779 that the frozen 2018 dump doesn't know about,
    so SPGC_COUNTS_DIR/PG<N>_counts.txt simply doesn't exist. We tokenize
    the raw text into the same user_counts/ + user_tokens/ folders; the
    fallback in rag_tools._counts_path / _tokens_path picks up the user_*
    file when the SPGC one is missing.

For each book:
  - /data/spgc/user_tokens/<ID>_tokens.txt   — one lowercased alpha token per line
  - /data/spgc/user_counts/<ID>_counts.txt   — `word\tcount` lines, sorted desc

After tokenization the book becomes a first-class citizen for top_ngrams_by_author,
affinity_by_author, word_contexts, word_collocates, lexical_diversity and
word_freq_timeline.

CLI:
  # tokenize all u*.txt that don't already have tokens
  python tokenize_user_books.py

  # tokenize a single book (e.g. just uploaded U7, or orphan PG60000)
  python tokenize_user_books.py --book U7
  python tokenize_user_books.py --book PG60000

  # tokenize all orphan PG raws (post-2018 books absent from SPGC dump)
  python tokenize_user_books.py --orphan-pg

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
SPGC_COUNTS_DIR = Path("/workspace/spgc/SPGC-counts-2018-07-18")

# Same Penn-Treebank-ish tokenizer that SPGC used (lowercased alphabetic + apostrophe).
# We DON'T strip stopwords — SPGC counts keep them, our tools filter at analysis time.
TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

# Project Gutenberg header/footer markers. Several variations exist across decades
# of releases. We strip everything before the first START and after the first END,
# falling back to the full text if no markers are found (some early ebooks predate
# the standardized banner).
_PG_START_RE = re.compile(
    r"\*\*\*\s*START\s+OF\s+(?:THE|THIS)\s+PROJECT\s+GUTENBERG\s+EBOOK[^*]*\*\*\*",
    re.IGNORECASE,
)
_PG_END_RE = re.compile(
    r"\*\*\*\s*END\s+OF\s+(?:THE|THIS)\s+PROJECT\s+GUTENBERG\s+EBOOK[^*]*\*\*\*",
    re.IGNORECASE,
)


def _strip_pg_boilerplate(text: str) -> tuple[str, bool]:
    """Slice off Gutenberg header/footer if present. Returns (body, stripped_bool).

    If only one marker is present (rare but exists for some incomplete dumps),
    we trim on the side we found. Both missing → return text unchanged."""
    start_m = _PG_START_RE.search(text)
    end_m = _PG_END_RE.search(text, pos=start_m.end() if start_m else 0)
    if start_m and end_m:
        return text[start_m.end():end_m.start()], True
    if start_m:
        return text[start_m.end():], True
    if end_m:
        return text[:end_m.start()], True
    return text, False


def _tokenize_text(text: str) -> list[str]:
    """Lowercased word-level tokens. Same regex SPGC tools effectively use."""
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def _src_path(book_id: str) -> Path:
    """raw_text filename for either U<N> or PG<N> id."""
    bid = book_id.upper()
    if bid.startswith("U"):
        return RAW_DIR / f"{bid.lower()}.txt"
    # PG: raws are lowercase pg<n>.txt
    return RAW_DIR / f"{bid.lower()}.txt"


def _process(book_id: str, force: bool = False) -> dict:
    """Tokenize one book (U<N> or PG<N>) → write tokens + counts. Returns stats."""
    bid = book_id.upper()
    src = _src_path(bid)
    if not src.exists():
        return {"id": bid, "error": "raw text not found", "path": str(src)}

    tok_path   = USER_TOKENS_DIR / f"{bid}_tokens.txt"
    count_path = USER_COUNTS_DIR / f"{bid}_counts.txt"
    if not force and tok_path.exists() and count_path.exists():
        return {"id": bid, "skipped": "already tokenized",
                "tokens_file": str(tok_path)}

    t0 = time.perf_counter()
    text = src.read_text(encoding="utf-8", errors="replace")

    # Strip PG header/footer for any PG-derived book (orphan or otherwise).
    # U-books from admin EPUB conversion don't carry the banner.
    stripped = False
    if bid.startswith("PG"):
        text, stripped = _strip_pg_boilerplate(text)

    tokens = _tokenize_text(text)
    if not tokens:
        return {"id": bid, "error": "no tokens extracted (empty/non-text?)"}

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
        "id": bid,
        "tokens": len(tokens),
        "unique": len(counter),
        "stripped_pg_boilerplate": stripped,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "tokens_file": str(tok_path),
        "counts_file": str(count_path),
    }


def _orphan_pg_ids() -> list[str]:
    """All pg<N>.txt in raw_text whose corresponding SPGC counts file is missing.

    Walking ~55k files takes a few seconds — fine for an offline batch."""
    orphan = []
    for p in RAW_DIR.glob("pg*.txt"):
        bid = p.stem.upper()
        if not bid.startswith("PG"):
            continue
        spgc = SPGC_COUNTS_DIR / f"{bid}_counts.txt"
        if not spgc.exists():
            orphan.append(bid)
    return sorted(orphan)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", help="single book id, e.g. U7 or PG60000. "
                    "Otherwise all u*.txt (or all orphan PG when --orphan-pg).")
    ap.add_argument("--orphan-pg", action="store_true",
                    help="walk all pg*.txt in raw_text and tokenize those missing "
                    "from SPGC_COUNTS_DIR (post-2018 PG releases).")
    ap.add_argument("--force", action="store_true",
                    help="re-tokenize even if output already present")
    args = ap.parse_args()

    if args.book:
        bids = [args.book.upper()]
    elif args.orphan_pg:
        bids = _orphan_pg_ids()
    else:
        bids = sorted(p.stem.upper() for p in RAW_DIR.glob("u*.txt"))

    if not bids:
        print("no books to tokenize")
        return

    print(f"[tokenize] {len(bids)} book(s)")
    total_tokens = 0
    total_stripped = 0
    errors = 0
    skipped = 0
    t_batch = time.perf_counter()
    # Progress every 500 books for big runs (orphan PG is ~20k).
    progress_every = 500 if len(bids) > 1000 else max(1, len(bids) // 20)
    for i, bid in enumerate(bids, 1):
        r = _process(bid, force=args.force)
        if "error" in r:
            errors += 1
            print(f"  {bid:10s}  ERROR: {r['error']}", flush=True)
        elif "skipped" in r:
            skipped += 1
        else:
            total_tokens += r["tokens"]
            if r.get("stripped_pg_boilerplate"):
                total_stripped += 1
        if i % progress_every == 0 or i == len(bids):
            elapsed = time.perf_counter() - t_batch
            rate = i / elapsed if elapsed else 0
            print(f"[progress] {i}/{len(bids)} · {rate:.1f} books/s · "
                  f"{total_tokens:,} tokens · {errors} errors · "
                  f"{skipped} already done", flush=True)
    print(f"[done] {total_tokens:,} tokens, {total_stripped} with PG boilerplate "
          f"stripped, {errors} errors, {skipped} skipped (already tokenized)")


if __name__ == "__main__":
    main()
