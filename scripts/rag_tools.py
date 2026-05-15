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
CHROMA_PATH     = "/workspace/chroma_db"
COLLECTION_NAME = "gutenberg-index"
EMBEDDER_NAME   = "paraphrase-multilingual-MiniLM-L12-v2"
DERIVED_DIR     = Path("/workspace/spgc/derived")
SCRIPTS_DIR     = Path("/workspace/scripts")
CORPUS_COUNTS   = DERIVED_DIR / "corpus_counts.csv"
RAW_DIR         = Path("/workspace/raw_text")
USER_UPLOADS_META = DERIVED_DIR / "user_uploads_metadata.csv"
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


# mtime-aware cache so user uploads added by admin_server become visible to a
# long-running chat_server without needing a restart. SPGC dump never changes
# so its mtime is stable in practice; user_uploads_metadata.csv grows on upload.
_metadata_cache: dict = {"df": None, "spgc_mtime": 0.0, "user_mtime": 0.0}


def _metadata_df() -> pd.DataFrame:
    spgc_m = SPGC_METADATA.stat().st_mtime if SPGC_METADATA.exists() else 0.0
    user_m = USER_UPLOADS_META.stat().st_mtime if USER_UPLOADS_META.exists() else 0.0
    if (_metadata_cache["df"] is not None
            and _metadata_cache["spgc_mtime"] == spgc_m
            and _metadata_cache["user_mtime"] == user_m):
        return _metadata_cache["df"]
    df = pd.read_csv(SPGC_METADATA)
    if USER_UPLOADS_META.exists():
        try:
            ud = pd.read_csv(USER_UPLOADS_META)
            # align columns to SPGC schema; missing → NaN, extra → dropped
            for col in df.columns:
                if col not in ud.columns:
                    ud[col] = pd.NA
            ud = ud[df.columns]
            df = pd.concat([df, ud], ignore_index=True)
            _log(f"merged {len(ud)} user uploads into metadata ({len(df)} total)")
        except Exception as e:
            _log(f"failed to load user uploads metadata: {e}")
    _metadata_cache.update({"df": df, "spgc_mtime": spgc_m, "user_mtime": user_m})
    return df


