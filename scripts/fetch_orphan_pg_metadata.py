#!/usr/bin/env python3
"""
fetch_orphan_pg_metadata.py — fill in SPGC-shaped metadata for raw_text/pg*.txt
files that aren't in the SPGC-2018-07-18 dump.

After rsync against ibiblio we have ~55k PG books on disk but SPGC metadata
only covers ~47k (frozen 2018). The ~8-24k orphans are post-2018 releases
(or earlier ones missed by SPGC). They have raw text but no title/author/
year/language, so:
- _select_books() can't find them by author regex
- top_authors_by skips them
- author_metadata returns 'no books matched'
- semantic_search only retrieves them with empty metadata strings

This script queries Gutendex (https://gutendex.com/books?ids=...) in batches
of ~32 ids per request, parses to SPGC schema (id / title / author /
authoryearofbirth / authoryearofdeath / language / downloads / subjects /
type), and appends to /workspace/spgc/derived/orphan_pg_metadata.csv.

rag_tools._metadata_df() then merges three sources transparently:
  SPGC dump  ∪  user_uploads_metadata  ∪  orphan_pg_metadata

CLI:
  # fetch metadata for ALL orphan pg ids on disk
  python fetch_orphan_pg_metadata.py

  # try a specific batch (testing)
  python fetch_orphan_pg_metadata.py --ids 1342,2701,11

  # resume after Ctrl-C — already-fetched ids are skipped
  python fetch_orphan_pg_metadata.py    # re-run, idempotent

  # force refresh of a known id
  python fetch_orphan_pg_metadata.py --ids 1342 --force
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

import requests

RAW_DIR     = Path("/workspace/raw_text")
SPGC_META   = Path("/workspace/spgc/SPGC-metadata-2018-07-18.csv")
ORPHAN_META = Path("/workspace/spgc/derived/orphan_pg_metadata.csv")
GUTENDEX    = "https://gutendex.com/books"

# SPGC schema columns (exactly the order used in user_uploads_metadata.csv)
COLUMNS = [
    "id", "title", "author",
    "authoryearofbirth", "authoryearofdeath",
    "language", "downloads", "subjects", "type",
]

# Gutendex politely accepts ~32 ids per `ids=` param request (URL length cap).
BATCH = 32


def _load_existing_ids() -> set[str]:
    """All PG ids we already have metadata for (SPGC + previous orphan-fetch
    runs). Used to compute the to-fetch list."""
    have: set[str] = set()
    if SPGC_META.exists():
        with open(SPGC_META, encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                if row.get("id"):
                    have.add(row["id"])
    if ORPHAN_META.exists():
        with open(ORPHAN_META, encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                if row.get("id"):
                    have.add(row["id"])
    return have


def _ondisk_pg_ids() -> set[str]:
    """All PG<N> we have raw text for."""
    out: set[str] = set()
    for p in RAW_DIR.glob("pg*.txt"):
        try:
            n = int(p.stem[2:])
            out.add(f"PG{n}")
        except ValueError:
            pass
    return out


def _gutendex_batch(ids: list[int], retries: int = 3) -> list[dict]:
    """One Gutendex request for up to BATCH ids. Returns raw `results` list.
    Polite UA + exponential backoff on transient errors."""
    params = {"ids": ",".join(str(i) for i in ids)}
    headers = {"User-Agent": "wordcracker/1.0 (NAS-uploader; +https://slovoeb.net)"}
    for attempt in range(retries):
        try:
            resp = requests.get(GUTENDEX, params=params, headers=headers, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"  rate-limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as e:
            if attempt == retries - 1:
                print(f"  FAIL batch [{ids[0]}..{ids[-1]}]: {e}")
                return []
            time.sleep(2 ** attempt)
    return []


def _row_from_gutendex(d: dict) -> dict:
    """Convert one Gutendex book record into SPGC-shaped row."""
    authors = d.get("authors") or []
    a = authors[0] if authors else {}
    langs = d.get("languages") or []
    subj  = d.get("subjects") or []
    return {
        "id":                f"PG{d.get('id', 0)}",
        "title":             (d.get("title") or "").strip(),
        "author":            a.get("name", "").strip(),
        "authoryearofbirth": a.get("birth_year") if a.get("birth_year") else "",
        "authoryearofdeath": a.get("death_year") if a.get("death_year") else "",
        "language":          repr(langs) if langs else "['en']",
        "downloads":         d.get("download_count") or 0,
        "subjects":          "; ".join(subj[:10]),
        "type":              "Text",
    }


def _append_rows(rows: list[dict]) -> None:
    """Append batch to orphan_pg_metadata.csv, creating header if first run."""
    ORPHAN_META.parent.mkdir(parents=True, exist_ok=True)
    new = not ORPHAN_META.exists()
    with open(ORPHAN_META, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated PG ids (testing). Otherwise all orphans.")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even already-known ids")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap number of ids fetched this run (default: no cap)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="sleep between batch requests (default 0.5s, safe rate)")
    args = ap.parse_args()

    have = set() if args.force else _load_existing_ids()
    if args.ids:
        wanted = [f"PG{int(i.strip())}" for i in args.ids.split(",") if i.strip()]
    else:
        ondisk = _ondisk_pg_ids()
        wanted = sorted(ondisk - have, key=lambda s: int(s[2:]))

    if args.limit:
        wanted = wanted[: args.limit]

    if not wanted:
        print("no orphan PG ids to fetch.")
        return

    print(f"[fetch] {len(wanted):,} orphan ids ({len(have):,} already known)")

    todo = [int(s[2:]) for s in wanted]
    fetched = 0
    skipped = 0
    t_start = time.time()
    batch_no = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i : i + BATCH]
        batch_no += 1
        results = _gutendex_batch(batch)
        # Gutendex may omit ids it doesn't have — track skipped explicitly.
        got = {d["id"] for d in results}
        miss = [b for b in batch if b not in got]
        rows = [_row_from_gutendex(d) for d in results]
        if rows:
            _append_rows(rows)
        fetched += len(rows)
        skipped += len(miss)
        elapsed = time.time() - t_start
        rate = fetched / max(elapsed, 0.1)
        eta = (len(todo) - i - len(batch)) / max(rate, 0.01)
        print(f"  batch {batch_no:>4d}  [{batch[0]}..{batch[-1]}]  "
              f"+{len(rows):2d}  miss={len(miss):2d}  "
              f"total fetched={fetched:,}/missed={skipped:,}  "
              f"{rate:.1f}/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s")
        time.sleep(args.delay)

    print(f"[done] fetched {fetched:,} new rows, {skipped:,} not in Gutendex, "
          f"{int((time.time() - t_start) // 60)} min total. CSV: {ORPHAN_META}")


if __name__ == "__main__":
    main()
