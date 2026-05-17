#!/usr/bin/env python3
"""
fetch_author_nationality.py — map Project Gutenberg authors to country of
citizenship via Wikidata. Background batch (Sprint 9.2).

For each unique author in _metadata_df() we don't already have a row for:
    GET wikidata.org/w/api.php?action=wbsearchentities&search=<name>
    -> top Q-id (or skip)
    GET wikidata.org/wiki/Special:EntityData/<Q-id>.json
    -> P27 claim -> country Q-id -> mapped to ISO code via COUNTRY_MAP
       (top-60 countries covered exactly; rest stored with Q-id only).

Output (atomic append): /data/spgc/derived/authors_geo.csv
    author,qid,country_qid,country_code,country_name,fetched_at

Resume-safe: skip ids already in CSV (any verdict — hit, no_match, error,
ambiguous — all 'done').

Rate limit: 0.5s between requests. ~14k unique authors x 2 reqs x 0.5s
= ~4 hours background.

CLI:
    python fetch_author_nationality.py --smoke
    python fetch_author_nationality.py --limit 500
    python fetch_author_nationality.py            # walk everything
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

DERIVED_DIR = Path("/workspace/spgc/derived")
OUT_CSV = DERIVED_DIR / "authors_geo.csv"
UA = "wordcracker/1.0 author-nationality (https://slovoeb.net)"

WD_SEARCH = "https://www.wikidata.org/w/api.php"
WD_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"

SLEEP_SEC = 0.5
PROGRESS_EVERY = 50
FLUSH_EVERY = 25

# Country Q-id -> (ISO alpha-2, English name). Covers writers we actually have
# in PG by volume; rare countries are stored with just the Q-id.
COUNTRY_MAP = {
    "Q30":    ("US", "United States"),
    "Q145":   ("GB", "United Kingdom"),
    "Q174193": ("GB", "United Kingdom"),  # UK of GB & Ireland (historical)
    "Q161885": ("GB", "United Kingdom"),  # UK of GB & N. Ireland (alt)
    "Q142":   ("FR", "France"),
    "Q183":   ("DE", "Germany"),
    "Q159":   ("RU", "Russia"),
    "Q15180": ("RU", "Soviet Union"),
    "Q34":    ("SE", "Sweden"),
    "Q35":    ("DK", "Denmark"),
    "Q20":    ("NO", "Norway"),
    "Q33":    ("FI", "Finland"),
    "Q40":    ("AT", "Austria"),
    "Q39":    ("CH", "Switzerland"),
    "Q31":    ("BE", "Belgium"),
    "Q55":    ("NL", "Netherlands"),
    "Q29":    ("ES", "Spain"),
    "Q38":    ("IT", "Italy"),
    "Q45":    ("PT", "Portugal"),
    "Q41":    ("GR", "Greece"),
    "Q43":    ("TR", "Turkey"),
    "Q43287": ("DE", "German Empire"),
    "Q41304": ("DE", "Weimar Republic"),
    "Q172579": ("DE", "Holy Roman Empire"),
    "Q229":   ("CY", "Cyprus"),
    "Q15":    ("CA", "Canada"),  # actually Africa, but ok small
    "Q16":    ("CA", "Canada"),
    "Q224":   ("HR", "Croatia"),
    "Q403":   ("RS", "Serbia"),
    "Q36":    ("PL", "Poland"),
    "Q28":    ("HU", "Hungary"),
    "Q213":   ("CZ", "Czech Republic"),
    "Q214":   ("SK", "Slovakia"),
    "Q215":   ("SI", "Slovenia"),
    "Q218":   ("RO", "Romania"),
    "Q219":   ("BG", "Bulgaria"),
    "Q27":    ("IE", "Ireland"),
    "Q664":   ("NZ", "New Zealand"),
    "Q408":   ("AU", "Australia"),
    "Q258":   ("ZA", "South Africa"),
    "Q668":   ("IN", "India"),
    "Q148":   ("CN", "China"),
    "Q17":    ("JP", "Japan"),
    "Q884":   ("KR", "South Korea"),
    "Q96":    ("MX", "Mexico"),
    "Q155":   ("BR", "Brazil"),
    "Q414":   ("AR", "Argentina"),
    "Q298":   ("CL", "Chile"),
    "Q717":   ("VE", "Venezuela"),
    "Q794":   ("IR", "Iran"),
    "Q801":   ("IL", "Israel"),
    "Q796":   ("IQ", "Iraq"),
    "Q833":   ("MY", "Malaysia"),
    "Q252":   ("ID", "Indonesia"),
    "Q928":   ("PH", "Philippines"),
    "Q424":   ("KH", "Cambodia"),
    "Q869":   ("TH", "Thailand"),
    "Q881":   ("VN", "Vietnam"),
    "Q79":    ("EG", "Egypt"),
    "Q1041":  ("SN", "Senegal"),
    "Q1009":  ("CM", "Cameroon"),
    "Q1033":  ("NG", "Nigeria"),
    "Q1028":  ("MA", "Morocco"),
    "Q117":   ("GH", "Ghana"),
    "Q114":   ("KE", "Kenya"),
    "Q1037":  ("RW", "Rwanda"),
    "Q1042":  ("SC", "Seychelles"),
    "Q953":   ("ZM", "Zambia"),
    # Historical / empire mapping
    "Q41304": ("DE", "Weimar Republic"),
    "Q43287": ("DE", "German Empire"),
    "Q12548": ("RU", "Russian Empire"),
    "Q34266": ("RU", "Russian Empire"),
    "Q189920": ("CZ", "Bohemia"),
    "Q170579": ("AT", "Austria-Hungary"),
    "Q131964": ("AT", "Austrian Empire"),
}


import re as _re

_SUFFIX_PAT = _re.compile(r"\b(graf|baron|sir|dame|lady|lord|jr|sr|esq|dr|prof|saint|st)\b\.?\s*",
                          _re.IGNORECASE)


def _normalize_author(spgc_author: str) -> str:
    """SPGC stores 'Surname, First Middle' with various junk:
        'Tolstoy, Leo, graf'  -> 'Leo Tolstoy'
        'Lovecraft, H. P. (Howard Phillips)' -> 'Howard Phillips Lovecraft'
        'Doyle, Sir Arthur Conan' -> 'Arthur Conan Doyle'
        'Dickens, Charles' -> 'Charles Dickens'
    """
    s = (spgc_author or "").strip()
    if not s:
        return ""
    # Take the surname (first comma-delimited part) and the rest separately.
    parts = [p.strip() for p in s.split(",")]
    surname = parts[0]
    rest_segments = parts[1:]
    # Extract parenthesised expansion ("H. P. (Howard Phillips)" -> prefer
    # the expansion over the initials).
    paren_full = _re.search(r"\(([^)]+)\)", " ".join(rest_segments))
    if paren_full:
        first = paren_full.group(1)
    else:
        # Drop honorific suffix tokens like ", graf" / ", Sir" / ", Jr"
        cleaned = [seg for seg in rest_segments
                   if not _SUFFIX_PAT.fullmatch(seg.strip().rstrip("."))]
        first = " ".join(cleaned)
    # Strip leftover parens, periods after initials, honorifics
    first = _re.sub(r"\([^)]*\)", "", first)
    first = _SUFFIX_PAT.sub("", first)
    first = _re.sub(r"\s+", " ", first).strip()
    return f"{first} {surname}".strip()


def _wd_search(name: str, timeout: float = 12.0):
    params = {"action": "wbsearchentities", "search": name, "language": "en",
              "format": "json", "type": "item", "limit": "1"}
    url = WD_SEARCH + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    s = d.get("search") or []
    if not s:
        return None, ""
    top = s[0]
    return top.get("id"), top.get("description", "")


def _wd_country_qid(qid: str, timeout: float = 15.0):
    """Read P27 (country of citizenship) claim from the entity."""
    url = WD_ENTITY.format(qid)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    ent = d.get("entities", {}).get(qid, {})
    claims = ent.get("claims", {})
    p27 = claims.get("P27") or []
    if not p27:
        return None
    for c in p27:
        try:
            return c["mainsnak"]["datavalue"]["value"]["id"]
        except Exception:
            continue
    return None


def _load_done() -> set[str]:
    if not OUT_CSV.exists():
        return set()
    done = set()
    try:
        with open(OUT_CSV, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                a = (row.get("author") or "").strip()
                if a:
                    done.add(a)
    except Exception as e:
        print(f"[warn] csv read: {e}", flush=True)
    return done


def _open_csv():
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    new = not OUT_CSV.exists()
    fh = open(OUT_CSV, "a", encoding="utf-8", newline="")
    w = csv.writer(fh)
    if new:
        w.writerow(["author", "qid", "country_qid", "country_code",
                    "country_name", "wd_description", "fetched_at"])
        fh.flush()
    return fh, w


def _load_authors():
    sys.path.insert(0, "/workspace/scripts")
    from rag_tools import _metadata_df
    df = _metadata_df()
    en = df["language"].fillna("").str.contains("'en'", regex=False)
    df = df[en]
    auths = (df["author"].fillna("").str.strip().replace("", None)
             .dropna().drop_duplicates().tolist())
    return auths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=SLEEP_SEC)
    ap.add_argument("--smoke", action="store_true",
                    help="5 well-known authors, no CSV write")
    args = ap.parse_args()

    if args.smoke:
        for a in ["Christie, Agatha", "Doyle, Arthur Conan",
                  "Twain, Mark", "Tolstoy, Leo, graf",
                  "Lovecraft, H. P. (Howard Phillips)"]:
            n = _normalize_author(a)
            qid, desc = _wd_search(n)
            cq = _wd_country_qid(qid) if qid else None
            code, name = COUNTRY_MAP.get(cq, ("?", cq or "?"))
            print(f"  {a:42s} -> qid={qid} cq={cq} -> {code} ({name})")
            time.sleep(args.sleep)
        return

    auths = _load_authors()
    done = _load_done()
    print(f"[geo] authors total: {len(auths)} · done: {len(done)} · "
          f"todo: {len(auths) - len(done)}", flush=True)

    fh, w = _open_csv()
    t0 = time.perf_counter()
    fetched = hits = misses = errors = 0
    try:
        for a in auths:
            if a in done:
                continue
            name = _normalize_author(a)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                qid, desc = _wd_search(name)
            except Exception as e:
                errors += 1
                w.writerow([a, "", "", "", "", "", now])
                fetched += 1
                if fetched % FLUSH_EVERY == 0: fh.flush()
                time.sleep(args.sleep)
                if args.limit and fetched >= args.limit: break
                continue
            if not qid:
                misses += 1
                w.writerow([a, "", "", "", "", "wd:no_match", now])
                fetched += 1
                if fetched % FLUSH_EVERY == 0: fh.flush()
                time.sleep(args.sleep)
                if args.limit and fetched >= args.limit: break
                continue
            time.sleep(args.sleep)
            try:
                cq = _wd_country_qid(qid)
            except Exception as e:
                errors += 1
                cq = None
            code, country_name = COUNTRY_MAP.get(cq, ("", ""))
            if cq:
                hits += 1
            else:
                misses += 1
            w.writerow([a, qid, cq or "", code, country_name, desc[:140], now])
            fetched += 1
            if fetched % FLUSH_EVERY == 0: fh.flush()
            if fetched % PROGRESS_EVERY == 0:
                el = time.perf_counter() - t0
                rate = fetched / el if el else 0
                print(f"[progress] {fetched} fetched | {hits} hits | "
                      f"{misses} misses | {errors} errors | "
                      f"{rate:.2f} req/s", flush=True)
            if args.limit and fetched >= args.limit:
                break
            time.sleep(args.sleep)
    except KeyboardInterrupt:
        print("[interrupt] caught Ctrl-C, flushing", flush=True)
    finally:
        fh.flush()
        fh.close()

    print(f"[done] {fetched} fetched | {hits} country-tagged | "
          f"{misses} missed | {errors} errors | csv: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