def _select_books(author_regex: str, lang: str = "en") -> pd.DataFrame:
    df = _metadata_df()
    mask_lang = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
    mask_auth = df["author"].fillna("").str.contains(author_regex, case=False, regex=True)
    return df[mask_lang & mask_auth]


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
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDER_NAME, device="cuda")
        col = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)

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
def corpus_stats_by_author(author_regex: str) -> dict:
    """Aggregate per-author corpus stats from SPGC counts files."""
    t0 = time.perf_counter()
    try:
        sel = _select_books(author_regex)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex}

        total_tokens = 0
        per_book = []
        vocab = Counter()
        for pid, row in sel.iterrows():
            pg = row["id"]
            f = SPGC_COUNTS_DIR / f"{pg}_counts.txt"
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
                         pos_filter: list[str] | None = None) -> dict:
    """N-gram frequencies (n=1,2,3) from per-author SPGC tokens.

    pos_filter (only n=1): list like ["NOUN","VERB","ADJ","ADV","PROPN"]; keeps
    only unigrams whose spaCy POS is in the list. For n=2/3 the filter is
    applied to the FIRST token of the n-gram (typical use: "find adjective+noun
    bigrams" → pos_filter=["ADJ"]). On a large candidate pool the spaCy pass
    runs only on the top ~5x final size, so it stays cheap.
    """
    t0 = time.perf_counter()
    if n not in (1, 2, 3):
        return {"error": "n must be 1, 2 or 3"}
    try:
        sel = _select_books(author_regex)
        if not len(sel):
            return {"error": "no books matched", "author_regex": author_regex}

        counter: Counter = Counter()
        used = 0
        total_ngrams = 0
        for pid, row in sel.iterrows():
            pg = row["id"]
            f = SPGC_TOKENS_DIR / f"{pg}_tokens.txt"
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
def affinity_by_author(author_regex: str, top: int = 50, min_author_count: int = 5) -> dict:
    """Per-author affinity vs corpus. Uses cached CSV if present, else runs spgc_author_affinity.py."""
    t0 = time.perf_counter()
    slug = _slug(author_regex)
    csv_path = DERIVED_DIR / f"{slug}_affinity.csv"
    cached = csv_path.exists()
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

        df = pd.read_csv(csv_path)
        df = df[df["author_count"] >= min_author_count]
        df = df.sort_values("affinity", ascending=False, na_position="last").head(top)
        top_rows = [
            {"word": r["word"], "author_count": int(r["author_count"]),
             "corpus_count": int(r["corpus_count"]),
             "affinity": round(float(r["affinity"]), 2)}
            for _, r in df.iterrows() if pd.notna(r["affinity"])
        ]
        out = {
            "author_regex":       author_regex,
            "slug":               slug,
            "total_unique_words": int(len(pd.read_csv(csv_path))),
            "top":                top_rows,
            "cached":             cached,
        }
        _log(f"affinity_by_author done in {time.perf_counter()-t0:.2f}s (cached={cached})")
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
            f = SPGC_TOKENS_DIR / f"{pg}_tokens.txt"
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
def compare_authors(author1_regex: str, author2_regex: str, top: int = 20) -> dict:
    """Composition of affinity_by_author for two authors + cosine similarity of their affinity vectors."""
    t0 = time.perf_counter()
    try:
        a1 = affinity_by_author(author1_regex, top=top * 5)
        a2 = affinity_by_author(author2_regex, top=top * 5)
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

        out = {
            "author1": {"regex": author1_regex, "slug": a1["slug"], "top_unique": a1["top"][:top]},
            "author2": {"regex": author2_regex, "slug": a2["slug"], "top_unique": a2["top"][:top]},
            "shared_high_affinity": shared,
            "cosine_similarity":    cosine,
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
            if pg.startswith("U"):
                return {"error": "lexical_diversity requires SPGC counts; user-uploaded books "
                                 "(U-prefix) are not yet tokenized into SPGC counts format",
                        "id": pg}
            f = SPGC_COUNTS_DIR / f"{pg}_counts.txt"
            if not f.exists():
                return {"error": "counts file not found", "pg_id": pg}
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
                f = SPGC_COUNTS_DIR / f"{pg}_counts.txt"
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
    """Words that co-occur within ±window tokens of `word` in the scope's books."""
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc:
        return {"error": "word required"}
    try:
        if isinstance(scope, dict) and scope.get("book"):
            pg = scope["book"].upper()
            if not (pg.startswith("PG") or pg.startswith("U")): pg = f"PG{pg}"
            if pg.startswith("U"):
                return {"error": "word_collocates requires SPGC tokens; user-uploaded books "
                                 "(U-prefix) are not yet tokenized into SPGC tokens format",
                        "id": pg}
            book_ids = [pg]
            label = f"book:{pg}"
        elif isinstance(scope, dict) and scope.get("author"):
            sel = _select_books(scope["author"])
            if not len(sel):
                return {"error": "no books matched", "author_regex": scope["author"]}
            book_ids = list(sel["id"])
            label = f"author:{scope['author']}"
        else:
            return {"error": "bad scope; use {'book':PGid} | {'author':regex}"}

        neighbors: Counter = Counter()
        hits = 0
        books_with_hits = 0
        for pg in book_ids:
            f = SPGC_TOKENS_DIR / f"{pg}_tokens.txt"
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
                      lang: str = "en") -> dict:
    """Frequency of `word` across periods, bucketed by author birth year.

    Caveat: SPGC metadata has authoryearofbirth, not publication year. We use
    birth-year + ~30 (rough writing-prime) as a proxy. Books with missing or
    out-of-range birth year are dropped.
    """
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc:
        return {"error": "word required"}
    try:
        df = _metadata_df()
        mask_lang = df["language"].fillna("").str.contains(f"'{lang}'", regex=False)
        df = df[mask_lang].copy()
        df["birth"] = pd.to_numeric(df["authoryearofbirth"], errors="coerce")
        df = df[(df["birth"] >= 1500) & (df["birth"] <= 2020)]
        df["period_start"] = (df["birth"] // bucket_years * bucket_years).astype(int)

        buckets: dict = {}
        for period, group in df.groupby("period_start"):
            tok_total = 0
            occurrences = 0
            books_used = 0
            for pg in group["id"]:
                f = SPGC_COUNTS_DIR / f"{pg}_counts.txt"
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
                    "writing_prime_approx": f"~{int(period)+30}-{int(period)+bucket_years+30}",
                    "books":    books_used,
                    "total_tokens":  tok_total,
                    "occurrences":   occurrences,
                    "per_million":   round(1_000_000 * occurrences / tok_total, 2),
                }
        timeline = [buckets[k] for k in sorted(buckets)]
        return {
            "word": word_lc,
            "bucket_years": bucket_years,
            "axis_basis":   "authoryearofbirth (writing prime ≈ birth + 30)",
            "timeline": timeline,
        }
    except Exception as e:
        return {"error": "word_freq_timeline failed", "details": str(e)}
    finally:
        _log(f"word_freq_timeline({word_lc}) done in {time.perf_counter()-t0:.2f}s")


