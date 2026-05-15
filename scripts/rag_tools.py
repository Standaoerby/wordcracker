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
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _metadata_df() -> pd.DataFrame:
    return pd.read_csv(SPGC_METADATA)


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


def top_ngrams_by_author(author_regex: str, n: int = 2, top: int = 20) -> dict:
    """N-gram frequencies (n=1,2,3) from per-author SPGC tokens, with stopword/length filters."""
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

        out = {
            "author_regex":  author_regex,
            "n":             n,
            "books_used":    used,
            "total_ngrams":  total_ngrams,
            "top":           [{"ngram": ng, "count": c} for ng, c in counter.most_common(top)],
        }
        _log(f"top_ngrams_by_author(n={n}) done in {time.perf_counter()-t0:.2f}s, "
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


# ============================ Ollama tool schemas ============================
TOOLS_SPEC = [
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
            "Используй для «топ биграмм у X», «фирменные обороты», «частые связки слов». "
            "Стоп-слова и пунктуация уже отфильтрованы."
        ),
        "parameters": {"type": "object", "properties": {
            "author_regex": {"type": "string", "description": "Regex, например '^Dostoyevsky,'"},
            "n":   {"type": "integer", "description": "1, 2 или 3"},
            "top": {"type": "integer", "description": "Сколько вернуть (default 20)"},
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

TOOL_DISPATCH = {
    "semantic_search":        semantic_search,
    "corpus_stats_by_author": corpus_stats_by_author,
    "top_ngrams_by_author":   top_ngrams_by_author,
    "affinity_by_author":     affinity_by_author,
    "word_contexts":          word_contexts,
    "compare_authors":        compare_authors,
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
