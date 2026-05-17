#!/usr/bin/env python3
"""
fetch_pub_year.py — enrich the merged metadata frame with real publication
years from Open Library, one slow polite background batch.

For each book in _metadata_df() we don't already have a pub_year for, query
    GET https://openlibrary.org/search.json?title=<>&author=<>&limit=1

Take `first_publish_year` from the top match. Filter:
  - Must be plausible (> author birth, < 2030, > 1400).
  - Skip if title shorter than 3 chars or starts with "Vol", "No.", "Bd."
    (these are usually too generic to disambiguate via OL).

Output (atomic append): /data/spgc/derived/pub_year_enrichment.csv
    id,pub_year,source,fetched_at

Resume-safe: re-runs skip ids already present in the output CSV.

Rate limit: ~1 request / second (Open Library polite policy).
ETA: ~60k books × 1.5s = ~25 hours background.

CLI:
    # tackle the next 1000 unenriched ids (use to smoke-test)
    python fetch_pub_year.py --limit 1000

    # walk every unenriched id; run in background
    python fetch_pub_year.py
"""
import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Container-side defaults; host-side ones if we want to run via python3 directly
DERIVED_DIR = Path("/workspace/spgc/derived")
OUT_CSV     = DERIVED_DIR / "pub_year_enrichment.csv"

OL_BASE = "https://openlibrary.org/search.json"
UA      = "wordcracker/1.0 pub-year-fetcher (https://slovoeb.net)"
SLEEP_SEC = 1.0          # polite — ~1 req/sec
PROGRESS_EVERY = 50      # books between progress prints
FLUSH_EVERY    = 25      # books between CSV flushes (resume granularity)

_TITLE_BLOCK_PREFIXES = (
    "vol ", "vol.", "volume ",
    "no ", "no.",
    "bd ", "bd.",
    "tome ", "tom.",
    "part ",
)


def _load_done_ids() -> set[str]:
    """Set of book ids we already enriched (any row, even pub_year=NULL)."""
    if not OUT_CSV.exists():
        return set()
    done = set()
    try:
        with open(OUT_CSV, encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            for row in rd:
                bid = (row.get("id") or "").strip()
                if bid:
                    done.add(bid)
    except Exception as e:
        print(f"[warn] failed to read existing enrichment csv: {e}", flush=True)
    return done


def _writable_csv():
    """Open the output CSV in append mode, writing header if new."""
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    new = not OUT_CSV.exists()
    fh = open(OUT_CSV, "a", encoding="utf-8", newline="")
    w = csv.writer(fh)
    if new:
        w.writerow(["id", "pub_year", "source", "fetched_at"])
        fh.flush()
    return fh, w


def _is_skippable_title(title: str) -> bool:
    if not title or len(title.strip()) < 3:
        return True
    t = title.strip().lower()
    return any(t.startswith(p) for p in _TITLE_BLOCK_PREFIXES)


def _author_surname(author: str) -> str:
    """SPGC stores authors as 'Surname, First Middle'. OL likes plain 'Surname'.
    Take everything before the first comma."""
    if not author:
        return ""
    return author.split(",", 1)[0].strip()


def _ol_search(title: str, author: str, timeout: float = 15.0) -> dict | None:
    """Single OL query. Returns top result dict or None."""
    q = {"title": title}
    if author:
        q["author"] = author
    q["limit"] = "1"
    q["fields"] = "key,title,first_publish_year,author_name"
    url = OL_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}
    docs = data.get("docs") or []
    if not docs:
        return None
    return docs[0]


def _validate_pub_year(pub_year, author_birth):
    """Plausibility filter. Returns (year_int_or_None, reason)."""
    if pub_year is None:
        return None, "no first_publish_year"
    try:
        y = int(pub_year)
    except (TypeError, ValueError):
        return None, f"non-int year: {pub_year!r}"
    if y < 1400 or y > 2030:
        return None, f"out of range: {y}"
    if author_birth is not None:
        try:
            yob = int(float(author_birth))
            if y < yob + 10:
                # OL sometimes returns a much-earlier edition that predates
                # the author. Reject — falls back to birth+30 proxy at query time.
                return None, f"earlier than author birth+10: pub={y}, yob={yob}"
        except (TypeError, ValueError):
            pass
    return y, ""