# ============================ TOOL 11: word_contexts_global ============================
def word_contexts_global(word: str, k: int = 12, snippet_chars: int = 280) -> dict:
    """Contexts of a target word from many authors at once.

    Uses the ChromaDB semantic index to fetch chunks that are likely to
    contain the word (we pad the query with surrounding text to bias the
    retriever), then filters to chunks that actually mention the word.
    Returns up to k samples, each with author/title/PG id + snippet around
    the first occurrence.
    """
    t0 = time.perf_counter()
    word_lc = word.strip().lower()
    if not word_lc or " " in word_lc:
        return {"error": "word must be a single token"}
    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDER_NAME, device="cuda")
        col = client.get_collection(COLLECTION_NAME, embedding_function=embed_fn)

        # ChromaDB has a where_document substring filter — use it so we only
        # retrieve chunks that literally contain the word. Then we rank what
        # comes back by semantic distance to a paraphrastic query.
        q = f"usage of the word {word_lc} in literature"
        fetch = max(k * 4, 40)
        try:
            res = col.query(query_texts=[q], n_results=fetch,
                            where_document={"$contains": word_lc})
        except Exception:
            # fallback if backend doesn't support where_document
            res = col.query(query_texts=[q], n_results=fetch * 4)

        seen_authors = set()
        out_samples = []
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
            if author in seen_authors:
                continue
            seen_authors.add(author)
            lo = max(0, idx - snippet_chars // 2)
            hi = min(len(doc), idx + len(word_lc) + snippet_chars // 2)
            snippet = doc[lo:hi].replace("\n", " ").strip()
            out_samples.append({
                "author":   author,
                "title":    md.get("title") or "",
                "pg_id":    md.get("pg_id") or "",
                "distance": round(float(dist), 4),
                "snippet":  snippet,
            })
            if len(out_samples) >= k:
                break
        return {"word": word_lc, "k": k, "samples": out_samples,
                "unique_authors": len(seen_authors)}
    except Exception as e:
        return {"error": "word_contexts_global failed", "details": str(e)}
    finally:
        _log(f"word_contexts_global({word_lc}) done in {time.perf_counter()-t0:.2f}s")


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
            "Стоп-слова и пунктуация уже отфильтрованы."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string", "description": "Regex, например '^Dostoyevsky,'"},
            "n":   {"type": "integer", "description": "1, 2 или 3"},
            "top": {"type": "integer", "description": "Сколько вернуть (default 20)"},
            "pos_filter": {"type": "array", "items": {"type": "string"},
                           "description": "Фильтр POS первой токена ngram: ['NOUN','VERB','ADJ','ADV','PROPN']"},
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
        ),
        "parameters": {"type": "object", "properties": {
            "scope":  {"type": "object", "description": "{'book': PGid} или {'author': regex}"},
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
        }, "required": ["word"]},
    }},
    {"type": "function", "function": {
        "name": "word_freq_timeline",
        "description": (
            "Частота слова по периодам (bucket по году рождения автора + ~30 лет = period of writing). "
            "Используй для «как менялось значение awful с XVIII по XX», "
            "«когда radio стало массовым в литературе». "
            "ВНИМАНИЕ: SPGC в основном pre-1950, после 1950 данных мало."
        ),
        "parameters": {"type": "object", "properties": {
            "word":         {"type": "string"},
            "bucket_years": {"type": "integer", "description": "default 25"},
            "min_books_per_bucket": {"type": "integer", "description": "default 3"},
        }, "required": ["word"]},
    }},
]

TOOL_DISPATCH = {
    "corpus_overview":        corpus_overview,
    "semantic_search":        semantic_search,
    "corpus_stats_by_author": corpus_stats_by_author,
    "top_ngrams_by_author":   top_ngrams_by_author,
    "affinity_by_author":     affinity_by_author,
    "word_contexts":          word_contexts,
    "compare_authors":        compare_authors,
    "lexical_diversity":      lexical_diversity,
    "word_collocates":        word_collocates,
    "book_readability":       book_readability,
    "word_freq_timeline":     word_freq_timeline,
    "word_contexts_global":   word_contexts_global,
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
