#!/usr/bin/env python3
"""
rag_tools.py — corpus-analytics tools exposed to the LLM agent.

Each tool:
  * Has a typed signature
  * Returns a JSON-serializable dict (or {"error": ..., "details": ...})
  * Logs its wall-clock duration to stderr

TOOLS_SPEC   — OpenAI/Ollama-compatible function schemas for tool calling
TOOL_DISPATCH — name → callable, used by rag_query.py
"""
import csv
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pandas as pd

# ---------- paths ----------
SPGC_METADATA   = Path("/workspace/spgc/SPGC-metadata-2018-07-18.csv")
SPGC_COUNTS_DIR = Path("/workspace/spgc/SPGC-counts-2018-07-18")
SPGC_TOKENS_DIR = Path("/workspace/spgc/SPGC-tokens-2018-07-18")
USER_COUNTS_DIR = Path("/workspace/spgc/user_counts")
USER_TOKENS_DIR = Path("/workspace/spgc/user_tokens")


def _counts_path(book_id: str) -> Path:
    """Return counts file path: SPGC dump for PG<N>, user_counts/ for U<N>.
    Centralizes the U-vs-PG dispatch so tools work for both without if/else
    sprayed everywhere. U-counts files are produced by tokenize_user_books.py
    on upload (called from admin_server).

    Orphan PG fallback (Sprint 9.5): post-2018 PG raws are absent from the
    frozen 2018 SPGC dump. tokenize_user_books.py --orphan-pg writes their
    counts into user_counts/PG<N>_counts.txt; we fall back to that path when
    the SPGC file doesn't exist. Callers still need to .exists()-check, but
    they get a single path that points at whichever one is present."""
    if book_id.startswith("U"):
        return USER_COUNTS_DIR / f"{book_id}_counts.txt"
    spgc = SPGC_COUNTS_DIR / f"{book_id}_counts.txt"
    if spgc.exists():
        return spgc
    return USER_COUNTS_DIR / f"{book_id}_counts.txt"


def _tokens_path(book_id: str) -> Path:
    """Same dispatch for tokens — SPGC for PG-prefix, user_tokens/ for U-prefix,
    with fallback to user_tokens/PG<N>_tokens.txt for orphan PG (Sprint 9.5)."""
    if book_id.startswith("U"):
        return USER_TOKENS_DIR / f"{book_id}_tokens.txt"
    spgc = SPGC_TOKENS_DIR / f"{book_id}_tokens.txt"
    if spgc.exists():
        return spgc
    return USER_TOKENS_DIR / f"{book_id}_tokens.txt"
CHROMA_PATH     = "/workspace/chroma_db"
COLLECTION_NAME = "gutenberg-index"
EMBEDDER_NAME   = "paraphrase-multilingual-MiniLM-L12-v2"
DERIVED_DIR     = Path("/workspace/spgc/derived")
SCRIPTS_DIR     = Path("/workspace/scripts")
CORPUS_COUNTS   = DERIVED_DIR / "corpus_counts.csv"
RAW_DIR         = Path("/workspace/raw_text")
USER_UPLOADS_META = DERIVED_DIR / "user_uploads_metadata.csv"
ORPHAN_PG_META    = DERIVED_DIR / "orphan_pg_metadata.csv"
PUB_YEAR_ENRICH   = DERIVED_DIR / "pub_year_enrichment.csv"
AUTHORS_GEO       = DERIVED_DIR / "authors_geo.csv"
OLLAMA_HOST     = os.environ.get("OLLAMA_HOST", "http://ollama:11434")

STOPWORDS = {
    "the", "a", "an", "of", "and", "to", "in", "is", "it", "that", "was", "for",
    "on", "with", "as", "at", "by", "be", "from", "this", "but", "his", "her",
    "he", "she", "they", "we", "i", "you", "had", "have", "has", "not", "or",
    "are", "were", "been", "if", "then", "so", "do", "did", "would", "could",
    "will", "shall", "may", "might", "must", "can", "no", "yes", "what", "which",
    "who", "whom", "when", "where", "why", "how", "their", "them", "our", "us",
    "my", "your", "its", "out", "up", "down", "into", "than", "now",
}

CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _log(msg: str) -> None:
    print(f"[tool] {msg}", file=sys.stderr)


def _slug(author_regex: str) -> str:
    base = author_regex.lstrip("^").split(",", 1)[0].lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_") or "author"


# ChromaDB collection singleton — loads ChromaDB persistent client +
# SentenceTransformer model on GPU exactly once per process. First call is
# slow (~30s: SentenceTransformer cold-load to cuda + chromadb hnsw open).
# Subsequent calls return the cached collection instantly. chat_server.py
# calls this at startup so the first user query doesn't pay the cold cost.
import threading as _threading
_CHROMA_COLLECTION_CACHE: dict = {"col": None}
_CHROMA_LOCK = _threading.Lock()


def _get_chroma_collection_with_embedder():
    """Return a cached ChromaDB collection with the multilingual MiniLM
    embedder bound to cuda. Thread-safe via double-checked locking."""
    if _CHROMA_COLLECTION_CACHE["col"] is not None:
        return _CHROMA_COLLECTION_CACHE["col"]
    with _CHROMA_LOCK:
        if _CHROMA_COLLECTION_CACHE["col"] is not None:
            return _CHROMA_COLLECTION_CACHE["col"]
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        embed_fn = SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDER_NAME, device="cuda"
        )
        col = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)
        _CHROMA_COLLECTION_CACHE["col"] = col
        return col


# mtime-aware cache so user uploads added by admin_server become visible to a
# long-running chat_server without needing a restart. SPGC dump never changes
# so its mtime is stable in practice; user_uploads (admin upload) and
# orphan_pg (fetch_orphan_pg_metadata.py) both grow over time.
_metadata_cache: dict = {"df": None, "spgc_mtime": 0.0,
                         "user_mtime": 0.0, "orphan_mtime": 0.0}


def _metadata_df() -> pd.DataFrame:
    spgc_m   = SPGC_METADATA.stat().st_mtime    if SPGC_METADATA.exists()   else 0.0
    user_m   = USER_UPLOADS_META.stat().st_mtime if USER_UPLOADS_META.exists() else 0.0
    orphan_m = ORPHAN_PG_META.stat().st_mtime    if ORPHAN_PG_META.exists()   else 0.0
    pub_m    = PUB_YEAR_ENRICH.stat().st_mtime   if PUB_YEAR_ENRICH.exists()  else 0.0
    if (_metadata_cache["df"] is not None
            and _metadata_cache["spgc_mtime"] == spgc_m
            and _metadata_cache["user_mtime"] == user_m
            and _metadata_cache["orphan_mtime"] == orphan_m
            and _metadata_cache.get("pub_mtime") == pub_m):
        return _metadata_cache["df"]
    df = pd.read_csv(SPGC_METADATA)

    def _merge(path, label):
        nonlocal df
        if not path.exists():
            return
        try:
            extra = pd.read_csv(path)
            for col in df.columns:
                if col not in extra.columns:
                    extra[col] = pd.NA
            extra = extra[df.columns]
            df = pd.concat([df, extra], ignore_index=True)
            _log(f"merged {len(extra)} {label} → {len(df)} total rows")
        except Exception as e:
            _log(f"failed to load {label} metadata: {e}")

    _merge(USER_UPLOADS_META, "user uploads")
    _merge(ORPHAN_PG_META,   "orphan PG (post-2018)")

    # Drop duplicate ids — SPGC wins over orphan_pg (SPGC has consistent
    # token/counts files), user uploads are unique by construction.
    if "id" in df.columns:
        df = df.drop_duplicates(subset="id", keep="first")

    # Open Library pub_year enrichment (Sprint 9.7, D31). Left-merge on id;
    # books without an OL hit get NaN and the timeline tools fall back to the
    # birth-year+30 proxy.
    if PUB_YEAR_ENRICH.exists():
        try:
            pe = pd.read_csv(PUB_YEAR_ENRICH, usecols=["id", "pub_year"])
            pe["pub_year"] = pd.to_numeric(pe["pub_year"], errors="coerce")
            pe = pe.dropna(subset=["pub_year"]).drop_duplicates(subset="id")
            df = df.merge(pe[["id", "pub_year"]], on="id", how="left")
            n_with_pub = int(df["pub_year"].notna().sum())
            _log(f"merged pub_year enrichment → {n_with_pub:,} books with real pub_year")
        except Exception as e:
            _log(f"failed to load pub_year enrichment: {e}")
            df["pub_year"] = pd.NA
    else:
        df["pub_year"] = pd.NA

    _metadata_cache.update({
        "df": df, "spgc_mtime": spgc_m,
        "user_mtime": user_m, "orphan_mtime": orphan_m,
        "pub_mtime": pub_m,
    })
    return df


_geo_cache: dict = {"df": None, "mtime": 0.0}


def _authors_geo_df():
    """Lazy-load authors_geo.csv. Grows as the Sprint 9.2 background batch
    fills it. Mtime-cached. Returns None if file absent."""
    if not AUTHORS_GEO.exists():
        return None
    m = AUTHORS_GEO.stat().st_mtime
    if _geo_cache["df"] is not None and _geo_cache["mtime"] == m:
        return _geo_cache["df"]
    try:
        g = pd.read_csv(AUTHORS_GEO)
        g = g.dropna(subset=["author"])
        # Keep only the rows where country_code is populated — the rest are
        # 'wd:no_match'/error/etc. and don't help filtering.
        g["country_code"] = g["country_code"].fillna("").astype(str).str.strip().str.upper()
        _geo_cache["df"] = g
        _geo_cache["mtime"] = m
        return g
    except Exception as e:
        _log(f"authors_geo read failed: {e}")
        return None


def _select_books(author_regex: str, lang: str = "en",
                  year_from: int | None = None,
                  year_to: int | None = None,
                  country: str | None = None) -> pd.DataFrame:
    """Books matching author regex and language.

    year_from / year_to filter on `authoryearofbirth + 30` (writing prime
    proxy). Useful for period queries like 'fog у викторианцев'
    (year_from=1837, year_to=1901).

    country: ISO alpha-2 ('GB', 'US', 'RU', 'FR', ...). Filters via
    authors_geo.csv (Wikidata enrichment, Sprint 9.2). Use to ask for
    'British vocabulary' / 'AmE literature only'. Books whose author isn't
    in the geo enrichment yet are dropped when country is set."""
    df = _metadata_df()
    mask_lang = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
    mask_auth = df["author"].fillna("").str.contains(author_regex, case=False, regex=True)
    out = df[mask_lang & mask_auth]
    if country and country.strip():
        cc = country.strip().upper()
        geo = _authors_geo_df()
        if geo is None or not len(geo):
            return out.iloc[0:0]  # empty
        ok_authors = set(geo[geo["country_code"] == cc]["author"])
        if not ok_authors:
            return out.iloc[0:0]
        out = out[out["author"].isin(ok_authors)]
    if (year_from is not None or year_to is not None) and "authoryearofbirth" in out.columns:
        yob = pd.to_numeric(out["authoryearofbirth"], errors="coerce")
        writing_prime = yob + 30
        # Strict filter — when a year range is set, drop books with unknown
        # birth year too. Mixing "Victorians" with "unknown era" would muddy
        # period analysis. Tradeoff: lose ~10-30% of books with NaN yob.
        mask_year = writing_prime.notna()
        if year_from is not None:
            mask_year &= (writing_prime >= year_from)
        if year_to is not None:
            mask_year &= (writing_prime <= year_to)
        out = out[mask_year]
    return out


def _maybe_translate(query: str) -> str:
    """If query contains Cyrillic, route through Ollama to get a canonical English form.
    Multilingual MiniLM does not transliterate proper nouns, so retrieval mistargets
    without this step (Дживс ≠ Jeeves in embedding space)."""
    if not CYRILLIC_RE.search(query):
        return query
    try:
        import requests
        prompt = (
            "Translate the following question to English. "
            "Use canonical English forms for proper nouns (Дживс→Jeeves, "
            "Берти→Bertie Wooster, Шерлок→Sherlock, Достоевский→Dostoyevsky, "
            "Толстой→Tolstoy, Чехов→Chekhov). Output ONLY the translation.\n\n"
            f"Question: {query}\n\nEnglish:"
        )
        resp = requests.post(f"{OLLAMA_HOST}/api/generate", json={
            "model": "qwen3:14b", "prompt": prompt, "stream": False, "keep_alive": 0,
            "options": {"temperature": 0}, "think": False,
        }, timeout=60)
        resp.raise_for_status()
        translated = resp.json().get("response", "").strip().strip('"').strip()
        _log(f"translated query: {translated!r}")
        return translated or query
    except Exception as e:
        _log(f"translation failed: {e}")
        return query