def _load_metadata():
    """Lazy import the merged frame so this script can run standalone or in
    container alongside chat. Uses _metadata_df from rag_tools."""
    sys.path.insert(0, "/workspace/scripts")
    from rag_tools import _metadata_df
    return _metadata_df()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many newly-fetched books (0 = no limit)")
    ap.add_argument("--sleep", type=float, default=SLEEP_SEC,
                    help=f"polite delay between requests (default {SLEEP_SEC}s)")
    ap.add_argument("--smoke", action="store_true",
                    help="just do 5 well-known books and print, don't write CSV")
    args = ap.parse_args()

    if args.smoke:
        for title, author, expect in [
            ("Crime and Punishment", "Dostoyevsky", 1866),
            ("Pride and Prejudice",  "Austen", 1813),
            ("Dracula",              "Stoker", 1897),
            ("Adventures of Sherlock Holmes", "Doyle", 1892),
            ("The Importance of Being Earnest", "Wilde", 1895),
        ]:
            r = _ol_search(title, author)
            print(f"  {title:42s} | {author:16s} | got={r.get('first_publish_year') if r else '?'} | want ≈{expect}")
            time.sleep(args.sleep)
        return

    df = _load_metadata()
    if df is None or not len(df):
        print("metadata empty, aborting")
        return

    # English only — OL multilingual results dilute single-author signals.
    mask_en = df["language"].fillna("").str.contains("'en'", regex=False)
    df = df[mask_en].copy()
    df = df.drop_duplicates(subset="id")
    print(f"[fetch_pub_year] candidate corpus: {len(df):,} EN books", flush=True)

    done = _load_done_ids()
    print(f"[fetch_pub_year] already enriched: {len(done):,}", flush=True)

    fh, w = _writable_csv()
    t_batch = time.perf_counter()
    fetched = 0
    hits = 0
    rejects = 0
    errors = 0

    try:
        for _, row in df.iterrows():
            bid = str(row["id"])
            if bid in done:
                continue
            title = (row.get("title") or "").strip()
            if _is_skippable_title(title):
                w.writerow([bid, "", "skip:title", datetime.now(timezone.utc).isoformat(timespec="seconds")])
                rejects += 1
                fetched += 1
                if fetched % FLUSH_EVERY == 0:
                    fh.flush()
                continue
            author = _author_surname(row.get("author") or "")
            doc = _ol_search(title, author)
            if doc is None:
                w.writerow([bid, "", "ol:no_match", datetime.now(timezone.utc).isoformat(timespec="seconds")])
                rejects += 1
            elif "_error" in doc:
                errors += 1
                w.writerow([bid, "", f"ol:err:{doc['_error'][:40]}", datetime.now(timezone.utc).isoformat(timespec="seconds")])
            else:
                pub_year, reason = _validate_pub_year(
                    doc.get("first_publish_year"),
                    row.get("authoryearofbirth"),
                )
                if pub_year:
                    w.writerow([bid, pub_year, "open_library",
                                datetime.now(timezone.utc).isoformat(timespec="seconds")])
                    hits += 1
                else:
                    w.writerow([bid, "", f"ol:reject:{reason[:40]}",
                                datetime.now(timezone.utc).isoformat(timespec="seconds")])
                    rejects += 1
            fetched += 1
            if fetched % FLUSH_EVERY == 0:
                fh.flush()
            if fetched % PROGRESS_EVERY == 0:
                elapsed = time.perf_counter() - t_batch
                rate = fetched / elapsed if elapsed else 0
                print(f"[progress] {fetched} fetched · {hits} hits · "
                      f"{rejects} rejects · {errors} errors · {rate:.2f} req/s",
                      flush=True)
            if args.limit and fetched >= args.limit:
                break
            time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("[interrupt] caught Ctrl-C; flushing csv", flush=True)
    finally:
        fh.flush()
        fh.close()

    print(f"[done] {fetched} fetched | {hits} hits | {rejects} rejects | "
          f"{errors} errors | csv: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
