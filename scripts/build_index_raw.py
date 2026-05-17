#!/usr/bin/env python3
"""
Build ChromaDB semantic index over /data/raw_text/ (pg<id>.txt files).

Metadata is looked up in SPGC-metadata-2018-07-18.csv by PG id. Chunks
are added in batches; embeddings run on GPU via SentenceTransformer
'paraphrase-multilingual-MiniLM-L12-v2' (384-d, 50+ languages incl. Russian).

Optional --author regex narrows the books indexed (e.g. '^Wodehouse,'
for a quick test pass before indexing the whole corpus).

Usage:
  python build_index_raw.py \
    --raw-dir   /workspace/raw_text \
    --metadata  /workspace/spgc/SPGC-metadata-2018-07-18.csv \
    --db-path   /workspace/chroma_db \
    --collection gutenberg-index \
    --batch     256
"""
import argparse
import json
import re
import time
from pathlib import Path

import chromadb
import pandas as pd
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from tqdm import tqdm

GUTENBERG_HEADER = re.compile(r"\*\*\* START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.IGNORECASE | re.DOTALL)
GUTENBERG_FOOTER = re.compile(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG.*", re.IGNORECASE | re.DOTALL)


def _resume_cache_path(metadata_path: Path) -> Path:
    return metadata_path.parent / "derived" / "build_index_already.json"


def _load_resume_cache(metadata_path: Path, current_count: int) -> set | None:
    """If the cache file matches the current ChromaDB count, return its
    set of book ids and skip the (slow) coll.get(include=[]) call.

    Returns None if the cache is missing or stale — caller falls back."""
    cp = _resume_cache_path(metadata_path)
    if not cp.exists():
        return None
    try:
        with open(cp, encoding="utf-8") as fh:
            data = json.load(fh)
        if int(data.get("chroma_count", -1)) != int(current_count):
            return None
        books = data.get("books") or []
        return set(books)
    except Exception as e:
        print(f"  [resume-cache] read failed: {e}", flush=True)
        return None


def _save_resume_cache(metadata_path: Path, current_count: int, books: set) -> None:
    cp = _resume_cache_path(metadata_path)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        with open(cp, "w", encoding="utf-8") as fh:
            json.dump({"chroma_count": int(current_count),
                       "books": sorted(books)}, fh)
    except Exception as e:
        print(f"  [resume-cache] write failed: {e}", flush=True)


def strip_gutenberg(text: str) -> str:
    m = GUTENBERG_HEADER.search(text)
    if m:
        text = text[m.end():]
    m = GUTENBERG_FOOTER.search(text)
    if m:
        text = text[:m.start()]
    return text


def chunk_words(text: str, max_words: int = 200):
    words = text.split()
    for i in range(0, len(words), max_words):
        yield i, " ".join(words[i:i + max_words])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir",   required=True, type=Path)
    ap.add_argument("--metadata",  required=True, type=Path)
    ap.add_argument("--db-path",   required=True, type=str)
    ap.add_argument("--collection", default="gutenberg-index")
    ap.add_argument("--author", default=None, help="regex against metadata.author column to filter books")
    ap.add_argument("--lang",   default="en")
    ap.add_argument("--max-words", type=int, default=500)
    ap.add_argument("--embedder", default="paraphrase-multilingual-MiniLM-L12-v2",
                    help="sentence-transformers model name (384-d default works as drop-in)")
    ap.add_argument("--batch",     type=int, default=256)
    ap.add_argument("--limit-books", type=int, default=None, help="cap on book count (for dry-run)")
    ap.add_argument("--reset", action="store_true", help="drop and recreate the collection first")
    args = ap.parse_args()

    print(f"[meta] loading {args.metadata}")
    df = pd.read_csv(args.metadata).set_index("id")
    print(f"  {len(df)} SPGC rows")

    # Merge user_uploads_metadata.csv (admin-uploaded books with U<N> ids).
    # The CSV mirrors SPGC's schema so concat works cleanly.
    user_meta_path = args.metadata.parent / "derived" / "user_uploads_metadata.csv"
    if user_meta_path.exists():
        try:
            udf = pd.read_csv(user_meta_path).set_index("id")
            # keep only columns present in SPGC; pad missing with NA
            for col in df.columns:
                if col not in udf.columns:
                    udf[col] = pd.NA
            udf = udf[df.columns]
            df = pd.concat([df, udf])
            print(f"  + {len(udf)} user upload rows → {len(df)} total")
        except Exception as e:
            print(f"  WARN: failed to load user uploads metadata: {e}")

    sel = df[df["language"].fillna("").str.contains(f"'{args.lang}'", regex=False)]
    if args.author:
        sel = sel[sel["author"].fillna("").str.contains(args.author, case=False, regex=True)]
    print(f"  {len(sel)} rows after lang='{args.lang}' author~='{args.author}'")

    # which raw files do we actually have on disk?
    have = {}
    for p in list(args.raw_dir.glob("pg*.txt")) + list(args.raw_dir.glob("u*.txt")):
        have[p.stem] = p  # 'pg2005' / 'u3' -> Path
    sel_ids = [pid for pid in sel.index if pid.lower() in have]  # pid like 'PG2005' or 'U3'
    if args.limit_books:
        sel_ids = sel_ids[:args.limit_books]
    print(f"  {len(sel_ids)} books present on disk")

    print(f"[chroma] {args.db_path}  collection={args.collection!r}")
    client = chromadb.PersistentClient(path=args.db_path)
    if args.reset:
        try:
            client.delete_collection(args.collection)
            print("  deleted existing collection")
        except Exception:
            pass
    # device='cuda' is critical -- without it sentence-transformers defaults
    # to CPU even when torch.cuda is available, ~10x slower for MiniLM.
    embed_fn = SentenceTransformerEmbeddingFunction(model_name=args.embedder, device="cuda")
    coll = client.get_or_create_collection(
        name=args.collection, embedding_function=embed_fn,
        metadata={"creator": "wordcracker", "source": "raw_text"})
    print(f"  starting count: {coll.count()}", flush=True)

    # Resume: skip books whose chunks are already indexed.
    # Fast path: if /data/.../derived/build_index_already.json matches the
    # current Chroma count, read the book set from disk (≈10ms). Slow path:
    # coll.get(include=[]) and parse chunk suffixes — 20-30s on 3.8M chunks.
    # Saves ~30s on every admin-triggered incremental reindex.
    already_books = set()
    if not args.reset:
        current_count = coll.count()
        t_rc = time.perf_counter()
        cached = _load_resume_cache(args.metadata, current_count)
        if cached is not None:
            already_books = cached
            print(f"  [resume-cache] hit ({len(already_books)} books, "
                  f"{int((time.perf_counter()-t_rc)*1000)} ms)", flush=True)
        else:
            print(f"  [resume-cache] miss, scanning collection...", flush=True)
            existing = coll.get(include=[])
            for eid in existing["ids"]:
                already_books.add(eid.rsplit("_", 1)[0])
            _save_resume_cache(args.metadata, current_count, already_books)
            print(f"  already indexed books: {len(already_books)} "
                  f"(took {time.perf_counter()-t_rc:.1f}s, cache rebuilt)",
                  flush=True)

    buf_docs, buf_ids, buf_meta = [], [], []

    def flush():
        if not buf_docs:
            return
        coll.add(documents=buf_docs, ids=buf_ids, metadatas=buf_meta)
        buf_docs.clear(); buf_ids.clear(); buf_meta.clear()

    total_chunks = 0
    skipped = 0
    for pid in tqdm(sel_ids, desc="books", unit="book", file=__import__("sys").stdout, mininterval=2.0):
        if pid in already_books:
            skipped += 1
            continue
        meta = sel.loc[pid]
        path = have[pid.lower()]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        text = strip_gutenberg(text)
        author = str(meta.get("author") or "")
        title  = str(meta.get("title")  or "")
        year   = meta.get("authoryearofbirth")
        for chunk_idx, chunk in chunk_words(text, args.max_words):
            buf_docs.append(chunk)
            buf_ids.append(f"{pid}_{chunk_idx}")
            buf_meta.append({
                "pg_id":  pid,
                "author": author,
                "title":  title,
                "year":   int(year) if pd.notna(year) else 0,
                "chunk":  chunk_idx,
            })
            total_chunks += 1
            if len(buf_docs) >= args.batch:
                flush()
    flush()

    final_count = coll.count()
    new_books = {pid for pid in sel_ids if pid not in already_books}
    if new_books and not args.reset:
        already_books |= new_books
        _save_resume_cache(args.metadata, final_count, already_books)
        print(f"  [resume-cache] updated with {len(new_books)} new books", flush=True)
    print(f"[done] added {total_chunks:,} chunks across {len(sel_ids) - skipped} new books "
          f"(skipped {skipped} already indexed)", flush=True)
    print(f"  collection now has: {final_count:,} documents", flush=True)


if __name__ == "__main__":
    main()