# ============================ TOOL 0: corpus_overview ============================
def corpus_overview() -> dict:
    """Total numbers about the corpus: raw books, indexed books, chunks, freshness gap."""
    t0 = time.perf_counter()
    out: dict = {}

    # raw_text — what's available for indexing right now
    try:
        pg_count = len(list(RAW_DIR.glob("pg*.txt")))
        user_count = len(list(RAW_DIR.glob("u*.txt")))
        out["raw_books_available"] = pg_count + user_count
        out["raw_books_pg"] = pg_count
        out["raw_books_user_uploads"] = user_count
    except Exception:
        out["raw_books_available"] = None

    # rsync source dump — what's been downloaded (may include dupes)
    try:
        out["rsync_mirror_files"] = sum(1 for _ in Path("/workspace/gutenberg_raw").rglob("*-0.txt"))
    except Exception:
        out["rsync_mirror_files"] = None

    # is rsync still running?
    try:
        proc = subprocess.run(["pgrep", "-f", "rsync.*gutenberg"], capture_output=True, text=True)
        out["rsync_running"] = bool(proc.stdout.strip())
    except Exception:
        out["rsync_running"] = None

    # ChromaDB state — only attempt count() if no reindex is racing the
    # hnsw segments. count() doesn't need an embedder.
    reindex_active = False
    try:
        proc = subprocess.run(["pgrep", "-f", "build_index_raw"], capture_output=True, text=True)
        reindex_active = bool(proc.stdout.strip())
    except Exception:
        pass
    if reindex_active:
        out["chromadb_chunks"] = "indexing in progress, count unavailable"
    else:
        try:
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            col = client.get_collection(COLLECTION_NAME)
            out["chromadb_chunks"] = col.count()
        except Exception as e:
            out["chromadb_error"] = str(e)

    # index build process — parse tqdm progress line from the most recent log
    try:
        proc = subprocess.run(["pgrep", "-f", "build_index_raw"], capture_output=True, text=True)
        out["reindex_running"] = bool(proc.stdout.strip())
        # pick the most recently modified build_index*.log
        logs = sorted(
            Path("/workspace/spgc/derived").glob("build_index*.log"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if logs:
            log = logs[0]
            out["last_index_log"] = log.name
            out["last_index_log_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(log.stat().st_mtime))
            # read last few KB and grab the most recent tqdm line
            with open(log, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", errors="ignore").replace("\r", "\n")
            tqdm_re = re.compile(
                r"books:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)\s*"
                r"\[([\d:]+)<([\d:?]+),\s*([\d.]+)\s*book/s\]"
            )
            last = None
            for line in tail.splitlines():
                m = tqdm_re.search(line)
                if m:
                    last = m
            if last:
                out["reindex_progress"] = {
                    "percent":         int(last.group(1)),
                    "books_done":      int(last.group(2)),
                    "books_total":     int(last.group(3)),
                    "elapsed":         last.group(4),
                    "eta_remaining":   last.group(5),
                    "rate_book_per_s": float(last.group(6)),
                }
    except Exception:
        pass

    # gap — only meaningful when we have an integer chunk count (not the
    # "indexing in progress" placeholder)
    if isinstance(out.get("chromadb_chunks"), int) and out.get("raw_books_available"):
        approx_indexed_books = max(1, out["chromadb_chunks"] // 125)  # ~125 chunks/book avg
        out["index_gap_approx"] = max(0, out["raw_books_available"] - approx_indexed_books)

    # SPGC baseline counts (for the affinity pipeline, separate from chromadb)
    try:
        meta_path = DERIVED_DIR / "corpus_meta.json"
        if meta_path.exists():
            out["spgc_baseline"] = json.loads(meta_path.read_text())
    except Exception:
        pass

    out["sources"] = [
        "rsync mirror ftp.ibiblio.org (current)",
        "per-author direct download from gutenberg.org/cache/epub",
        "PG DVD July 2006 local copy",
    ]
    _log(f"corpus_overview done in {time.perf_counter()-t0:.2f}s")
    return out


# ============================ TOOL 1: semantic_search ============================
def semantic_search(query: str, k: int = 8, author_filter: str | None = None) -> dict:
    """ChromaDB semantic search. Optional author_filter is a regex applied to metadata.author."""
    t0 = time.perf_counter()
    try:
        col = _get_chroma_collection_with_embedder()

        retrieval_q = _maybe_translate(query)
        # Pull more than k if filtering; we'll post-filter on author.
        fetch = k * 6 if author_filter else k
        res = col.query(query_texts=[retrieval_q], n_results=fetch)

        results = []
        for doc, md, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            author = md.get("author") or ""
            if author_filter and not re.search(author_filter, author, re.IGNORECASE):
                continue
            results.append({
                "author":   author,
                "title":    md.get("title") or "",
                "pg_id":    md.get("pg_id") or "",
                "chunk":    md.get("chunk"),
                "distance": round(float(dist), 4),
                "snippet":  doc[:500].replace("\n", " ").strip(),
            })
            if len(results) >= k:
                break

        _log(f"semantic_search done in {time.perf_counter()-t0:.2f}s, {len(results)} results")
        return {"query": query, "retrieval_query": retrieval_q,
                "author_filter": author_filter, "results": results}
    except Exception as e:
        return {"error": "semantic_search failed", "details": str(e)}


# ============================ TOOL 2: corpus_stats_by_author ============================
_BROAD_REGEXES = {"", ".", ".*", ".+", "*", "[a-z]", "[A-Za-z]"}


def corpus_stats_by_author(author_regex: str) -> dict:
    """Aggregate per-author corpus stats from SPGC counts files."""
    t0 = time.perf_counter()
    try:
        # Guard against catch-all regex — agents sometimes send '.*' when they want
        # "all authors", which then scans ~47k books for 100+ seconds and returns
        # nonsense. Force the caller to specify an actual author.
        if author_regex.strip() in _BROAD_REGEXES:
            return {"error": "regex too broad; use '^Surname,' format (e.g. '^Dickens,'). "
                             "For 'top authors by X' use the top_authors_by tool instead.",
                    "author_regex": author_regex}
        sel = _select_books(author_regex)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex}
        # Even with a non-trivial regex, refuse if it matches absurdly many books.
        if len(sel) > 500:
            return {"error": f"regex matched {int(len(sel))} books — too broad. "
                             "Tighten with an '^Surname,' anchor.",
                    "author_regex": author_regex, "matched": int(len(sel))}

        total_tokens = 0
        per_book = []
        vocab = Counter()
        for pid, row in sel.iterrows():
            pg = row["id"]
            f = _counts_path(pg)
            if not f.exists():
                continue
            book_tokens = 0
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c = parts[0], int(parts[1])
                    vocab[w] += c
                    book_tokens += c
            total_tokens += book_tokens
            per_book.append({"pg_id": pg, "title": row.get("title") or "", "tokens": book_tokens})

        if not per_book:
            return {"error": "books matched but no counts files found", "author_regex": author_regex}

        per_book.sort(key=lambda r: r["tokens"], reverse=True)
        out = {
            "author_regex":  author_regex,
            "books_matched": int(len(sel)),
            "books_with_counts": len(per_book),
            "titles":        [r["title"] for r in per_book[:50]],
            "total_tokens":  total_tokens,
            "unique_words":  len(vocab),
            "avg_book_length_words": int(total_tokens / max(1, len(per_book))),
            "longest_book":  per_book[0],
            "shortest_book": per_book[-1],
            "languages":     sorted({row.get("language") for _, row in sel.iterrows()
                                     if isinstance(row.get("language"), str)}),
        }
        _log(f"corpus_stats_by_author done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "corpus_stats_by_author failed", "details": str(e)}


# ============================ TOOL 3: top_ngrams_by_author ============================
def _is_clean_token(t: str) -> bool:
    return len(t) >= 2 and any(c.isalpha() for c in t)


def _spacy_pos_tags(words: list[str]) -> dict:
    """word -> POS tag (NOUN/VERB/ADJ/ADV/...). spaCy en_core_web_sm on CPU."""
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm", disable=["ner", "parser", "lemmatizer"])
    except Exception:
        return {w: "" for w in words}
    out = {}
    for w in words:
        doc = nlp(w)
        out[w] = doc[0].pos_ if len(doc) else ""
    return out


def top_ngrams_by_author(author_regex: str, n: int = 2, top: int = 20,
                         pos_filter: list[str] | None = None,
                         year_from: int | None = None,
                         year_to: int | None = None,
                         country: str | None = None) -> dict:
    """N-gram frequencies (n=1,2,3) from per-author SPGC tokens.

    pos_filter (only n=1): list like ["NOUN","VERB","ADJ","ADV","PROPN"]; keeps
    only unigrams whose spaCy POS is in the list. For n=2/3 the filter is
    applied to the FIRST token of the n-gram (typical use: "find adjective+noun
    bigrams" → pos_filter=["ADJ"]). On a large candidate pool the spaCy pass
    runs only on the top ~5x final size, so it stays cheap.

    year_from / year_to: filter by author birth_year+30 (writing prime proxy).
    Use author_regex='.*' to mean "any author" when filtering only by period.
    """
    t0 = time.perf_counter()
    if n not in (1, 2, 3):
        return {"error": "n must be 1, 2 or 3"}
    try:
        sel = _select_books(author_regex, year_from=year_from, year_to=year_to,
                            country=country)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex,
                    "year_from": year_from, "year_to": year_to,
                    "country": country}

        counter: Counter = Counter()
        used = 0
        total_ngrams = 0
        for pid, row in sel.iterrows():
            pg = row["id"]
            f = _tokens_path(pg)
            if not f.exists():
                continue
            used += 1
            with open(f, encoding="utf-8") as fh:
                toks = [t.strip().lower() for t in fh if t.strip()]
            if len(toks) < n:
                continue
            if n == 1:
                grams = (t for t in toks)
            elif n == 2:
                grams = zip(toks, toks[1:])
            else:
                grams = zip(toks, toks[1:], toks[2:])
            for g in grams:
                pieces = (g,) if n == 1 else g
                if not all(_is_clean_token(p) for p in pieces):
                    continue
                if all(p in STOPWORDS for p in pieces):
                    continue
                key = " ".join(pieces)
                counter[key] += 1
                total_ngrams += 1

        if pos_filter:
            pos_filter = [p.upper() for p in pos_filter]
            # tag the first token of the top-5x candidates and drop misses
            pool = counter.most_common(top * 5)
            heads = list({key.split(" ", 1)[0] for key, _ in pool})
            tags = _spacy_pos_tags(heads)
            filtered = [(k, c) for k, c in pool
                        if tags.get(k.split(" ", 1)[0]) in pos_filter]
            top_pairs = filtered[:top]
        else:
            top_pairs = counter.most_common(top)

        out = {
            "author_regex":  author_regex,
            "n":             n,
            "pos_filter":    pos_filter,
            "books_used":    used,
            "total_ngrams":  total_ngrams,
            "top":           [{"ngram": ng, "count": c} for ng, c in top_pairs],
        }
        _log(f"top_ngrams_by_author(n={n},pos={pos_filter}) done in {time.perf_counter()-t0:.2f}s, "
             f"used {used} books, {total_ngrams:,} ngrams")
        return out
    except Exception as e:
        return {"error": "top_ngrams_by_author failed", "details": str(e)}


# ============================ TOOL 4: affinity_by_author ============================
def affinity_by_author(author_regex: str, top: int = 50, min_author_count: int = 5,
                       min_corpus_count: int = 0,
                       pos_filter: list[str] | None = None) -> dict:
    """Per-author affinity vs corpus. Uses cached CSV if present, else runs
    spgc_author_affinity.py.

    If a NER-cleaned variant (`{slug}_affinity_clean.csv`) exists, prefer it —
    these have proper nouns (character names, place names) explicitly filtered
    out via spaCy NER, so the top is real stylistic markers like "blighter"
    instead of "Wrykyn" / "Threepwood". For authors without a clean variant we
    apply an inline heuristic: drop words whose corpus_count is essentially 0
    (appears only in this author = almost always a proper noun).

    min_corpus_count: if > 0, drop rows where corpus_count < min_corpus_count.
    Useful for filtering OOV/proper-noun bleed-through that spaCy POS misses
    on lowercased archaic words (oinos/luchesi/ulalume in Poe etc.). compare_authors
    passes 100 by default — words present in the 2.8B-token corpus < 100 times
    are almost always OOV character/place names from a single author's universe."""
    t0 = time.perf_counter()
    slug = _slug(author_regex)
    csv_path = DERIVED_DIR / f"{slug}_affinity.csv"
    clean_path = DERIVED_DIR / f"{slug}_affinity_clean.csv"
    cached = csv_path.exists() or clean_path.exists()
    try:
        if not cached:
            _log(f"running spgc_author_affinity.py for {author_regex!r} (slug={slug})")
            cmd = [
                "python", str(SCRIPTS_DIR / "spgc_author_affinity.py"),
                "--metadata", str(SPGC_METADATA),
                "--counts-dir", str(SPGC_COUNTS_DIR),
                "--corpus-counts", str(CORPUS_COUNTS),
                "--author", author_regex, "--slug", slug,
                "--out", str(DERIVED_DIR),
                "--min-author-count", str(min_author_count),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                return {"error": "spgc_author_affinity.py failed",
                        "stderr": proc.stderr[-500:]}
            if not csv_path.exists():
                return {"error": "affinity CSV not produced (no matching books?)",
                        "stdout": proc.stdout[-500:]}

        use_clean = clean_path.exists()
        df = pd.read_csv(clean_path if use_clean else csv_path)
        df = df[df["author_count"] >= min_author_count]
        # When narrowing by POS, OOV proper nouns get systematically mis-tagged
        # by spaCy as ADJ/NOUN (czarevitch, mahaffy, tuppy from Wilde). Force
        # min_corpus_count up to 1000 in that case unless the caller asked for
        # something higher — known English adjectives are abundant in a 2.8B
        # corpus, so this hurdle barely filters real lexemes.
        effective_min_corpus = min_corpus_count
        if pos_filter:
            effective_min_corpus = max(effective_min_corpus, 1000)
        if effective_min_corpus > 0:
            df = df[df["corpus_count"] >= effective_min_corpus]
        # Stylometric heuristic: a word is a real stylistic marker only if it
        # appears MORE in the corpus-without-this-author than in the author
        # alone. Words with corpus_count ≈ author_count are almost always
        # fictional names that en_core_web_sm NER missed (Threepwood,
        # Stockheath, Merevale, ...). Threshold: corpus_count - author_count >=
        # max(10, author_count * 0.5) — i.e. the word shows up "elsewhere" at
        # at least half the rate it appears in this author. "blighter" (143
        # corpus / 65 author → diff 78 ≥ 32.5) survives; "threepwood" (35/35 →
        # diff 0) is dropped.
        before = len(df)
        diff = df["corpus_count"] - df["author_count"]
        threshold = pd.concat([
            pd.Series(10, index=df.index),
            (df["author_count"] * 0.5).astype(int),
        ], axis=1).max(axis=1)
        df = df[diff >= threshold]
        heuristic_dropped = before - len(df)

        # Second pass: spaCy POS-tag the surviving top-N candidates and drop
        # anything tagged PROPN. spaCy's POS for isolated lowercased words
        # leans toward NOUN/VERB/ADJ for real lexemes; rare OOV strings get
        # tagged PROPN. This catches what the corpus-diff heuristic misses
        # (e.g. Wodehouse 'marvis' / 'stanning' which leak through).
        # When pos_filter is set we narrow further to those POS tags
        # ("characteristic adjectives of Wilde" → pos_filter=["ADJ"]).
        sorted_df = df.sort_values("affinity", ascending=False, na_position="last")
        # Pool 8x the requested top when narrowing by POS, since most words
        # won't match the filter; 4x is enough for the default PROPN drop.
        pool_mult = 8 if pos_filter else 4
        candidate_pool = sorted_df.head(top * pool_mult).copy()
        if len(candidate_pool):
            try:
                tags = _spacy_pos_tags(candidate_pool["word"].tolist())
                if pos_filter:
                    allowed = {p.upper() for p in pos_filter}
                    keep_mask = candidate_pool["word"].map(
                        lambda w: tags.get(w, "") in allowed
                    )
                else:
                    keep_mask = candidate_pool["word"].map(
                        lambda w: tags.get(w, "") != "PROPN"
                    )
                propn_dropped = int((~keep_mask).sum())
                df = candidate_pool[keep_mask]
            except Exception as e:
                _log(f"spaCy POS filter failed: {e}")
                propn_dropped = 0
                df = sorted_df
        else:
            propn_dropped = 0
        df = df.sort_values("affinity", ascending=False, na_position="last").head(top)
        top_rows = [
            {"word": r["word"], "author_count": int(r["author_count"]),
             "corpus_count": int(r["corpus_count"]),
             "affinity": round(float(r["affinity"]), 2)}
            for _, r in df.iterrows() if pd.notna(r["affinity"])
        ]
        total_unique = int(len(pd.read_csv(clean_path if use_clean else csv_path)))
        out = {
            "author_regex":       author_regex,
            "slug":               slug,
            "pos_filter":         pos_filter,
            "effective_min_corpus_count": effective_min_corpus,
            "total_unique_words": total_unique,
            "top":                top_rows,
            "cached":             cached,
            "proper_noun_filter": (
                f"corpus-diff heuristic dropped {heuristic_dropped}, "
                f"spaCy PROPN dropped {propn_dropped}"
                + (" (over NER-cleaned base)" if use_clean else "")
            ),
        }
        _log(f"affinity_by_author done in {time.perf_counter()-t0:.2f}s "
             f"(cached={cached}, clean={use_clean})")
        return out
    except Exception as e:
        return {"error": "affinity_by_author failed", "details": str(e)}


# ============================ TOOL 5: word_contexts ============================
def word_contexts(author_regex: str, word: str, window: int = 10, max_samples: int = 5) -> dict:
    """±window token contexts for a word, from SPGC tokens of matching books."""
    t0 = time.perf_counter()
    try:
        sel = _select_books(author_regex)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex}

        word_lc = word.strip().lower()
        samples: list[dict] = []
        total = 0
        titles = dict(zip(sel["id"], sel["title"].fillna("")))

        for pg in sel["id"]:
            f = _tokens_path(pg)
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                toks = [t.strip() for t in fh if t.strip()]
            hits = [i for i, t in enumerate(toks) if t.lower() == word_lc]
            total += len(hits)
            for i in hits:
                if len(samples) >= max_samples:
                    break
                lo, hi = max(0, i - window), min(len(toks), i + window + 1)
                ctx = " ".join(toks[lo:i] + [f"[{word_lc.upper()}]"] + toks[i + 1:hi])
                samples.append({"pg_id": pg, "title": titles.get(pg, ""), "context": ctx})
            if len(samples) >= max_samples:
                break

        out = {
            "author_regex":      author_regex,
            "word":              word_lc,
            "total_occurrences": total,
            "samples":           samples,
        }
        _log(f"word_contexts done in {time.perf_counter()-t0:.2f}s ({total} hits)")
        return out
    except Exception as e:
        return {"error": "word_contexts failed", "details": str(e)}


# ============================ TOOL 6: compare_authors ============================
def compare_authors(author1_regex: str, author2_regex: str, top: int = 20,
                    min_corpus_count: int = 100) -> dict:
    """Composition of affinity_by_author for two authors + cosine similarity of their affinity vectors.

    min_corpus_count: minimum corpus_count for a word to be included as a
    "stylistic marker". Default 100 in a 2.8B-token corpus filters out OOV
    character/place names (oinos, ulalume, dunwich, threepwood) that the
    spaCy PROPN heuristic misses on lowercased archaic words. Pass 0 to
    include rare words too."""
    t0 = time.perf_counter()
    try:
        a1 = affinity_by_author(author1_regex, top=top * 5, min_corpus_count=min_corpus_count)
        a2 = affinity_by_author(author2_regex, top=top * 5, min_corpus_count=min_corpus_count)
        if "error" in a1:
            return {"error": f"author1 failed: {a1['error']}", "details": a1}
        if "error" in a2:
            return {"error": f"author2 failed: {a2['error']}", "details": a2}

        m1 = {r["word"]: r["affinity"] for r in a1["top"]}
        m2 = {r["word"]: r["affinity"] for r in a2["top"]}
        shared_words = set(m1) & set(m2)
        shared = sorted(
            ({"word": w, "affinity_1": m1[w], "affinity_2": m2[w]} for w in shared_words),
            key=lambda r: min(r["affinity_1"], r["affinity_2"]), reverse=True,
        )[:top]

        # cosine on the intersection of vocabularies
        all_words = set(m1) | set(m2)
        v1 = [m1.get(w, 0.0) for w in all_words]
        v2 = [m2.get(w, 0.0) for w in all_words]
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1)) or 1.0
        n2 = math.sqrt(sum(b * b for b in v2)) or 1.0
        cosine = round(dot / (n1 * n2), 4)

        # Cosine on top-N affinity vectors is structurally low: the affinity
        # vector is concentrated on each author's *unique* high-affinity words,
        # so unless two authors share signature vocabulary the intersection is
        # near-empty and cosine ≈ 0. This does NOT mean "styles are unrelated"
        # — it means "the top stylistic markers do not overlap". Flag this so
        # the agent doesn't over-interpret a 0.0 result.
        if cosine < 0.05:
            cosine_note = ("low cosine (< 0.05) reflects that each author's top "
                           "affinity vector is dominated by author-unique words; "
                           "this is structural, not a measure of overall stylistic "
                           "distance. Use `shared_high_affinity` to see common "
                           "stylistic markers if any exist.")
        else:
            cosine_note = "non-trivial overlap between top affinity vectors"

        out = {
            "author1": {"regex": author1_regex, "slug": a1["slug"], "top_unique": a1["top"][:top]},
            "author2": {"regex": author2_regex, "slug": a2["slug"], "top_unique": a2["top"][:top]},
            "shared_high_affinity": shared,
            "cosine_similarity":    cosine,
            "cosine_note":          cosine_note,
            "min_corpus_count":     min_corpus_count,
        }
        _log(f"compare_authors done in {time.perf_counter()-t0:.2f}s, cosine={cosine}")
        return out
    except Exception as e:
        return {"error": "compare_authors failed", "details": str(e)}


# ============================ TOOL 7: lexical_diversity ============================
def lexical_diversity(scope: dict | str) -> dict:
    """Type-Token Ratio over the scope. Higher = more varied vocabulary.

    scope: {"book": "PG1342"}  |  {"author": "^Doyle,"}  |  "all_corpus"
    """
    t0 = time.perf_counter()
    try:
        counts: Counter = Counter()
        per_book = []
        if isinstance(scope, str) and scope == "all_corpus":
            counts = Counter(_metadata_df()  # noqa: F841 placeholder
                             ) if False else None  # too expensive to recompute; use baseline
            corpus_counts_path = CORPUS_COUNTS
            with open(corpus_counts_path, encoding="utf-8") as fh:
                rd = csv.reader(fh); next(rd)
                v = t = 0
                for w, c in rd:
                    c = int(c); v += 1; t += c
            return {"scope": "all_corpus", "tokens": t, "types": v,
                    "ttr": round(v / max(1, t), 6), "note": "from corpus_counts.csv baseline"}
        elif isinstance(scope, dict) and scope.get("book"):
            pg = scope["book"].upper()
            if not (pg.startswith("PG") or pg.startswith("U")): pg = f"PG{pg}"
            f = _counts_path(pg)
            if not f.exists():
                hint = ("run scripts/tokenize_user_books.py --book " + pg
                        if pg.startswith("U") else "")
                return {"error": "counts file not found", "id": pg, "hint": hint}
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) == 2:
                        counts[parts[0]] = int(parts[1])
            tokens, types = sum(counts.values()), len(counts)
            return {"scope": f"book:{pg}", "tokens": tokens, "types": types,
                    "ttr": round(types / max(1, tokens), 6)}
        elif isinstance(scope, dict) and scope.get("author"):
            sel = _select_books(scope["author"])
            if not len(sel):
                return {"error": "no books matched", "author_regex": scope["author"]}
            for pg in sel["id"]:
                f = _counts_path(pg)
                if not f.exists(): continue
                book = Counter()
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) == 2:
                            book[parts[0]] = int(parts[1])
                            counts[parts[0]] += int(parts[1])
                btok, btyp = sum(book.values()), len(book)
                if btok:
                    per_book.append({"pg_id": pg, "tokens": btok, "types": btyp,
                                     "ttr": round(btyp / btok, 6)})
            tokens, types = sum(counts.values()), len(counts)
            per_book.sort(key=lambda r: r["ttr"], reverse=True)
            return {"scope": f"author:{scope['author']}",
                    "tokens": tokens, "types": types,
                    "ttr_aggregate": round(types / max(1, tokens), 6),
                    "ttr_avg_per_book": round(
                        sum(b["ttr"] for b in per_book) / max(1, len(per_book)), 6),
                    "books_used": len(per_book),
                    "top_5_most_varied": per_book[:5],
                    "bottom_5_least_varied": per_book[-5:],
                    "note": "TTR collapses to a single number across the whole author; "
                            "per-book averages or higher-order measures (MATTR, MTLD) "
                            "are more comparable across books of different length"}
        else:
            return {"error": "bad scope; use {'book':PGid} | {'author':regex} | 'all_corpus'"}
    except Exception as e:
        return {"error": "lexical_diversity failed", "details": str(e)}
    finally:
        _log(f"lexical_diversity done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL 8: word_collocates ============================
def word_collocates(scope: dict | str, word: str, window: int = 4,
                    top: int = 20, exclude_stopwords: bool = True) -> dict:
    """Words that co-occur within ±window tokens of `word` in the scope's books.

    scope:
        {'book': 'PG1342'}                        — single book
        {'author': '^Doyle,'}                     — all books of author(s)
        {'author': '.*', 'year_from': 1837,       — period filter via author
                          'year_to':  1901}         birth_year+30 (writing prime).
                                                    e.g. Victorians: 1837-1901.
        {'author': '.*', 'country': 'GB'}         — nationality filter via
                                                    Sprint 9.2 Wikidata enrichment.
                                                    'British words near fog'.
        Combine: {'author': '.*', 'year_from': 1837, 'country': 'GB'}
    """
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc:
        return {"error": "word required"}
    try:
        if isinstance(scope, dict) and scope.get("book"):
            pg = scope["book"].upper()
            if not (pg.startswith("PG") or pg.startswith("U")): pg = f"PG{pg}"
            book_ids = [pg]
            label = f"book:{pg}"
        elif isinstance(scope, dict) and scope.get("author"):
            yf = scope.get("year_from")
            yt = scope.get("year_to")
            country = scope.get("country")
            sel = _select_books(scope["author"], year_from=yf, year_to=yt,
                                country=country)
            if not len(sel):
                return {"error": "no books matched", "author_regex": scope["author"],
                        "year_from": yf, "year_to": yt, "country": country}
            book_ids = list(sel["id"])
            period = f" {yf}-{yt}" if (yf or yt) else ""
            cn = f" country={country}" if country else ""
            label = f"author:{scope['author']}{period}{cn} ({len(sel)} books)"
        else:
            return {"error": "bad scope; use {'book':PGid} | {'author':regex, [year_from, year_to, country]}"}

        neighbors: Counter = Counter()
        hits = 0
        books_with_hits = 0
        for pg in book_ids:
            f = _tokens_path(pg)
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                toks = [t.strip().lower() for t in fh if t.strip()]
            local_hits = 0
            for i, t in enumerate(toks):
                if t != word_lc:
                    continue
                local_hits += 1
                lo, hi = max(0, i - window), min(len(toks), i + window + 1)
                for j in range(lo, hi):
                    if j == i:
                        continue
                    nb = toks[j]
                    if not _is_clean_token(nb):
                        continue
                    if nb == word_lc:
                        continue
                    if exclude_stopwords and nb in STOPWORDS:
                        continue
                    neighbors[nb] += 1
            if local_hits:
                books_with_hits += 1
                hits += local_hits

        return {
            "scope":            label,
            "word":             word_lc,
            "window":           window,
            "total_occurrences": hits,
            "books_with_hits":   books_with_hits,
            "top_collocates":   [{"word": w, "count": c} for w, c in neighbors.most_common(top)],
        }
    except Exception as e:
        return {"error": "word_collocates failed", "details": str(e)}
    finally:
        _log(f"word_collocates done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL 9: book_readability ============================
_VOWEL_GROUPS_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)


def _count_syllables(word: str) -> int:
    word = word.lower()
    if not word:
        return 0
    n = len(_VOWEL_GROUPS_RE.findall(word))
    if word.endswith("e") and n > 1:
        n -= 1
    return max(1, n)


_GUTENBERG_HEADER = re.compile(r"\*\*\* START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
                               re.IGNORECASE | re.DOTALL)
_GUTENBERG_FOOTER = re.compile(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG.*",
                               re.IGNORECASE | re.DOTALL)


def book_readability(pg_id: str, sample_chars: int = 200_000) -> dict:
    """Flesch Reading Ease + Flesch-Kincaid Grade for a single book.

    Reads /data/raw_text/<pg|u><id>.txt (after Gutenberg header/footer strip),
    samples the first `sample_chars` chars to keep this fast, splits sentences
    on .!? heuristics, counts words and syllables. Accepts PG<n> (Gutenberg)
    or U<n> (user-uploaded) ids.
    """
    t0 = time.perf_counter()
    pg = pg_id.upper()
    if not (pg.startswith("PG") or pg.startswith("U")):
        pg = f"PG{pg}"
    raw_path = Path(f"/workspace/raw_text/{pg.lower()}.txt")
    if not raw_path.exists():
        return {"error": "raw text not in /data/raw_text/ for this id", "id": pg}
    try:
        text = raw_path.read_text(encoding="utf-8", errors="replace")
        m = _GUTENBERG_HEADER.search(text)
        if m: text = text[m.end():]
        m = _GUTENBERG_FOOTER.search(text)
        if m: text = text[:m.start()]
        text = text[:sample_chars]

        sentences = re.split(r"(?<=[.!?])\s+", text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
        words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
        if not sentences or not words:
            return {"error": "not enough sentences/words after cleanup", "pg_id": pg}
        syll_total = sum(_count_syllables(w) for w in words)
        n_w, n_s = len(words), len(sentences)
        asl  = n_w / n_s              # avg sentence length
        asw  = syll_total / n_w       # avg syllables / word
        flesch_ease  = round(206.835 - 1.015 * asl - 84.6 * asw, 1)
        fk_grade     = round(0.39 * asl + 11.8 * asw - 15.59, 1)

        # CEFR band rough heuristic from Flesch Reading Ease
        if   flesch_ease >= 80: cefr = "A2 (very easy)"
        elif flesch_ease >= 70: cefr = "B1"
        elif flesch_ease >= 60: cefr = "B1-B2"
        elif flesch_ease >= 50: cefr = "B2"
        elif flesch_ease >= 30: cefr = "C1"
        else:                   cefr = "C2+ (academic / archaic)"

        df = _metadata_df()
        meta_row = df[df["id"] == pg]
        title  = meta_row.iloc[0]["title"]  if len(meta_row) else ""
        author = meta_row.iloc[0]["author"] if len(meta_row) else ""

        return {
            "id": pg, "pg_id": pg, "title": title, "author": author,
            "user_uploaded": pg.startswith("U"),
            "sampled_chars":      len(text),
            "sentences":          n_s,
            "words":              n_w,
            "avg_sentence_length_words": round(asl, 2),
            "avg_syllables_per_word":    round(asw, 3),
            "flesch_reading_ease": flesch_ease,
            "flesch_kincaid_grade": fk_grade,
            "cefr_heuristic":     cefr,
        }
    except Exception as e:
        return {"error": "book_readability failed", "details": str(e)}
    finally:
        _log(f"book_readability({pg}) done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL 10: word_freq_timeline ============================
def word_freq_timeline(word: str, bucket_years: int = 25,
                      min_books_per_bucket: int = 3,
                      lang: str = "en",
                      basis: str = "auto") -> dict:
    """Frequency of `word` across periods.

    `basis`:
      - "auto"      (default): pub_year if available for the book, otherwise
                    `authoryearofbirth + 30` (writing-prime proxy). Mixed
                    timelines are common because OL coverage isn't 100%.
      - "pub_year": drop books without pub_year. Cleaner curve for the books
                    that do have it. Use after the Sprint 9.7 OL batch fills
                    the enrichment table.
      - "birth"     : ignore pub_year, always use birth_year+30. The pre-9.7
                    behaviour — useful when checking that timelines didn't
                    regress.
    Books outside [1500, 2030] on the chosen basis are dropped.
    """
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc:
        return {"error": "word required"}
    try:
        df = _metadata_df()
        mask_lang = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
        df = df[mask_lang].copy()
        birth = pd.to_numeric(df["authoryearofbirth"], errors="coerce")
        birth_proxy = birth + 30  # writing prime
        pub = pd.to_numeric(df.get("pub_year"), errors="coerce") \
            if "pub_year" in df.columns else pd.Series([pd.NA] * len(df), index=df.index)

        if basis == "pub_year":
            df["axis_year"] = pub
            axis_label = "pub_year (Open Library, real publication year)"
        elif basis == "birth":
            df["axis_year"] = birth_proxy
            axis_label = "authoryearofbirth + 30 (writing prime proxy)"
        else:  # auto
            df["axis_year"] = pub.fillna(birth_proxy)
            axis_label = ("pub_year when known (OL enrichment), "
                          "otherwise authoryearofbirth + 30")

        df = df[(df["axis_year"] >= 1500) & (df["axis_year"] <= 2030)]
        df["period_start"] = (df["axis_year"] // bucket_years * bucket_years).astype(int)

        buckets: dict = {}
        for period, group in df.groupby("period_start"):
            tok_total = 0
            occurrences = 0
            books_used = 0
            for pg in group["id"]:
                f = _counts_path(pg)
                if not f.exists():
                    continue
                book_total = 0
                book_word = 0
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) != 2:
                            continue
                        w, c = parts[0], int(parts[1])
                        book_total += c
                        if w == word_lc:
                            book_word += c
                if book_total:
                    tok_total += book_total
                    occurrences += book_word
                    books_used += 1
            if books_used >= min_books_per_bucket and tok_total:
                buckets[int(period)] = {
                    "period":   f"{int(period)}-{int(period)+bucket_years-1}",
                    "books":    books_used,
                    "total_tokens":  tok_total,
                    "occurrences":   occurrences,
                    "per_million":   round(1_000_000 * occurrences / tok_total, 2),
                }
        timeline = [buckets[k] for k in sorted(buckets)]
        return {
            "word": word_lc,
            "bucket_years": bucket_years,
            "basis":     basis,
            "axis_basis": axis_label,
            "timeline": timeline,
        }
    except Exception as e:
        return {"error": "word_freq_timeline failed", "details": str(e)}
    finally:
        _log(f"word_freq_timeline({word_lc}) done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL 11: word_contexts_global ============================
def _normalize_lang(raw: str) -> str:
    """Pull a plain ISO code out of the various shapes `language` can take in
    the merged metadata frame: ``"en"``, ``"['en']"``, ``"['en', 'fr']"``,
    ``""``, ``"nan"``. Returns lower-cased primary code or "" if unknown."""
    if not raw:
        return ""
    s = str(raw).strip().lower()
    if s in {"nan", "none"}:
        return ""
    # Strip Python-list-repr brackets and quotes: "['en']" → "en"
    s = s.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
    if not s:
        return ""
    # Multiple codes separated by comma — take primary.
    return s.split(",")[0].strip()


# Metalinguistic books — dictionaries, grammar manuals, encyclopedias — match
# a target word as a *headword/definition*, not a usage example. Drop them.
METALINGUISTIC_SUBJECT_SUBSTRINGS = (
    "dictionar", "encyclopedi", "grammar", "lexico",
    "philolog", "linguistic", "etymolog",
    " language --", " language",  # "English language", "Malay language", etc.
)
METALINGUISTIC_TITLE_SUBSTRINGS = (
    "dictionary", "encyclopaedia", "encyclopedia", "grammar",
    "manual of", "book about words", "lexicon", "thesaurus",
)


def _is_metalinguistic(title: str, subjects: str) -> bool:
    """True if the book is a dictionary/grammar/encyclopedia where a word
    appears as a headword instead of in natural usage."""
    t_lc = (title or "").lower()
    s_lc = (subjects or "").lower()
    if any(s in t_lc for s in METALINGUISTIC_TITLE_SUBSTRINGS):
        return True
    if any(s in s_lc for s in METALINGUISTIC_SUBJECT_SUBSTRINGS):
        return True
    return False


def word_contexts_global(word: str, k: int = 12, snippet_chars: int = 280,
                         lang: str = "en") -> dict:
    """Contexts of a target word from many authors at once.

    Uses the ChromaDB semantic index to fetch chunks that are likely to
    contain the word (we pad the query with surrounding text to bias the
    retriever), then filters to chunks that actually mention the word.
    Returns up to k samples, each with author/title/PG id + snippet around
    the first occurrence.

    Post-filters via _metadata_df():
    - language must match `lang` (default "en") — drops Malay/Spanish hits where
      the word is a homograph in another language.
    - drops dictionaries, grammars, encyclopedias and other metalinguistic
      books where the word appears as a headword, not in natural usage.
    Pass lang=None to disable language filter.
    """
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc or " " in word_lc:
        return {"error": "word must be a single token"}
    try:
        col = _get_chroma_collection_with_embedder()

        # ChromaDB has a where_document substring filter — use it so we only
        # retrieve chunks that literally contain the word. Then we rank what
        # comes back by semantic distance to a paraphrastic query.
        q = f"usage of the word {word_lc} in literature"
        # Fetch wider — metalinguistic + non-English filtering can drop a lot.
        fetch = max(k * 8, 80)
        try:
            res = col.query(query_texts=[q], n_results=fetch,
                            where_document={"$contains": word_lc})
        except Exception:
            # fallback if backend doesn't support where_document
            res = col.query(query_texts=[q], n_results=fetch * 4)

        # Build a pg_id → (language, subjects) lookup from the merged metadata
        # frame. Cheap because _metadata_df() is mtime-cached.
        meta = _metadata_df()
        meta_lookup = {}
        if meta is not None and len(meta):
            for _, row in meta[["id", "language", "subjects"]].iterrows():
                meta_lookup[str(row["id"])] = (str(row.get("language", "") or ""),
                                               str(row.get("subjects", "") or ""))

        seen_authors = set()
        out_samples = []
        dropped_lang = 0
        dropped_meta = 0
        for doc, md, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            doc_lc = doc.lower()
            idx = doc_lc.find(word_lc)
            if idx < 0:
                continue
            # token-boundary check — avoid matching "blackbird" when asked "bird"
            before = doc_lc[idx-1] if idx > 0 else " "
            after  = doc_lc[idx+len(word_lc)] if idx+len(word_lc) < len(doc_lc) else " "
            if before.isalnum() or after.isalnum():
                continue
            author = md.get("author") or ""
            pg_id = str(md.get("pg_id") or "")
            title = md.get("title") or ""

            # Language + metalinguistic filters via metadata lookup.
            # NOTE: language is missing for many orphan_pg / user_upload rows,
            # so we only drop on a KNOWN non-match. Unknown language → keep
            # (better to show borderline samples than miss the obvious
            # English ones whose metadata just isn't filled in).
            book_lang_raw, book_subj = meta_lookup.get(pg_id, ("", ""))
            book_lang = _normalize_lang(book_lang_raw)
            want_lang = lang.strip().lower() if lang else ""
            if want_lang and book_lang and book_lang != want_lang:
                dropped_lang += 1
                continue
            if _is_metalinguistic(title, book_subj):
                dropped_meta += 1
                continue

            if author in seen_authors:
                continue
            seen_authors.add(author)
            lo = max(0, idx - snippet_chars // 2)
            hi = min(len(doc), idx + len(word_lc) + snippet_chars // 2)
            snippet = doc[lo:hi].replace("\n", " ").strip()
            out_samples.append({
                "author":   author,
                "title":    title,
                "pg_id":    pg_id,
                "distance": round(float(dist), 4),
                "snippet":  snippet,
            })
            if len(out_samples) >= k:
                break
        return {"word": word_lc, "k": k, "samples": out_samples,
                "unique_authors": len(seen_authors),
                "filter_stats": {"dropped_lang": dropped_lang,
                                 "dropped_metalinguistic": dropped_meta,
                                 "lang": lang}}
    except Exception as e:
        return {"error": "word_contexts_global failed", "details": str(e)}
    finally:
        _log(f"word_contexts_global({word_lc}) done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL: NRC sentiment / emotion ============================
NRC_PATH = DERIVED_DIR / "nrc_emotion_lexicon.json"
NRC_EMOTIONS = ("anger", "anticipation", "disgust", "fear", "joy",
                "sadness", "surprise", "trust")
NRC_VALENCES = ("positive", "negative")
_NRC_CACHE: dict = {"lex": None, "by_emotion": None}


def _nrc_lexicon() -> dict:
    """Lazy-load NRC Emotion Lexicon JSON (~200KB). Returns {word: [emotions]}.

    Built once via scripts/download_nrc.py; if missing returns {} and downstream
    tools surface an error. Cached in module memory after first call."""
    if _NRC_CACHE["lex"] is not None:
        return _NRC_CACHE["lex"]
    if not NRC_PATH.exists():
        _NRC_CACHE["lex"] = {}
        return _NRC_CACHE["lex"]
    try:
        with open(NRC_PATH, encoding="utf-8") as fh:
            _NRC_CACHE["lex"] = json.load(fh)
    except Exception as e:
        _log(f"NRC lexicon read failed: {e}")
        _NRC_CACHE["lex"] = {}
    # Build inverted index emotion -> set(words) for emotion_collocates
    inv: dict = {e: set() for e in NRC_EMOTIONS + NRC_VALENCES}
    for w, es in _NRC_CACHE["lex"].items():
        for e in es:
            if e in inv:
                inv[e].add(w)
    _NRC_CACHE["by_emotion"] = inv
    return _NRC_CACHE["lex"]


def _nrc_inverted() -> dict[str, set[str]]:
    if _NRC_CACHE["by_emotion"] is None:
        _nrc_lexicon()
    return _NRC_CACHE["by_emotion"] or {}


def book_emotion_profile(pg_id: str) -> dict:
    """Distribution of NRC emotions across all tokens in one book.

    For each token in /data/spgc/{counts}/{pg_id}_counts.txt we look up the
    NRC lexicon. A token may carry several emotions ('terrified' → fear +
    negative). We report:
      - per_million for each of the 8 emotions + positive/negative
      - share = emotion_tokens / total_emotion_bearing_tokens (normalised
        so the 8 emotions sum to less than 1.0 — overlap with the
        valences is large)
      - top emotion-anchor words actually used in the book

    Use for 'эмоциональный профиль Dracula', 'насколько у По много слов
    страха', 'позитивная/негативная окраска книги X'.
    """
    t0 = time.perf_counter()
    pg = pg_id.upper()
    if not (pg.startswith("PG") or pg.startswith("U")):
        pg = f"PG{pg}"
    try:
        f = _counts_path(pg)
        if not f.exists():
            return {"error": "counts file not found", "id": pg,
                    "hint": "U-books: run tokenize_user_books.py after upload"}
        lex = _nrc_lexicon()
        if not lex:
            return {"error": "NRC lexicon not loaded — run download_nrc.py first"}

        per_emotion: Counter = Counter()
        sample_words: dict[str, list[tuple[str, int]]] = {e: [] for e in NRC_EMOTIONS + NRC_VALENCES}
        total_tokens = 0
        with_emotion = 0
        seen_for_sample: dict[str, int] = {e: 0 for e in NRC_EMOTIONS + NRC_VALENCES}
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                w, c = parts[0], int(parts[1])
                total_tokens += c
                ems = lex.get(w)
                if not ems:
                    continue
                with_emotion += c
                for e in ems:
                    per_emotion[e] += c
                    if seen_for_sample[e] < 8:
                        sample_words[e].append((w, c))
                        seen_for_sample[e] += 1

        df = _metadata_df()
        meta_row = df[df["id"] == pg]
        title  = meta_row.iloc[0]["title"]  if len(meta_row) else ""
        author = meta_row.iloc[0]["author"] if len(meta_row) else ""

        per_million = {e: round(1_000_000 * per_emotion[e] / max(1, total_tokens), 2)
                       for e in NRC_EMOTIONS + NRC_VALENCES}
        # Share among the 8 primary emotions (skip valences for the share — those
        # double-count by design).
        prim_sum = sum(per_emotion[e] for e in NRC_EMOTIONS)
        share = {e: round(per_emotion[e] / max(1, prim_sum), 3) for e in NRC_EMOTIONS}

        # Sort sample words by count desc within each emotion
        for e in sample_words:
            sample_words[e].sort(key=lambda kv: -kv[1])
            sample_words[e] = sample_words[e][:8]

        out = {
            "id": pg, "title": title, "author": author,
            "total_tokens": total_tokens,
            "emotion_bearing_tokens": with_emotion,
            "emotion_coverage_pct": round(100 * with_emotion / max(1, total_tokens), 2),
            "per_million": per_million,
            "share_among_primary_emotions": share,
            "sample_anchor_words": sample_words,
        }
        _log(f"book_emotion_profile({pg}) done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "book_emotion_profile failed", "details": str(e)}


_HIGH_FREQ_NEIGHBOR_DROP = {
    # Pronouns + auxiliaries + bare prepositions + filler quantifiers that
    # always rank high in any collocate query. STOPWORDS catches some; this
    # adds the worst-offenders we kept seeing in 'fear collocates у По' etc.
    "all", "any", "most", "very", "still", "more", "one", "two", "three",
    "some", "such", "even", "ever", "never", "again", "also", "yet", "thus",
    "upon", "into", "among", "between", "without", "within",
    "me", "him", "us", "them", "myself", "himself", "herself",
    "thee", "thou", "thy", "thine", "ye",
    "said", "say", "made", "make", "make", "went", "came", "got", "gone",
    "great", "little", "good", "own", "much", "many", "long", "old",
    "well", "right", "left",
}


def emotion_collocates(scope: dict, emotion: str, window: int = 4,
                      top: int = 25, exclude_stopwords: bool = True,
                      max_anchors: int = 30,
                      anchor_min_corpus_rank: int = 1000) -> dict:
    """Words that co-occur with an emotional-anchor cluster from NRC.

    For example emotion='fear' → anchor words are the NRC fear-tagged terms
    that actually appear in the scope ('terror', 'dread', 'horror',
    'fearful', 'panic'...). We collect ±window collocates of those anchors
    in the scope's tokens and aggregate.

    scope:
        {'book': 'PG345'}       — one book
        {'author': '^Poe,'}     — all books of author
    emotion: one of NRC categories — anger / anticipation / disgust / fear /
        joy / sadness / surprise / trust / positive / negative.

    For 'слова страха у По', 'tone слов у Лавкрафта', 'joyful collocates of
    Wodehouse'.
    """
    t0 = time.perf_counter()
    e_lc = emotion.strip().lower()
    if e_lc not in NRC_EMOTIONS and e_lc not in NRC_VALENCES:
        return {"error": "unknown emotion",
                "supported": list(NRC_EMOTIONS + NRC_VALENCES), "got": e_lc}
    inv = _nrc_inverted()
    anchors = inv.get(e_lc, set())
    if not anchors:
        return {"error": "NRC lexicon not loaded",
                "hint": "scripts/download_nrc.py"}

    # Drop NRC anchors that are also in the top-N most frequent corpus words.
    # NRC over-tags generic words ('case', 'force', 'shell' as fear) — these
    # poison the anchor pool with high-frequency-but-weak-signal words. Drop
    # top-1000 of the corpus by default; tunable via anchor_min_corpus_rank.
    if anchor_min_corpus_rank > 0:
        try:
            top_general: set = set()
            with open(CORPUS_COUNTS, encoding="utf-8") as fh:
                rd = csv.reader(fh); next(rd)
                for i, (w, _c) in enumerate(rd):
                    if i >= anchor_min_corpus_rank:
                        break
                    top_general.add(w)
            anchors = {a for a in anchors if a not in top_general}
        except Exception as e:
            _log(f"corpus-rank filter failed: {e}")

    try:
        if isinstance(scope, dict) and scope.get("book"):
            pg = scope["book"].upper()
            if not (pg.startswith("PG") or pg.startswith("U")): pg = f"PG{pg}"
            book_ids = [pg]
            label = f"book:{pg}"
        elif isinstance(scope, dict) and scope.get("author"):
            sel = _select_books(scope["author"],
                               year_from=scope.get("year_from"),
                               year_to=scope.get("year_to"))
            if not len(sel):
                return {"error": "no books matched", "author_regex": scope["author"]}
            book_ids = list(sel["id"])
            label = f"author:{scope['author']} ({len(sel)} books)"
        else:
            return {"error": "bad scope; use {'book': PGid} | {'author': regex}"}

        # First pass — pick the top-N anchor words that ACTUALLY appear in
        # the scope (lots of NRC entries are obscure for any one author).
        anchor_freq: Counter = Counter()
        for pg in book_ids:
            f = _counts_path(pg)
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c = parts[0], int(parts[1])
                    if w in anchors:
                        anchor_freq[w] += c
        top_anchors = [w for w, _ in anchor_freq.most_common(max_anchors)]
        anchor_set = set(top_anchors)
        if not anchor_set:
            return {"scope": label, "emotion": e_lc,
                    "warning": "no anchor words from this emotion in scope",
                    "anchor_pool_size": len(anchors)}

        # Second pass — sliding window collocates around any anchor.
        neighbors: Counter = Counter()
        anchor_hits = 0
        for pg in book_ids:
            f = _tokens_path(pg)
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                toks = [t.strip().lower() for t in fh if t.strip()]
            for i, t in enumerate(toks):
                if t not in anchor_set:
                    continue
                anchor_hits += 1
                lo, hi = max(0, i - window), min(len(toks), i + window + 1)
                for j in range(lo, hi):
                    if j == i:
                        continue
                    nb = toks[j]
                    if not _is_clean_token(nb):
                        continue
                    if nb in anchor_set:
                        continue
                    if exclude_stopwords and (nb in STOPWORDS or
                                              nb in _HIGH_FREQ_NEIGHBOR_DROP):
                        continue
                    neighbors[nb] += 1

        out = {
            "scope": label,
            "emotion": e_lc,
            "anchor_pool_in_lexicon": len(anchors),
            "anchors_in_scope": [{"word": w, "count": c}
                                 for w, c in anchor_freq.most_common(15)],
            "total_anchor_hits": anchor_hits,
            "top_collocates": [{"word": w, "count": c}
                               for w, c in neighbors.most_common(top)],
        }
        _log(f"emotion_collocates({e_lc}, {label}) done in "
             f"{time.perf_counter()-t0:.2f}s, anchors={len(anchor_set)}, "
             f"hits={anchor_hits}")
        return out
    except Exception as e:
        return {"error": "emotion_collocates failed", "details": str(e)}


# ============================ TOOL: find_book ============================
def find_book(title: str, author: str = "", top: int = 5,
              lang: str = "en") -> dict:
    """Look up a book in the metadata table by title (substring/regex, case-
    insensitive) with optional author hint. Returns matches with PG/U id,
    title, author, downloads, year.

    Use this BEFORE calling any tool that takes a `pg_id` (book_readability,
    affinity_by_book, lexical_diversity({"book": ...}), word_collocates(
    {"book": ...}), learning_words({"book": ...})) when the user names a
    book by title rather than ID. Never guess PG IDs from memory — the
    corpus has 75k books and ID assignment doesn't match popularity. For
    example "Crime and Punishment" is PG2554, not PG1327 (which is
    "Elizabeth and Her German Garden").

    title: substring or regex. Tries an unanchored case-insensitive regex
        first; falls back to a simple substring match if regex fails.
    author: optional regex on author column to disambiguate ("Hamlet" by
        Shakespeare vs by Updike).
    """
    if not title or not title.strip():
        return {"error": "title required"}
    t0 = time.perf_counter()
    try:
        df = _metadata_df()
        if df is None or not len(df):
            return {"error": "metadata frame empty"}

        # Language pre-filter (the metadata column stores "['en']" str-repr;
        # tolerate both forms via contains-substring).
        mask = pd.Series(True, index=df.index)
        if lang:
            mask &= df["language"].fillna("").str.contains(
                f"'{lang}'", regex=False
            ) | df["language"].fillna("").str.lower().eq(lang.lower())

        # Same Cyrillic-detect trick as semantic_search — PG titles are stored
        # in English (or transliterated for Russian classics like "Voina i mir"),
        # so a query like "Преступление и наказание" needs to become "Crime and
        # Punishment" before we hit the title column.
        title_q = _maybe_translate(title.strip())
        try:
            mask &= df["title"].fillna("").str.contains(
                title_q, case=False, regex=True, na=False
            )
        except Exception:
            mask &= df["title"].fillna("").str.lower().str.contains(
                title_q.lower(), regex=False, na=False
            )

        if author and author.strip():
            try:
                mask &= df["author"].fillna("").str.contains(
                    author.strip(), case=False, regex=True, na=False
                )
            except Exception:
                pass

        out = df[mask].copy()
        # Rank by downloads desc when available; ties by id asc.
        if "downloads" in out.columns:
            out["_dl"] = pd.to_numeric(out["downloads"], errors="coerce").fillna(0)
            out = out.sort_values(["_dl", "id"], ascending=[False, True])
        else:
            out = out.sort_values("id")

        keep_cols = [c for c in ("id", "title", "author", "downloads",
                                 "authoryearofbirth", "language")
                     if c in out.columns]
        matches = []
        for _, row in out.head(top).iterrows():
            m = {c: row[c] for c in keep_cols}
            if "downloads" in m:
                try:
                    m["downloads"] = int(float(m["downloads"]))
                except (TypeError, ValueError):
                    m["downloads"] = None
            matches.append(m)

        _log(f"find_book(title={title_q!r}, author={author!r}) "
             f"matched {len(out)} in {time.perf_counter()-t0:.2f}s")
        return {
            "title_query":   title_q,
            "author_filter": author or None,
            "total_matches": int(len(out)),
            "matches":       matches,
        }
    except Exception as e:
        return {"error": "find_book failed", "details": str(e)}


# ============================ TOOL: word_etymology ============================
# Wiktionary language codes → broad family bucket. The Wiktionary {{inh}} /
# {{der}} / {{bor}} templates use these short codes; we extract them with regex
# and bucket into language families for queries like "germanic words in Tolkien".
ETYMOLOGY_FAMILY_MAP = {
    # Germanic chain (inherited)
    "ang":     "old_english",          # Old English
    "enm":     "middle_english",       # Middle English
    "gmw-pro": "proto_germanic",       # Proto-West-Germanic
    "gem-pro": "proto_germanic",       # Proto-Germanic
    "got":     "germanic",             # Gothic
    # Norse
    "non":     "old_norse",
    "non-oks": "old_norse",
    "is":      "old_norse",            # Icelandic (often cited as a Norse cognate)
    # Other Germanic cognates that count as "germanic origin" when source
    "ofs":     "germanic",             # Old Frisian
    "osx":     "germanic",             # Old Saxon
    "goh":     "germanic",             # Old High German
    "odt":     "germanic",             # Old Dutch
    "nl":      "germanic",             # Dutch
    "de":      "germanic",             # German
    "da":      "germanic",             # Danish
    "no":      "germanic",             # Norwegian
    "sv":      "germanic",             # Swedish
    "fo":      "germanic",             # Faroese
    "nrn":     "germanic",             # Norn
    # Latin / Romance
    "la":      "latin",                # Latin
    "ML.":     "latin",                # Medieval Latin (template uses code la sometimes)
    "VL.":     "latin",                # Vulgar Latin
    "fro":     "old_french",           # Old French
    "frm":     "middle_french",        # Middle French
    "fr":      "french",               # French
    "es":      "spanish",
    "it":      "italian",
    "pt":      "portuguese",
    "roa-opt": "old_portuguese",
    # Greek
    "grc":     "ancient_greek",
    "el":      "greek",
    # Celtic
    "ga":      "celtic",               # Irish
    "gd":      "celtic",               # Scottish Gaelic
    "cy":      "celtic",               # Welsh
    "cel-pro": "proto_celtic",
    "owl":     "celtic",
    "sga":     "old_irish",
    "xbm":     "middle_breton",
    # Slavic
    "ru":      "slavic",
    "pl":      "slavic",
    "sla-pro": "proto_slavic",
    "cu":      "slavic",               # Old Church Slavonic
    # Proto-Indo-European at the root
    "ine-pro": "proto_indo_european",
    # Semitic / Arabic loans
    "ar":      "arabic",
    "he":      "hebrew",
    # Other notable loan sources
    "hi":      "hindi",
    "sa":      "sanskrit",
    "ja":      "japanese",
    "zh":      "chinese",
    "tr":      "turkish",
    "fi":      "uralic",
    "hu":      "uralic",
}

# Native English/Germanic chain — when a word descends only through these
# the etymology is "native Germanic" (sword, hand, mother). Any external
# family appearing in the chain means the word was loaned in (amber via
# Arabic, chivalry via French/Latin).
ETYMOLOGY_NATIVE_GERMANIC = {
    "old_english", "middle_english", "proto_germanic",
    "germanic", "old_norse", "proto_indo_european",
}

# Compact "Tolkien wanted these" buckets: which families count as
# "Germanic / Norse" for the high-level query.
ETYMOLOGY_FAMILY_GROUPS = {
    "germanic":        {"old_english", "middle_english", "proto_germanic",
                        "germanic", "old_norse"},
    "norse":           {"old_norse"},
    "romance":         {"latin", "old_french", "middle_french", "french",
                        "spanish", "italian", "portuguese", "old_portuguese"},
    "greek":           {"ancient_greek", "greek"},
    "celtic":          {"celtic", "proto_celtic", "old_irish", "middle_breton"},
    "slavic":          {"slavic", "proto_slavic"},
    "arabic":          {"arabic"},
    "hebrew":          {"hebrew"},
    "pie":             {"proto_indo_european"},
}

_ETYMOLOGY_TEMPLATE_RE = re.compile(
    r"\{\{(?:inh\+?|der|bor|lbor|cog|m)\|en\|([^|}\s]+)",
    re.IGNORECASE,
)
_ETYMOLOGY_SECTION_RE = re.compile(
    r"==+\s*Etymology\s*\d*\s*==+\s*\n(.*?)(?=\n==|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_ETYMOLOGY_CACHE_PATH = DERIVED_DIR / "word_etymology_cache.json"
_ETYMOLOGY_CACHE: dict | None = None


def _load_etymology_cache() -> dict:
    global _ETYMOLOGY_CACHE
    if _ETYMOLOGY_CACHE is not None:
        return _ETYMOLOGY_CACHE
    if _ETYMOLOGY_CACHE_PATH.exists():
        try:
            with open(_ETYMOLOGY_CACHE_PATH, encoding="utf-8") as fh:
                _ETYMOLOGY_CACHE = json.load(fh)
        except Exception:
            _ETYMOLOGY_CACHE = {}
    else:
        _ETYMOLOGY_CACHE = {}
    return _ETYMOLOGY_CACHE


def _save_etymology_cache():
    if _ETYMOLOGY_CACHE is None:
        return
    try:
        DERIVED_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ETYMOLOGY_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_ETYMOLOGY_CACHE, fh, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"failed to save etymology cache: {e}")


def word_etymology(word: str) -> dict:
    """Etymology breakdown for a single English word via Wiktionary wikitext.

    Returns:
        {"word": str, "family_chain": ["middle_english", "old_english", ...],
         "primary_family": "germanic"/"latin"/..., "raw_codes": [...],
         "wiktionary_url": str}

    Disk-cached at /data/spgc/derived/word_etymology_cache.json. Cold lookup
    via Wiktionary public API (~1.5s polite); cache makes subsequent calls
    instant. Bucketing follows the standard linguistic family taxonomy used
    by queries like "Germanic words in Tolkien".
    """
    word_lc = word.strip().lower()
    if not word_lc or " " in word_lc:
        return {"error": "word must be a single token"}

    cache = _load_etymology_cache()
    if word_lc in cache:
        out = dict(cache[word_lc])
        out["from_cache"] = True
        return out

    try:
        import urllib.request, urllib.parse
        url = ("https://en.wiktionary.org/w/api.php?action=parse&prop=wikitext"
               f"&format=json&page={urllib.parse.quote(word_lc)}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "wordcracker-etymology/1.0 (https://slovoeb.net)"
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
    except Exception as e:
        return {"error": "wiktionary fetch failed", "word": word_lc,
                "details": str(e)}

    if not wikitext:
        return {"error": "no wiktionary page", "word": word_lc}

    # Extract the English-section etymology block (first ==Etymology== inside
    # the ==English== heading). We rely on the language template prefix
    # `|en|...` inside `{{inh}}/{{der}}/{{bor}}` to anchor on English etymology.
    raw_codes: list[str] = []
    for m in _ETYMOLOGY_TEMPLATE_RE.finditer(wikitext):
        code = m.group(1).strip()
        if code and code not in raw_codes:
            raw_codes.append(code)
        if len(raw_codes) >= 40:
            break

    family_chain: list[str] = []
    seen_families = set()
    for code in raw_codes:
        fam = ETYMOLOGY_FAMILY_MAP.get(code, "")
        if fam and fam not in seen_families:
            family_chain.append(fam)
            seen_families.add(fam)

    # Primary family logic:
    # 1. If the chain has ANY family outside the English/Germanic native set,
    #    that's a loan word — pick the DEEPEST (latest in the chain, i.e. the
    #    most original source language) non-Germanic family. amber: ME →
    #    old_french → arabic ⇒ arabic. chivalry: ME → old_french → latin ⇒
    #    latin (romance).
    # 2. Otherwise the chain is pure Germanic descent (sword, hand) → germanic.
    non_native = [f for f in family_chain if f not in ETYMOLOGY_NATIVE_GERMANIC]
    primary_family = ""
    if non_native:
        # Deepest non-Germanic ancestor — last one in chain.
        deepest = non_native[-1]
        for group, members in ETYMOLOGY_FAMILY_GROUPS.items():
            if group in {"germanic", "norse", "pie"}:
                continue  # skip native groups when picking borrowing source
            if deepest in members:
                primary_family = group
                break
    elif family_chain:
        primary_family = "germanic"

    result = {
        "word": word_lc,
        "raw_codes": raw_codes[:20],
        "family_chain": family_chain,
        "primary_family": primary_family,
        "wiktionary_url": f"https://en.wiktionary.org/wiki/{word_lc}",
        "from_cache": False,
    }
    cache[word_lc] = {k: v for k, v in result.items() if k != "from_cache"}
    _save_etymology_cache()
    return result


def find_words_by_etymology(scope: dict, family: str, top: int = 30,
                            min_corpus_count: int = 200,
                            candidate_pool: int = 200) -> dict:
    """Words used by the author/book whose etymology matches a family
    (germanic / norse / romance / greek / celtic / slavic / arabic / pie).

    Workflow:
    1. affinity_by_author / affinity_by_book → high-affinity candidates
       (already PROPN- and OOV-filtered through min_corpus_count + spaCy POS).
    2. word_etymology(w) for each — pulls from cache or Wiktionary.
    3. Keep those whose primary_family equals `family`.
    4. Return top, sorted by author affinity.

    Beware: Wiktionary lookup is ~1.5s per cold word. First call on a fresh
    author scope may take 30-60 seconds (then cached). Subsequent calls
    instant.

    Note: affinity-based selection biases toward author-specific vocabulary,
    not general writing. For a baseline pool ("all germanic words this
    author uses") combine with word_collocates / top_ngrams_by_author and
    feed individual words through word_etymology.
    """
    family_lc = family.strip().lower()
    if family_lc not in ETYMOLOGY_FAMILY_GROUPS:
        return {"error": "unknown family",
                "supported": sorted(ETYMOLOGY_FAMILY_GROUPS.keys()),
                "got": family_lc}
    try:
        if isinstance(scope, dict) and scope.get("book"):
            from learning_tools import affinity_by_book
            aff_res = affinity_by_book(
                scope["book"], top=candidate_pool,
                min_corpus_count=max(min_corpus_count, 200),
            )
            key_count = "book_count"
        elif isinstance(scope, dict) and scope.get("author"):
            aff_res = affinity_by_author(
                scope["author"], top=candidate_pool,
                min_corpus_count=max(min_corpus_count, 200),
            )
            key_count = "author_count"
        else:
            return {"error": "bad scope; use {'book':PGid} | {'author':regex}"}

        if "error" in aff_res:
            return {"error": "affinity lookup failed", "details": aff_res["error"]}

        candidates = aff_res.get("top", [])
        if not candidates:
            return {"error": "no candidates", "scope": scope}

        matched = []
        looked_up = 0
        cold_lookups = 0
        for c in candidates:
            w = c["word"]
            cc = c.get("corpus_count", 0)
            if cc < min_corpus_count:
                continue
            ety = word_etymology(w)
            looked_up += 1
            if not ety.get("from_cache"):
                cold_lookups += 1
            if "error" in ety:
                continue
            if ety["primary_family"] == family_lc:
                matched.append({
                    "word": w,
                    "affinity": c.get("affinity"),
                    "occurrences": c.get(key_count),
                    "corpus_count": cc,
                    "family_chain": ety["family_chain"],
                    "raw_codes": ety["raw_codes"][:8],
                })
            if len(matched) >= top:
                break
        return {
            "scope": scope,
            "family": family_lc,
            "candidates_examined": looked_up,
            "cold_wiktionary_lookups": cold_lookups,
            "matched": matched,
        }
    except Exception as e:
        return {"error": "find_words_by_etymology failed", "details": str(e)}


# ============================ TOOL 12: top_authors_by ============================
# Placeholders/collective authors that pollute "most popular" lists.
# Check by first-comma-segment (the "Surname" slot in SPGC format), case-insensitive.
GENERIC_AUTHOR_FIRSTNAMES = {
    "various", "anonymous", "unknown", "anonymous (translator)",
    "catholic church", "church of england", "project gutenberg",
    "united states", "national gallery (great britain)",
}
# Fuzzy substring matches anywhere in author string (also lowercased).
GENERIC_AUTHOR_SUBSTRINGS = (
    "encyclopedia", "department of", "international organization",
    "library of congress",
)


def top_authors_by_country(country: str, metric: str = "books", top: int = 20,
                           lang: str = "en", include_generic: bool = False) -> dict:
    """Top N authors from a single country (ISO alpha-2 code), ranked by metric.

    For 'top British authors', 'most popular American writers', 'who's
    French in our corpus'. Underlying enrichment is the Sprint 9.2 Wikidata
    fetch; coverage grows as authors_geo.csv backfills.

    Returns each author with book_count, total_downloads, country_code.
    """
    if not country or not country.strip():
        return {"error": "country code required (e.g. 'GB', 'US', 'RU')"}
    cc = country.strip().upper()
    geo = _authors_geo_df()
    if geo is None:
        return {"error": "authors_geo.csv not present yet — Sprint 9.2 batch hasn't filled it"}
    geo_country = geo[geo["country_code"] == cc]
    if not len(geo_country):
        codes = sorted(set(geo["country_code"]) - {""})
        return {"error": f"no authors tagged {cc!r}",
                "available_codes_sample": codes[:30],
                "geo_coverage": int((geo["country_code"] != "").sum())}
    authors_set = set(geo_country["author"])

    df = _metadata_df()
    df = df[df["language"].fillna("").str.contains(f"'{lang}'", regex=False)]
    df = df[df["author"].isin(authors_set)]
    if not len(df):
        return {"error": "geo has authors but no matching books in metadata frame"}

    if metric == "downloads":
        df["downloads"] = pd.to_numeric(df["downloads"], errors="coerce").fillna(0)
        agg = df.groupby("author").agg(books=("id", "count"),
                                       downloads=("downloads", "sum"))
        agg = agg.sort_values("downloads", ascending=False)
    else:
        df["downloads"] = pd.to_numeric(df["downloads"], errors="coerce").fillna(0)
        agg = df.groupby("author").agg(books=("id", "count"),
                                       downloads=("downloads", "sum"))
        agg = agg.sort_values("books", ascending=False)

    rows = [{"author": idx, "books": int(r["books"]),
             "downloads": int(r["downloads"]), "country_code": cc}
            for idx, r in agg.head(top).iterrows()]
    return {"country": cc, "metric": metric, "top_n": top,
            "geo_coverage_for_country": int(len(geo_country)),
            "top": rows}


def top_authors_by(metric: str = "books", top: int = 10, lang: str = "en",
                   include_generic: bool = False) -> dict:
    """Top N authors by metric.

    metric:
      - 'books':     count of distinct books per author in the metadata table
      - 'downloads': sum of `downloads` column per author
      - 'tokens':    sum of SPGC counts per author (slower; aggregates files)

    include_generic=False (default): drop "Various / Anonymous / Unknown /
    Catholic Church / Encyclopedia" — these dominate raw counts but aren't
    what a user means by "most popular author".

    For 'кто самый популярный автор?' default 'books' if the user doesn't
    clarify what 'popular' means; offer 'downloads' as a follow-up.
    """
    t0 = time.perf_counter()
    if metric not in ("books", "downloads", "tokens"):
        return {"error": f"unknown metric: {metric!r} (use 'books'|'downloads'|'tokens')"}
    try:
        df = _metadata_df()
        df = df[df["language"].fillna("").str.contains(f"'{lang}'", regex=False)]
        df = df[df["author"].notna() & (df["author"].str.strip() != "")]
        if not include_generic:
            # split on first comma → "Various" or "Lytton" etc., lowercased
            head = df["author"].str.split(",").str[0].str.strip().str.lower()
            mask_set = head.isin(GENERIC_AUTHOR_FIRSTNAMES)
            mask_sub = df["author"].str.lower().apply(
                lambda s: any(sub in s for sub in GENERIC_AUTHOR_SUBSTRINGS)
            )
            df = df[~(mask_set | mask_sub)]
        if metric == "books":
            grouped = df.groupby("author").size().reset_index(name="books")
            grouped = grouped.sort_values("books", ascending=False).head(top)
            rows = [{"author": r["author"], "books": int(r["books"])}
                    for _, r in grouped.iterrows()]
        elif metric == "downloads":
            df = df.copy()
            df["downloads"] = pd.to_numeric(df["downloads"], errors="coerce").fillna(0)
            grouped = df.groupby("author").agg(
                downloads=("downloads", "sum"),
                books=("id", "count"),
            ).reset_index()
            grouped = grouped.sort_values("downloads", ascending=False).head(top)
            rows = [{"author": r["author"],
                     "downloads": int(r["downloads"]),
                     "books": int(r["books"])}
                    for _, r in grouped.iterrows()]
        else:  # tokens
            tot: Counter = Counter()
            book_count: Counter = Counter()
            for _, row in df.iterrows():
                pg = row["id"]; author = row["author"]
                f = _counts_path(pg)
                if not f.exists():
                    continue
                with open(f, encoding="utf-8") as fh:
                    book_total = sum(int(line.split("\t", 1)[1]) for line in fh
                                     if "\t" in line)
                tot[author] += book_total
                book_count[author] += 1
            ranked = tot.most_common(top)
            rows = [{"author": a, "tokens": int(t),
                     "books_with_counts": int(book_count[a])}
                    for a, t in ranked]
        out = {"metric": metric, "top_n": top, "lang": lang, "top": rows}
        _log(f"top_authors_by({metric}) done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "top_authors_by failed", "details": str(e)}


# ============================ TOOL 13: top_books_by_downloads ============================
def top_books_by_downloads(top: int = 20, lang: str = "en",
                           author_regex: str | None = None) -> dict:
    """Top N most-downloaded books from SPGC metadata. Optional author filter."""
    t0 = time.perf_counter()
    try:
        df = _metadata_df()
        df = df[df["language"].fillna("").str.contains(f"'{lang}'", regex=False)]
        if author_regex:
            df = df[df["author"].fillna("").str.contains(author_regex, case=False, regex=True)]
        df = df.copy()
        df["downloads"] = pd.to_numeric(df["downloads"], errors="coerce").fillna(0)
        df = df.sort_values("downloads", ascending=False).head(top)
        rows = [{"id": r["id"],
                 "title": (r["title"] or "")[:120],
                 "author": r["author"] or "",
                 "downloads": int(r["downloads"])}
                for _, r in df.iterrows()]
        out = {"top_n": top, "lang": lang, "author_regex": author_regex, "top": rows}
        _log(f"top_books_by_downloads done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "top_books_by_downloads failed", "details": str(e)}


# ============================ TOOL 13b: top_books_by_recency ============================
def top_books_by_recency(top: int = 20, lang: str = "en",
                         author_regex: str | None = None,
                         metric: str = "pg_id") -> dict:
    """Top N most recent books.

    metric (default "pg_id"):
      - "pg_id": recently ADDED to PG (sort by PG id desc). PG id != pub_year.
      - "pub_year": newest by REAL publication year (Open Library enrichment,
                    Sprint 9.7). Books without OL pub_year are dropped — this
                    mode answers "what's the newest book by publication date".

    For "топ-10 свежих книг" / "что нового в PG" use pg_id (default).
    For "самые новые книги в библиотеке" / "post-1920 fiction" use pub_year.
    """
    t0 = time.perf_counter()
    try:
        df = _metadata_df()
        df = df[df["language"].fillna("").str.contains(f"'{lang}'", regex=False)]
        if author_regex:
            df = df[df["author"].fillna("").str.contains(author_regex, case=False, regex=True)]
        df = df.copy()

        if metric == "pub_year":
            if "pub_year" not in df.columns:
                return {"error": "pub_year column not present — Sprint 9.7 batch hasn't filled it yet",
                        "fallback": "use metric='pg_id'"}
            df["pub_year"] = pd.to_numeric(df["pub_year"], errors="coerce")
            df = df.dropna(subset=["pub_year"])
            if not len(df):
                return {"error": "no books with pub_year metadata yet — Sprint 9.7 batch is still filling",
                        "fallback": "use metric='pg_id'"}
            df = df.sort_values("pub_year", ascending=False).head(top)
            sort_label = "pub_year descending (real publication year, OL enrichment)"
        else:
            df["_pg_num"] = df["id"].str.extract(r"PG(\d+)").astype(float)
            df = df.dropna(subset=["_pg_num"])
            df = df.sort_values("_pg_num", ascending=False).head(top)
            sort_label = "PG id descending (recently added to Project Gutenberg)"

        df["downloads"] = pd.to_numeric(df["downloads"], errors="coerce").fillna(0)
        rows = [{"id": r["id"],
                 "title": (r["title"] or "")[:120],
                 "author": r["author"] or "",
                 "author_birth": (int(r["authoryearofbirth"])
                                  if pd.notna(r["authoryearofbirth"]) else None),
                 "pub_year": (int(r["pub_year"])
                              if "pub_year" in df.columns and pd.notna(r.get("pub_year")) else None),
                 "downloads": int(r["downloads"])}
                for _, r in df.iterrows()]
        out = {"top_n": top, "lang": lang, "author_regex": author_regex,
               "metric": metric, "sort": sort_label, "top": rows}
        if metric == "pg_id":
            out["note"] = ("PG id != publication year. "
                           "Use metric='pub_year' for real publication date.")
        _log(f"top_books_by_recency(metric={metric}) done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "top_books_by_recency failed", "details": str(e)}


# ============================ TOOL 14: author_metadata ============================
def author_metadata(author_regex: str) -> dict:
    """Quick metadata for an author: birth/death year, language, book count,
    total downloads, sample titles. For questions like «когда родился X» /
    «сколько у X книг» — use this BEFORE going to per-author tools."""
    t0 = time.perf_counter()
    try:
        if author_regex.strip() in _BROAD_REGEXES:
            return {"error": "regex too broad; use '^Surname,' format"}
        sel = _select_books(author_regex)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex}
        # extract author-level info (uniform across rows for a given author)
        yob = pd.to_numeric(sel["authoryearofbirth"], errors="coerce").dropna()
        yod = pd.to_numeric(sel["authoryearofdeath"], errors="coerce").dropna()
        dl = pd.to_numeric(sel["downloads"], errors="coerce").fillna(0)
        out = {
            "author_regex":  author_regex,
            "books_matched": int(len(sel)),
            "authors_matched": sorted(sel["author"].dropna().unique().tolist())[:10],
            "year_of_birth_min": int(yob.min()) if len(yob) else None,
            "year_of_death_max": int(yod.max()) if len(yod) else None,
            "total_downloads":   int(dl.sum()),
            "languages":         sorted({lang for lang in sel["language"].dropna().unique()})[:5],
            "sample_titles":     sel["title"].dropna().head(10).tolist(),
        }
        _log(f"author_metadata done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "author_metadata failed", "details": str(e)}


# ============================ Ollama tool schemas ============================
TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "corpus_overview",
        "description": (
            "Сколько всего книг в базе, сколько чанков в ChromaDB, какие источники, "
            "сводка SPGC baseline. Используй для вопросов «сколько книг в базе», "
            "«что у тебя за корпус», «какой объём данных»."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "semantic_search",
        "description": (
            "Семантический поиск по корпусу 1828 книг Project Gutenberg. "
            "Используй для вопросов «найди упоминания X», «где описывается Y». "
            "Возвращает релевантные фрагменты с цитатами и PG-ссылками."
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Поисковый запрос на любом языке"},
            "k": {"type": "integer", "description": "Сколько фрагментов вернуть (default 8)"},
            "author_filter": {"type": "string",
                              "description": "Опционально: regex для фильтра по автору, например '^Dostoyevsky,'"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "corpus_stats_by_author",
        "description": (
            "Агрегированная статистика по автору: количество книг, токенов, словарь, длиннейшая/короткая книга. "
            "Используй для вопросов «дай статистику по автору», «сколько у X книг»."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string",
                             "description": "Regex по колонке author, обычно '^Surname,' например '^Wodehouse,'"},
        }, "required": ["author_regex"]},
    }},
    {"type": "function", "function": {
        "name": "top_ngrams_by_author",
        "description": (
            "Топ N-грамм у автора (n=1 unigrams, n=2 bigrams, n=3 trigrams). "
            "Используй для «топ биграмм у X», «фирменные обороты», «частые связки слов», "
            "«какие прилагательные характерны для X» (pos_filter=['ADJ'])."
            "Стоп-слова и пунктуация уже отфильтрованы. "
            "Для эпохи передай year_from/year_to (e.g. 1837-1901 = Victorian) и "
            "author_regex='.*' если автор любой."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string", "description": "Regex, например '^Dostoyevsky,'. Use '.*' if filtering only by period/country."},
            "n":   {"type": "integer", "description": "1, 2 или 3"},
            "top": {"type": "integer", "description": "Сколько вернуть (default 20)"},
            "pos_filter": {"type": "array", "items": {"type": "string"},
                           "description": "Фильтр POS первой токена ngram: ['NOUN','VERB','ADJ','ADV','PROPN']"},
            "year_from": {"type": "integer", "description": "Начало периода (writing prime = author birth + 30). 1837 = Викторианский."},
            "year_to":   {"type": "integer", "description": "Конец периода. 1901 = Викторианский."},
            "country":   {"type": "string", "description":
                "ISO alpha-2 фильтр через Wikidata (Sprint 9.2): GB/US/RU/FR/... 'американская vs британская лексика'."},
        }, "required": ["author_regex"]},
    }},
    {"type": "function", "function": {
        "name": "affinity_by_author",
        "description": (
            "Фирменные слова автора по метрике affinity (частота у автора vs частота в корпусе). "
            "Используй для «фирменные слова X», «маркеры стиля», «характерная лексика»."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex":     {"type": "string", "description": "Regex, например '^Wodehouse,'"},
            "top":              {"type": "integer", "description": "Сколько вернуть (default 50)"},
            "min_author_count": {"type": "integer", "description": "Минимум встреч у автора (default 5)"},
            "min_corpus_count": {"type": "integer", "description":
                "Минимум встреч в корпусе (default 0). Поставь 100, чтобы отфильтровать OOV/имена собственные (oinos/dunwich/threepwood) когда они проскальзывают через NER."},
            "pos_filter": {"type": "array", "items": {"type": "string"},
                "description":
                "POS фильтр: ['ADJ'] = только характерные прилагательные, ['NOUN'] = существительные, ['VERB'] = глаголы. Используй для запросов «характерные прилагательные/глаголы автора»."},
        }, "required": ["author_regex"]},
    }},
    {"type": "function", "function": {
        "name": "word_contexts",
        "description": (
            "±N token контексты для слова у автора. "
            "Используй для «в каком контексте слово X», «как X использует слово Y», «приведи примеры»."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string", "description": "Regex, например '^Wodehouse,'"},
            "word":         {"type": "string", "description": "Искомое слово (lowercased)"},
            "window":       {"type": "integer", "description": "Размер окна в токенах (default 10)"},
            "max_samples":  {"type": "integer", "description": "Сколько примеров (default 5)"},
        }, "required": ["author_regex", "word"]},
    }},
    {"type": "function", "function": {
        "name": "compare_authors",
        "description": (
            "Сравнение двух авторов: топ фирменных слов каждого, пересечение, cosine similarity affinity-векторов. "
            "Используй для «сравни автора A и автора B», «насколько похожи X и Y»."
        ),
        "parameters": {"type": "object", "properties": {
            "author1_regex": {"type": "string", "description": "Regex автора 1, например '^Wodehouse,'"},
            "author2_regex": {"type": "string", "description": "Regex автора 2, например '^Doyle,'"},
            "top":           {"type": "integer", "description": "Сколько слов в топе каждого (default 20)"},
            "min_corpus_count": {"type": "integer", "description":
                "Минимум встреч слова в корпусе (default 100). Фильтрует OOV/имена (oinos/dunwich/threepwood). Поставь 0 для включения редких слов."},
        }, "required": ["author1_regex", "author2_regex"]},
    }},
]

TOOLS_SPEC += [
    {"type": "function", "function": {
        "name": "lexical_diversity",
        "description": (
            "Лексическая разнообразность (TTR + per-book averages). "
            "Используй для «какая лексическая плотность у X», «насколько разнообразный словарь у автора»."
        ),
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "object",
                      "description": "{'book': 'PG1342'} или {'author': '^Doyle,'} или строка 'all_corpus'"},
        }, "required": ["scope"]},
    }},
    {"type": "function", "function": {
        "name": "word_collocates",
        "description": (
            "Слова в окне ±N токенов вокруг target word. "
            "Используй для «слова рядом со sea у Melville», «соседи слова X», «collocates слова X у автора»."
            " Для запросов по эпохе («что соседствует с fog у викторианцев») передай "
            "scope={'author': '.*', 'year_from': 1837, 'year_to': 1901} — period фильтр "
            "через год рождения автора + 30 (расцвет творчества)."
        ),
        "parameters": {"type": "object", "properties": {
            "scope":  {"type": "object", "description":
                "{'book': PGid} | {'author': regex} | {'author': regex, 'year_from': YYYY, 'year_to': YYYY}. "
                "Use author='.*' to mean 'any author' when filtering only by period."},
            "word":   {"type": "string"},
            "window": {"type": "integer", "description": "размер окна в токенах (default 4)"},
            "top":    {"type": "integer", "description": "сколько вернуть (default 20)"},
            "exclude_stopwords": {"type": "boolean", "description": "пропускать the/a/of/... (default true)"},
        }, "required": ["scope", "word"]},
    }},
    {"type": "function", "function": {
        "name": "book_readability",
        "description": (
            "Flesch Reading Ease + Flesch-Kincaid Grade + CEFR-heuristic для одной книги. "
            "Используй для «какой уровень сложности у книги X», «насколько сложна Pride and Prejudice», "
            "«найди книги уровня B2-C1» (нужно вызвать для нескольких кандидатов и сравнить). "
            "Работает и для пользовательских книг (id вида U<n>)."
        ),
        "parameters": {"type": "object", "properties": {
            "pg_id": {"type": "string",
                      "description": "id книги: 'PG1342' (Gutenberg) | 'U5' (user upload) | просто '1342'"},
        }, "required": ["pg_id"]},
    }},
    {"type": "function", "function": {
        "name": "word_contexts_global",
        "description": (
            "Контексты слова из разных авторов сразу (semantic search по всему корпусу). "
            "Используй для «приведи примеры слова X у разных авторов», «как слово X используется в литературе»."
        ),
        "parameters": {"type": "object", "properties": {
            "word": {"type": "string", "description": "одно слово (single token)"},
            "k":    {"type": "integer", "description": "сколько разных авторов (default 12)"},
            "lang": {"type": "string", "description":
                "фильтр языка книги (default 'en'). Отфильтровывает омонимы из не-английских книг (ajar=учить в малайском)."},
        }, "required": ["word"]},
    }},
    {"type": "function", "function": {
        "name": "word_freq_timeline",
        "description": (
            "Частота слова по эпохам. "
            "Sprint 9.7: после OL pub_year enrichment по умолчанию (`basis='auto'`) ось времени "
            "= **РЕАЛЬНЫЙ год публикации** когда известен, иначе fallback на `authoryearofbirth + 30` "
            "(writing-prime proxy). Покрытие pub_year растёт по мере того как фоновый batch "
            "fetch_pub_year.py заполняет таблицу. "
            "\n"
            "Если хочется чистый timeline только по подтверждённым pub_year — `basis='pub_year'` "
            "(книги без OL hit выпадают). Для воспроизведения старого поведения — `basis='birth'`. "
            "\n"
            "Limitations: (1) переводы — OL даёт год оригинала; (2) post-1950 данных меньше "
            "(SPGC в основном pre-1950); (3) OL coverage ~85-95%. "
            "\n"
            "Используй для «как менялось значение awful», «когда radio стало массовым», "
            "«слова исчезнувшие после 1920»."
        ),
        "parameters": {"type": "object", "properties": {
            "word":         {"type": "string"},
            "bucket_years": {"type": "integer", "description": "default 25"},
            "min_books_per_bucket": {"type": "integer", "description": "default 3"},
            "basis":        {"type": "string", "description":
                "'auto' (default, pub_year || birth+30) | 'pub_year' (strict, drop unknown) | 'birth' (legacy)"},
        }, "required": ["word"]},
    }},
    {"type": "function", "function": {
        "name": "top_authors_by",
        "description": (
            "🆕 Топ-N авторов в корпусе по выбранной метрике. ОБЯЗАТЕЛЬНО используй для вопросов "
            "«кто самый популярный автор», «топ-N авторов по числу книг», «у кого больше всего книг». "
            "НЕ ПЫТАЙСЯ собрать ответ из corpus_stats_by_author — он работает на ОДНОГО автора."
        ),
        "parameters": {"type": "object", "properties": {
            "metric": {"type": "string",
                       "description": "'books' (по числу книг) | 'downloads' (по скачиваниям) | 'tokens' (по объёму, медленно)"},
            "top":    {"type": "integer", "description": "Сколько вернуть (default 10)"},
            "lang":   {"type": "string", "description": "Язык (default 'en')"},
        }, "required": ["metric"]},
    }},
    {"type": "function", "function": {
        "name": "top_authors_by_country",
        "description": (
            "🆕 Топ-N авторов из одной страны (Sprint 9.2 Wikidata enrichment). "
            "country = ISO alpha-2 code: GB / US / RU / FR / DE / IE / CA / ... "
            "Используй для «топ британских авторов», «самые популярные American writers», "
            "«ирландская литература в корпусе». Покрытие растёт по мере того как "
            "fetch_author_nationality.py заполняет authors_geo.csv."
        ),
        "parameters": {"type": "object", "properties": {
            "country": {"type": "string", "description":
                "ISO alpha-2: GB / US / RU / FR / DE / IE / CA / AU / NZ / ZA / IN / ..."},
            "metric":  {"type": "string", "description":
                "'books' (default) | 'downloads'"},
            "top":     {"type": "integer", "description": "default 20"},
            "lang":    {"type": "string", "description": "default 'en'"},
        }, "required": ["country"]},
    }},
    {"type": "function", "function": {
        "name": "top_books_by_downloads",
        "description": (
            "🆕 Топ-N самых скачиваемых книг Gutenberg по метадате (column 'downloads'). "
            "Используй для «топ-20 самых популярных книг», «самые скачиваемые книги X». "
            "Опционально с author_regex чтобы ограничить одним автором."
        ),
        "parameters": {"type": "object", "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string",  "description": "опционально: regex по author, например '^Dickens,'"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "top_books_by_recency",
        "description": (
            "🆕 Топ-N свежих книг. Два режима через metric:\n"
            "  - metric='pg_id' (default): недавно ДОБАВЛЕННЫЕ в Project Gutenberg "
            "(PG id desc). Это **дата добавления**, НЕ год публикации.\n"
            "  - metric='pub_year': по реальному году публикации из Open Library "
            "(Sprint 9.7 enrichment). Книги без OL pub_year выпадают. "
            "Используй для «самые новые книги по дате публикации», «post-1920 fiction»."
        ),
        "parameters": {"type": "object", "properties": {
            "top":          {"type": "integer", "description": "default 20"},
            "lang":         {"type": "string",  "description": "default 'en'"},
            "author_regex": {"type": "string",  "description": "опционально: regex по author"},
            "metric":       {"type": "string",  "description":
                "'pg_id' (when added to PG, default) | 'pub_year' (real publication year via OL)"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "author_metadata",
        "description": (
            "🆕 Биографическая метадата автора: годы жизни (рождение/смерть), количество книг, "
            "общее число скачиваний, языки, образцы названий. Используй для «когда родился X», "
            "«сколько у X книг и скачиваний», «о каком авторе мы говорим». Быстро (без агрегации SPGC)."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string", "description": "Regex по author, например '^Wodehouse,'"},
        }, "required": ["author_regex"]},
    }},
    {"type": "function", "function": {
        "name": "book_emotion_profile",
        "description": (
            "🆕 Эмоциональный профиль книги через NRC Emotion Lexicon (Sprint 9.4). "
            "Возвращает per-million частоту 8 эмоций (anger / anticipation / disgust / fear / "
            "joy / sadness / surprise / trust) + 2 valences (positive / negative), плюс "
            "share среди 8 primary emotions, и top anchor-words для каждой. "
            "Используй для «эмоциональный профиль Dracula», «насколько у По много слов "
            "страха», «позитивная/негативная окраска книги X»."
        ),
        "parameters": {"type": "object", "properties": {
            "pg_id": {"type": "string", "description": "PG/U id"},
        }, "required": ["pg_id"]},
    }},
    {"type": "function", "function": {
        "name": "emotion_collocates",
        "description": (
            "🆕 Collocates вокруг слов-якорей конкретной эмоции из NRC Lexicon. "
            "Для emotion='fear' anchor-слова это NRC fear-tagged terms которые ACTUALLY "
            "appear в scope (terror/dread/horror/fearful/panic...). Возвращает ±window "
            "neighbors этих anchor'ов, агрегированные. "
            "Используй для «слова страха у По», «контекст радости у Wodehouse», "
            "«мрачная/тревожная лексика автора»."
        ),
        "parameters": {"type": "object", "properties": {
            "scope":   {"type": "object", "description":
                "{'book': PGid} | {'author': regex, [year_from, year_to]}"},
            "emotion": {"type": "string", "description":
                "anger | anticipation | disgust | fear | joy | sadness | surprise | trust | positive | negative"},
            "window":  {"type": "integer", "description": "default 4"},
            "top":     {"type": "integer", "description": "default 25"},
        }, "required": ["scope", "emotion"]},
    }},
    {"type": "function", "function": {
        "name": "find_book",
        "description": (
            "🆕 Найти книгу по названию (substring/regex), optional author hint. "
            "Возвращает топ-N matches с PG/U id + title + author + downloads. "
            "**ОБЯЗАТЕЛЬНО вызывай** ПЕРЕД book_readability / affinity_by_book / "
            "lexical_diversity({'book':...}) / word_collocates({'book':...}) / "
            "learning_words({'book':...}) когда пользователь называет книгу по "
            "title (не по PG id). НЕ выдумывай PG id из памяти — в корпусе 75k книг."
        ),
        "parameters": {"type": "object", "properties": {
            "title":  {"type": "string", "description":
                "название или часть названия, например 'Crime and Punishment', "
                "'Pride and Prejudice', 'Hound of the Baskervilles'"},
            "author": {"type": "string", "description":
                "опциональный фильтр автора (regex) — например 'Dostoyevsky' для "
                "disambiguation 'Hamlet' (Shakespeare vs Updike)"},
            "top":    {"type": "integer", "description": "default 5"},
            "lang":   {"type": "string",  "description": "default 'en'"},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "word_etymology",
        "description": (
            "🆕 Этимология одного слова через Wiktionary: цепочка языков "
            "(middle_english → old_english → proto_germanic → ine_pro) и primary_family "
            "(germanic / latin / romance / norse / greek / celtic / slavic / arabic / pie). "
            "Используй для «откуда слово X», «какого происхождения слово Y». "
            "Кэшируется на диске; первый запрос ~1.5s, повторный мгновенно."
        ),
        "parameters": {"type": "object", "properties": {
            "word": {"type": "string", "description": "одно английское слово (single token)"},
        }, "required": ["word"]},
    }},
    {"type": "function", "function": {
        "name": "find_words_by_etymology",
        "description": (
            "🆕 Найти слова автора/книги по этимологическому происхождению. "
            "Использует learning_words(scope, level='advanced') как кандидатов, потом для каждого "
            "тянет этимологию через word_etymology() и оставляет только нужного family. "
            "Используй для «германские/скандинавские слова Толкина», «латинские заимствования у X», "
            "«какие слова древнегерманского происхождения часто использует Y». "
            "Первый запрос на свежего автора ~30-60 сек (Wiktionary lookups), затем кэш."
        ),
        "parameters": {"type": "object", "properties": {
            "scope":  {"type": "object", "description":
                "{'book': PGid} или {'author': regex}"},
            "family": {"type": "string", "description":
                "germanic | norse | romance | greek | celtic | slavic | arabic | pie"},
            "top":    {"type": "integer", "description": "default 30"},
            "min_corpus_count": {"type": "integer", "description":
                "минимум встреч в корпусе (default 500) — отсеивает редкие слова"},
        }, "required": ["scope", "family"]},
    }},
]

TOOL_DISPATCH = {
    "corpus_overview":          corpus_overview,
    "semantic_search":          semantic_search,
    "corpus_stats_by_author":   corpus_stats_by_author,
    "top_ngrams_by_author":     top_ngrams_by_author,
    "affinity_by_author":       affinity_by_author,
    "word_contexts":            word_contexts,
    "compare_authors":          compare_authors,
    "lexical_diversity":        lexical_diversity,
    "word_collocates":          word_collocates,
    "book_readability":         book_readability,
    "word_freq_timeline":       word_freq_timeline,
    "word_contexts_global":     word_contexts_global,
    "find_book":                find_book,
    "book_emotion_profile":     book_emotion_profile,
    "emotion_collocates":       emotion_collocates,
    "word_etymology":           word_etymology,
    "find_words_by_etymology":  find_words_by_etymology,
    "top_authors_by":           top_authors_by,
    "top_authors_by_country":   top_authors_by_country,
    "top_books_by_downloads":   top_books_by_downloads,
    "top_books_by_recency":     top_books_by_recency,
    "author_metadata":          author_metadata,
}


if __name__ == "__main__":
    # smoke: dispatch each tool with sane defaults
    print("Available tools:")
    for name, fn in TOOL_DISPATCH.items():
        first_doc_line = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        print(f"  {name:30s}  {first_doc_line}")
    print()
    print("TOOLS_SPEC entries:", len(TOOLS_SPEC))
    print(json.dumps([{"name": s["function"]["name"]} for s in TOOLS_SPEC], indent=2))
