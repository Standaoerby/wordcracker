#!/usr/bin/env python3
"""Build a SQLite FTS5 lexical index over /data/raw_text/*.txt.

Two-table schema:
  documents       — one row per book with full text, used for snippets
  documents_fts   — FTS5 contentless index on the same text, used for ranked
                    word/phrase matching

BM25 is built into FTS5 (`bm25()` function), so ranking is free. We don't
ship a tokenizer override — the default tokenizer ("unicode61") works fine
for English literature and strips diacritics.

Run inside the container (volume /data is bind-mounted as /workspace):

    docker compose exec -T gutenberg-lab \
      python -u /workspace/scripts/v2/build_fts_index.py \
        --raw-dir /workspace/raw_text \
        --out /workspace/v2_fts.sqlite

Notes:
- The script is RESUMABLE. It records each indexed book's id + mtime in the
  `documents` table; subsequent runs skip files whose mtime hasn't changed.
- Build time on 3090 host with NVMe: ~25-35 min for 55k books / 23 GB raw.
- Resulting DB is ~6-8 GB (plain text + FTS5 trigrams).
- Use --limit N for a smoke test on a subset.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    id      TEXT PRIMARY KEY,    -- PG1342 / U7 ...
    path    TEXT NOT NULL,
    mtime   REAL NOT NULL,
    text    TEXT NOT NULL,
    n_bytes INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    text,
    content='documents',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
    INSERT INTO documents_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text)
        VALUES('delete', old.rowid, old.text);
END;
"""


def _book_id(path: Path) -> str:
    """pg1342.txt → PG1342, u7.txt → U7"""
    stem = path.stem
    if stem.startswith("pg"):
        return "PG" + stem[2:]
    if stem.startswith("u"):
        return "U" + stem[1:]
    return stem.upper()


def _strip_gutenberg_markers(text: str) -> str:
    """Drop the bulky Gutenberg header/footer so BM25 ranking isn't poisoned
    by every match getting credit for boilerplate. Cheap delimiter probe."""
    start_markers = (
        "*** START OF THIS PROJECT GUTENBERG",
        "*** START OF THE PROJECT GUTENBERG",
        "***START OF THE PROJECT GUTENBERG",
    )
    end_markers = (
        "*** END OF THIS PROJECT GUTENBERG",
        "*** END OF THE PROJECT GUTENBERG",
        "***END OF THE PROJECT GUTENBERG",
    )
    lo = 0
    for m in start_markers:
        i = text.find(m)
        if i >= 0:
            j = text.find("\n", i)
            if j >= 0:
                lo = j + 1
            break
    hi = len(text)
    for m in end_markers:
        i = text.find(m, lo)
        if i >= 0:
            hi = i
            break
    return text[lo:hi]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default="/workspace/raw_text")
    ap.add_argument("--out", default="/workspace/v2_fts.sqlite")
    ap.add_argument("--limit", type=int, default=0,
                    help="0 = all books; positive = smoke-test subset")
    ap.add_argument("--reset", action="store_true",
                    help="drop existing tables before building")
    ap.add_argument("--strip-headers", action="store_true", default=True,
                    help="strip PG boilerplate (on by default)")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    if not raw_dir.exists():
        print(f"ERROR: raw_dir {raw_dir} doesn't exist", file=sys.stderr)
        return 1

    files = sorted(raw_dir.glob("*.txt"))
    if args.limit > 0:
        files = files[:args.limit]
    print(f"[fts] {len(files)} candidate files in {raw_dir}", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if args.reset:
        conn.executescript(
            "DROP TRIGGER IF EXISTS documents_ai;"
            "DROP TRIGGER IF EXISTS documents_au;"
            "DROP TRIGGER IF EXISTS documents_ad;"
            "DROP TABLE IF EXISTS documents_fts;"
            "DROP TABLE IF EXISTS documents;"
        )
    conn.executescript(DDL)

    # Pre-load existing (id, mtime) so resumable runs skip up-to-date files.
    existing = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, mtime FROM documents")
    }
    print(f"[fts] {len(existing)} books already indexed", flush=True)

    t_start = time.perf_counter()
    n_done = n_skipped = n_failed = 0
    BATCH = 200
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        conn.executemany(
            "INSERT OR REPLACE INTO documents (id, path, mtime, text, n_bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            pending,
        )
        pending = []

    for i, path in enumerate(files, 1):
        bid = _book_id(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            n_failed += 1
            continue
        if existing.get(bid) == mtime:
            n_skipped += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            n_failed += 1
            continue
        if args.strip_headers:
            text = _strip_gutenberg_markers(text)
        if not text.strip():
            n_failed += 1
            continue
        pending.append((bid, str(path), mtime, text, len(text.encode("utf-8"))))
        n_done += 1
        if len(pending) >= BATCH:
            flush()
            elapsed = time.perf_counter() - t_start
            rate = n_done / elapsed if elapsed > 0 else 0
            print(f"[fts] {n_done:5d}/{len(files)-len(existing)} "
                  f"({n_skipped} skipped) "
                  f"@ {rate:.1f} books/s, "
                  f"elapsed {elapsed:.0f}s", flush=True)

    flush()
    elapsed = time.perf_counter() - t_start
    print(f"[fts] done: {n_done} indexed, {n_skipped} unchanged, "
          f"{n_failed} failed in {elapsed:.0f}s", flush=True)

    # Optimize the FTS index for read-heavy access.
    print("[fts] running optimize...", flush=True)
    t_opt = time.perf_counter()
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
    print(f"[fts] optimize done in {time.perf_counter()-t_opt:.0f}s", flush=True)

    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    print(f"[fts] total documents in index: {total}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
