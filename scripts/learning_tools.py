#!/usr/bin/env python3
"""
learning_tools.py — vocabulary-learning tools for the wordcracker agent.

Goal: surface mid-frequency words a reader keeps stumbling over (CEFR B1-C1
band), enrich them via LLM with translation/POS/etymology/examples, and
export to Anki/markdown for spaced-repetition study.

Tools exported (loaded by rag_query agent alongside rag_tools):
  affinity_by_book(pg_id, ...)
  learning_words(scope, level, ...)
  enrich_word(word, contexts, target_lang)
  export_word_list(words, format, out_path)

Cache: /data/spgc/derived/word_dictionary.json — persistent enrich_word
results keyed by (word, target_lang). Hits are instant on repeat queries.
"""
import csv
import json
import math
import os
import re
import sys
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

# Reuse path constants from rag_tools to keep one source of truth
from rag_tools import (
    SPGC_METADATA, SPGC_COUNTS_DIR, SPGC_TOKENS_DIR,
    CHROMA_PATH, COLLECTION_NAME, EMBEDDER_NAME,
    DERIVED_DIR, CORPUS_COUNTS, OLLAMA_HOST,
    STOPWORDS, _metadata_df, _select_books, _slug,
    _is_clean_token, _log,
    _counts_path, _tokens_path,
)

# Spaced-repetition band-pass presets, anchored to corpus_count quantiles
LEVELS = {
    "basic":        {"min_corpus": 10000, "max_corpus": None,    "skip_top_n": 0},
    "intermediate": {"min_corpus": 100,   "max_corpus": 10000,   "skip_top_n": 1000},
    "advanced":     {"min_corpus": 10,    "max_corpus": 100,     "skip_top_n": 1000},
    "rare":         {"min_corpus": 2,     "max_corpus": 10,      "skip_top_n": 1000},
}

WORD_DICT_PATH = DERIVED_DIR / "word_dictionary.json"


# ============================ corpus_counts loader (cached) ============================
@lru_cache(maxsize=1)
def _corpus_counts() -> dict:
    """word -> corpus_count, loaded once per process."""
    out = {}
    with open(CORPUS_COUNTS, encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd)
        for w, c in rd:
            try:
                out[w] = int(c)
            except ValueError:
                continue
    return out


@lru_cache(maxsize=1)
def _corpus_total_tokens() -> int:
    return sum(_corpus_counts().values())


# ============================ word dictionary cache ============================
def _load_word_dict() -> dict:
    if WORD_DICT_PATH.exists():
        try:
            return json.loads(WORD_DICT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_word_dict(d: dict) -> None:
    WORD_DICT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                              encoding="utf-8")


# ============================ helper: per-book NER cache ============================
_BOOK_PROPN_DIR = Path("/workspace/spgc/derived/book_propn_cache")

# Entity types from spaCy en_core_web_sm we treat as proper nouns. Some
# (DATE, MONEY, QUANTITY, CARDINAL, ORDINAL, PERCENT) are intentionally
# excluded — they aren't names, even though spaCy tags them.
_PROPN_ENT_LABELS = {"PERSON", "ORG", "GPE", "LOC", "FAC", "NORP",
                     "PRODUCT", "WORK_OF_ART", "LAW", "EVENT", "LANGUAGE"}

_GUTENBERG_HDR = re.compile(
    r"\*\*\*\s*START\s+OF\s+(?:THE|THIS)\s+PROJECT\s+GUTENBERG\s+EBOOK[^*]*\*\*\*",
    re.IGNORECASE)
_GUTENBERG_FTR = re.compile(
    r"\*\*\*\s*END\s+OF\s+(?:THE|THIS)\s+PROJECT\s+GUTENBERG\s+EBOOK[^*]*\*\*\*",
    re.IGNORECASE)


def _book_propn_set(pg_id: str, sample_chars: int = 400_000) -> set[str]:
    """Set of lowercased tokens spaCy NER tagged as PERSON / GPE / LOC / ... in
    the book's raw text. Cached at /data/spgc/derived/book_propn_cache/<id>.json
    so the cost is paid once per book ever.

    For Pride and Prejudice this turns the affinity_by_book top from
        longbourn / darcy / bennet / lizzy / gracechurch / hertfordshire
    into actual Austen lexemes
        civility / elopement / discomposure / apologising / perverseness.

    Sample 400k chars (~70-100k tokens) — enough for NER to see every named
    entity in any plausibly-sized novel. spaCy en_core_web_sm CPU pass:
    ~3-10s per book. Result is a flat set of lowercased token strings.
    """
    pg = pg_id.upper()
    cache_path = _BOOK_PROPN_DIR / f"{pg}.json"
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as fh:
                return set(json.load(fh))
        except Exception as e:
            _log(f"propn cache read failed for {pg}: {e}")

    raw = Path(f"/workspace/raw_text/{pg.lower()}.txt")
    if not raw.exists():
        return set()
    try:
        text = raw.read_text(encoding="utf-8", errors="replace")
        m = _GUTENBERG_HDR.search(text)
        if m: text = text[m.end():]
        m = _GUTENBERG_FTR.search(text)
        if m: text = text[:m.start()]
        text = text[:sample_chars]

        import spacy
        try:
            nlp = spacy.load("en_core_web_sm", disable=["lemmatizer", "tagger", "parser", "attribute_ruler"])
        except OSError:
            return set()
        doc = nlp(text)
        propn = set()
        for ent in doc.ents:
            if ent.label_ not in _PROPN_ENT_LABELS:
                continue
            for tok in ent.text.split():
                t = re.sub(r"[^A-Za-z']+", "", tok).lower()
                if len(t) >= 3:
                    propn.add(t)

        _BOOK_PROPN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(propn), fh, ensure_ascii=False)
        except Exception as e:
            _log(f"propn cache write failed for {pg}: {e}")
        return propn
    except Exception as e:
        _log(f"_book_propn_set({pg}) failed: {e}")
        return set()


# ============================ TOOL: affinity_by_book ============================
def affinity_by_book(pg_id: str, top: int = 50,
                     min_author_count: int = 3,
                     min_corpus_count: int = 200,
                     pos_filter: list[str] | None = None,
                     exclude_proper_nouns: bool = True,
                     use_ner: bool = True) -> dict:
    """Affinity for a single book vs the global corpus.

    min_corpus_count: drop words with global corpus_count below this. Default 200
    filters out the proper-noun bleed-through that dominates raw per-book
    affinity (Longbourn/Hunsford/Pemberley in Pride and Prejudice would otherwise
    crowd out real stylistic markers). Set to 0 to include rare words.

    pos_filter: keep only words spaCy POS-tags as one of these (e.g. ['ADJ'] for
    "characteristic adjectives of this book"). When set, min_corpus_count auto-
    raises to >=1000 because spaCy mis-tags lowercased OOV proper nouns as
    NOUN/ADJ on single-word input.
    """
    t0 = time.perf_counter()
    pg_id = pg_id.upper() if not pg_id.startswith("PG") else pg_id
    if not pg_id.startswith("PG"):
        pg_id = f"PG{pg_id}"
    try:
        f = _counts_path(pg_id)
        if not f.exists():
            return {"error": "counts file not found for this PG id", "pg_id": pg_id}

        df = _metadata_df()
        row = df[df["id"] == pg_id]
        title  = row.iloc[0]["title"]  if len(row) else ""
        author = row.iloc[0]["author"] if len(row) else ""

        book = Counter()
        book_tokens = 0
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2:
                    continue
                w, c = parts[0], int(parts[1])
                book[w] = c
                book_tokens += c

        corpus = _corpus_counts()
        corpus_total = _corpus_total_tokens()

        effective_min_corpus = min_corpus_count
        if pos_filter:
            effective_min_corpus = max(effective_min_corpus, 1000)

        # Re-use the LLM self-learning proper-noun cache: any word flagged as a
        # proper noun by previous enrich_word calls is poison for stylistic
        # markers too. This catches book-specific names spaCy POS doesn't catch
        # on lowercased single-token input (longbourn/darcy/bennet etc.).
        # Plus the per-book NER pass (use_ner=True, default) — this is what
        # actually shoots character/place names dead for this specific book.
        known_proper: set[str] = set()
        if exclude_proper_nouns or pos_filter:
            try:
                cache = _load_word_dict()
                for key, info in cache.items():
                    if info.get("proper_noun"):
                        known_proper.add(key.split("|", 1)[0])
            except Exception as e:
                _log(f"failed to load proper-noun cache: {e}")
            if use_ner:
                known_proper |= _book_propn_set(pg_id)

        rows = []
        for w, bc in book.items():
            if bc < min_author_count:
                continue
            cc = corpus.get(w, 0)
            if cc < effective_min_corpus:
                continue
            if w in known_proper:
                continue
            if cc == 0:
                affinity = None
            else:
                affinity = (bc / book_tokens) / (cc / corpus_total)
            rows.append({"word": w, "book_count": bc, "corpus_count": cc,
                         "affinity": round(affinity, 2) if affinity else None})
        rows.sort(key=lambda r: (r["affinity"] is None, -(r["affinity"] or 0)))

        # Optional POS narrowing via spaCy. Run on top 8x pool so we have
        # candidates left after filtering. When pos_filter is set we narrow
        # to those tags; otherwise we still drop PROPN to suppress the
        # character/place-name flood typical of high-coverage cached books
        # (Pride and Prejudice → longbourn/pemberley/netherfield).
        if rows and (pos_filter or exclude_proper_nouns):
            try:
                from rag_tools import _spacy_pos_tags
                pool = rows[: top * 8]
                tags = _spacy_pos_tags([r["word"] for r in pool])
                if pos_filter:
                    allowed = {p.upper() for p in pos_filter}
                    rows = [r for r in pool if tags.get(r["word"], "") in allowed]
                else:
                    rows = [r for r in pool if tags.get(r["word"], "") != "PROPN"]
            except Exception as e:
                _log(f"spaCy POS filter failed in affinity_by_book: {e}")

        out = {"pg_id": pg_id, "title": title, "author": author,
               "book_tokens": book_tokens, "book_vocab": len(book),
               "pos_filter": pos_filter,
               "effective_min_corpus_count": effective_min_corpus,
               "top": rows[:top]}
        _log(f"affinity_by_book({pg_id}) done in {time.perf_counter()-t0:.2f}s")
        return out
    except Exception as e:
        return {"error": "affinity_by_book failed", "details": str(e)}


# ============================ TOOL: learning_words ============================
def _score(book_count: int, corpus_count: int, scope_tokens: int) -> float:
    """Composite ranking: log(corpus_count) * affinity. Damps proper-noun spikes."""
    if corpus_count <= 0 or scope_tokens <= 0:
        return 0.0
    corpus_total = _corpus_total_tokens() or 1
    affinity = (book_count / scope_tokens) / (corpus_count / corpus_total)
    return math.log10(corpus_count) * math.log10(1 + affinity)


def _learning_priority_score(*, scope_count: int, corpus_count: int,
                              scope_tokens: int, level: str,
                              has_context: bool, is_proper_noun: bool) -> float:
    """Sprint 8.1 learning_score from the v2 roadmap.

    Formula:
        0.30 * author_affinity
      + 0.20 * book_frequency
      + 0.20 * rarity
      + 0.15 * CEFR_relevance
      + 0.10 * context_quality
      + 0.05 * non_proper_noun_confidence

    Each component is normalized to [0, 1] before weighting so the final
    score sits in [0, 1] regardless of corpus size. The weights stay tuned
    to the original roadmap §8.2 spec.
    """
    if corpus_count <= 0 or scope_tokens <= 0:
        return 0.0
    corpus_total = _corpus_total_tokens() or 1

    # 1. author_affinity — log10-compressed so giant affinity outliers don't
    # dominate. affinity 100 → 1.0; affinity 1 → 0.0; affinity <1 → 0.0
    affinity = (scope_count / scope_tokens) / (corpus_count / corpus_total)
    affinity_norm = max(0.0, min(1.0, math.log10(max(1.0, affinity)) / 2.0))

    # 2. book_frequency — normalized in-scope rate. 1/1000 typical
    freq = scope_count / scope_tokens
    freq_norm = max(0.0, min(1.0, math.log10(1 + freq * 100_000) / 3.0))

    # 3. rarity — log-flip of corpus_count.
    rarity = max(0.0, min(1.0, 1.0 - math.log10(max(1, corpus_count)) / 8.0))

    # 4. CEFR relevance — boost when corpus_count band matches user level.
    # Level cmin / cmax from LEVELS — we evaluate whether the corpus_count
    # sits inside the requested level's window.
    band = LEVELS.get(level, LEVELS["intermediate"])
    cmin = band["min_corpus"]
    cmax = band["max_corpus"] or 10**12
    cefr_norm = 1.0 if cmin <= corpus_count <= cmax else 0.4

    # 5. context_quality — flat boost if we managed to fetch an example.
    ctx_norm = 1.0 if has_context else 0.5

    # 6. non_proper_noun_confidence — proper noun → 0, else 1.
    propn_norm = 0.0 if is_proper_noun else 1.0

    return (0.30 * affinity_norm
            + 0.20 * freq_norm
            + 0.20 * rarity
            + 0.15 * cefr_norm
            + 0.10 * ctx_norm
            + 0.05 * propn_norm)


def _lemmatize_words(words: list[str]) -> dict:
    """Word -> (lemma, pos). spaCy is loaded once per call."""
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    except Exception as e:
        _log(f"spacy load failed: {e}")
        return {w: (w, "") for w in words}
    out = {}
    for w in words:
        doc = nlp(w)
        if not len(doc):
            out[w] = (w, "")
        else:
            out[w] = (doc[0].lemma_, doc[0].pos_)
    return out


def learning_words(
    scope: dict | str,
    level: str = "intermediate",
    top: int = 50,
    lemmatize: bool = True,
    pos_filter: list[str] | None = None,
    min_corpus_ratio: float = 2.5,
) -> dict:
    """Words a reader would actually want to study from a given scope.

    scope: {"book": "PG1342"}  |  {"author": "^Doyle,"}  |  "all_corpus"
    level: "basic" | "intermediate" | "advanced" | "rare"
    pos_filter: e.g. ["NOUN", "VERB", "ADJ"]; None = no POS filter

    Returns ranked candidates with corpus_count, scope_count, affinity, score,
    plus lemma/POS hint if lemmatize=True. Result is what enrich_word should
    consume next.
    """
    t0 = time.perf_counter()
    if level not in LEVELS:
        return {"error": f"unknown level {level!r}", "available": list(LEVELS)}
    band = LEVELS[level]
    pos_filter = [p.upper() for p in (pos_filter or [])]

    # ---- aggregate scope_counts depending on scope type ----
    scope_counts: Counter = Counter()
    scope_tokens = 0
    scope_label = ""
    try:
        if isinstance(scope, str) and scope == "all_corpus":
            corpus = _corpus_counts()
            scope_counts = Counter(corpus)
            scope_tokens = _corpus_total_tokens()
            scope_label = "all_corpus"
        elif isinstance(scope, dict) and scope.get("book"):
            pg_id = scope["book"]
            if not pg_id.startswith("PG") and not pg_id.startswith("U"):
                pg_id = f"PG{pg_id}"
            # _counts_path knows about user_counts fallback for post-2018
            # orphan PG additions (PG68283, PG70652, etc) and U-uploads.
            f = _counts_path(pg_id)
            if not f.exists():
                return {"error": "counts file not found", "pg_id": pg_id}
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c = parts[0], int(parts[1])
                    scope_counts[w] = c
                    scope_tokens += c
            scope_label = f"book:{pg_id}"
        elif isinstance(scope, dict) and scope.get("author"):
            sel = _select_books(scope["author"])
            if not len(sel):
                return {"error": "no books matched", "author_regex": scope["author"]}
            for pg in sel["id"]:
                f = _counts_path(pg)
                if not f.exists():
                    continue
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) != 2:
                            continue
                        w, c = parts[0], int(parts[1])
                        scope_counts[w] += c
                        scope_tokens += c
            scope_label = f"author:{scope['author']}"
        else:
            return {"error": "bad scope; use {'book': PGid} or {'author': regex} or 'all_corpus'"}
    except Exception as e:
        return {"error": "scope aggregation failed", "details": str(e)}

    if not scope_tokens:
        return {"error": "scope has no tokens", "scope": scope_label}

    # ---- which books to scan for example contexts ----
    if isinstance(scope, dict) and scope.get("book"):
        pg_id = scope["book"]
        if not pg_id.startswith("PG"):
            pg_id = f"PG{pg_id}"
        context_book_ids = [pg_id]
    elif isinstance(scope, dict) and scope.get("author"):
        sel = _select_books(scope["author"])
        context_book_ids = list(sel["id"])
    else:
        context_book_ids = []

    # ---- band-pass filter + ranking ----
    corpus = _corpus_counts()
    # the top-N basic words to skip
    skip_top_n = band["skip_top_n"]
    basic_set: set[str] = set()
    if skip_top_n:
        # cheap top-N: read pre-sorted CSV header row order
        with open(CORPUS_COUNTS, encoding="utf-8") as fh:
            rd = csv.reader(fh); next(rd)
            for i, (w, _c) in enumerate(rd):
                if i >= skip_top_n:
                    break
                basic_set.add(w)

    cmin = band["min_corpus"]
    cmax = band["max_corpus"] or 10**12

    # learn from past enrich_word verdicts — anything already tagged as a proper
    # noun in the dictionary cache is poison for vocabulary study
    known_proper: set[str] = set()
    cache = _load_word_dict()
    for key, info in cache.items():
        if info.get("proper_noun"):
            known_proper.add(key.split("|", 1)[0])

    # v1.1.5 — book-scope queries also get the per-book spaCy NER pass that
    # affinity_by_book uses. Q09/Q13 regression: 'B1-B2 words from P&P'
    # returned darcy/bennet/lizzy/gardiner — they weren't in word_dictionary
    # cache (never enriched) so the previous filter missed them. NER on the
    # raw text catches them deterministically.
    if isinstance(scope, dict) and scope.get("book") and context_book_ids:
        try:
            for pg_id in context_book_ids:
                known_proper |= _book_propn_set(pg_id)
        except Exception as e:
            _log(f"per-book NER skip in learning_words: {e}")

    candidates = []
    for w, sc in scope_counts.items():
        if sc < 3 or len(w) < 3:
            continue
        if w in STOPWORDS or w in basic_set or w in known_proper:
            continue
        if not _is_clean_token(w):
            continue
        cc = corpus.get(w, 0)
        if cc < cmin or cc > cmax:
            continue
        # heuristic: words whose corpus_count is close to scope_count are
        # author-unique → almost always proper nouns we don't want for study
        if cc < min_corpus_ratio * sc:
            continue
        candidates.append((w, sc, cc, _score(sc, cc, scope_tokens)))

    candidates.sort(key=lambda r: r[3], reverse=True)
    candidates = candidates[: top * 2 if lemmatize else top]  # extra room before POS filter

    # ---- optional lemmatize + POS filter ----
    if lemmatize and candidates:
        words = [c[0] for c in candidates]
        lem = _lemmatize_words(words)
        filtered = []
        seen_lemmas: set[str] = set()
        for w, sc, cc, scr in candidates:
            lemma, pos = lem.get(w, (w, ""))
            if pos_filter and pos not in pos_filter:
                continue
            if lemma in seen_lemmas:
                continue
            seen_lemmas.add(lemma)
            filtered.append((w, sc, cc, scr, lemma, pos))
        candidates_out = filtered[:top]
        rows = [{"word": w, "lemma": lemma, "pos": pos,
                 "scope_count": sc, "corpus_count": cc,
                 "affinity": round((sc / scope_tokens) / (cc / _corpus_total_tokens()), 2),
                 "score": round(scr, 3)}
                for w, sc, cc, scr, lemma, pos in candidates_out]
    else:
        rows = [{"word": w, "lemma": w, "pos": "",
                 "scope_count": sc, "corpus_count": cc,
                 "affinity": round((sc / scope_tokens) / (cc / _corpus_total_tokens()), 2),
                 "score": round(scr, 3)}
                for w, sc, cc, scr in candidates[:top]]

    # ---- attach example contexts so enrich_word / LLM see how the word is used ----
    if context_book_ids and rows:
        target_words = {r["word"] for r in rows}
        word_ctx: dict[str, str] = {}
        for pg_id in context_book_ids:
            if not target_words:
                break
            f = _tokens_path(pg_id)
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                toks = [t.strip() for t in fh if t.strip()]
            for i, t in enumerate(toks):
                tl = t.lower()
                if tl in target_words and tl not in word_ctx:
                    lo, hi = max(0, i - 8), min(len(toks), i + 9)
                    word_ctx[tl] = " ".join(toks[lo:i] + [f"[{tl.upper()}]"] + toks[i+1:hi])
                    target_words.discard(tl)
        for r in rows:
            r["example_context"] = word_ctx.get(r["word"], "")

    out = {
        "scope": scope_label, "level": level,
        "band": {"min_corpus": cmin, "max_corpus": band["max_corpus"], "skip_top_n": skip_top_n},
        "scope_tokens": scope_tokens, "scope_vocab": len(scope_counts),
        "lemmatize": lemmatize, "pos_filter": pos_filter,
        "results": rows,
    }
    _log(f"learning_words({scope_label}, {level}) done in {time.perf_counter()-t0:.2f}s, "
         f"{len(rows)} returned")
    return out


# ============================ TOOL: enrich_word ============================
ENRICH_PROMPT = """You are a literary vocabulary tutor. The user is reading
English literature and needs to learn a mid-frequency word.

Word: {word}
Lemma hint: {lemma}
POS hint: {pos}
Sample contexts from the book (lowercased, no punctuation):
{contexts}

Return JSON ONLY, no other text. Keys:
- "proper_noun": true if this is a character / place name (skip it), else false
- "lemma": canonical base form
- "pos": part of speech (noun/verb/adj/adv/...)
- "translation_{target_lang}": short translation (1-3 words)
- "definition_en": one-sentence simple English definition (10-15 words)
- "example_sentence": one short example sentence using the word
- "etymology": short etymology hint (1 sentence, ok if approximate)
- "cefr_estimate": "A2"/"B1"/"B2"/"C1"/"C2"
- "archaic": true if the word is archaic / obsolete / no longer in everyday
    modern English (Victorian-era or earlier register only). False if still
    in current use. Examples of archaic=true: thee, thou, hath, ere, oft,
    nay, wherefore, betwixt, prithee, methinks, anon, forsooth, doth, ye,
    yon, hither, perchance. Examples archaic=false: house, run, terrible,
    candle, gentleman (still in use).
- "archaic_note": one short phrase explaining the archaism if archaic=true,
    otherwise empty string. e.g. "Middle English 2nd-person singular pronoun";
    "older 'before/until' conjunction"; "King-James-Bible style affirmation".
"""


def enrich_word(word: str, contexts: list[str] | None = None,
                target_lang: str = "ru", lemma_hint: str = "",
                pos_hint: str = "", force_refresh: bool = False) -> dict:
    """LLM-enrich a single word. Result cached on disk by (word, target_lang)."""
    t0 = time.perf_counter()
    key = f"{word.lower()}|{target_lang}"
    cache = _load_word_dict()
    if not force_refresh and key in cache:
        cached = cache[key]
        cached["_cached"] = True
        cached["_lookup_ms"] = round((time.perf_counter()-t0)*1000, 1)
        return cached

    ctx_block = "\n".join(f"- {c}" for c in (contexts or [])[:3]) or "(no contexts provided)"
    prompt = ENRICH_PROMPT.format(
        word=word, lemma=lemma_hint or word, pos=pos_hint or "?",
        contexts=ctx_block, target_lang=target_lang,
    )
    payload = {
        "model": "qwen3:14b", "prompt": prompt, "stream": False,
        "keep_alive": "5m", "format": "json",
        "options": {"temperature": 0.2}, "think": False,
    }
    try:
        resp = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=90)
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        # Qwen sometimes wraps json in ```; strip
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": "LLM returned invalid JSON", "raw": raw[:300], "details": str(e)}
    except Exception as e:
        return {"error": "enrich_word failed", "details": str(e)}

    parsed["_cached"] = False
    parsed["_lookup_ms"] = round((time.perf_counter()-t0)*1000, 1)

    # persist
    cache[key] = {k: v for k, v in parsed.items() if not k.startswith("_")}
    _save_word_dict(cache)
    _log(f"enrich_word({word}) done in {time.perf_counter()-t0:.2f}s")
    return parsed


# ============================ TOOL: book_archaic_words ============================
# Curated seed list of well-known archaisms — every word here is auto-tagged
# archaic=True regardless of LLM verdict, so 'archaisms in Dracula' returns
# something useful immediately without first round-tripping every candidate
# through enrich_word.
_KNOWN_ARCHAISMS = {
    "thee", "thou", "thy", "thine", "ye", "yon", "yonder",
    "hath", "doth", "dost", "art", "wast", "wert", "shalt", "wilt",
    "ere", "oft", "nay", "yea", "aye",
    "wherefore", "whither", "whence", "hence", "thence", "hither", "thither",
    "betwixt", "amongst", "amidst", "athwart", "anon", "betimes",
    "prithee", "methinks", "perchance", "mayhap", "forsooth", "verily",
    "fain", "alack", "alas", "lo", "behold",
    "gainsay", "vouchsafe", "bespoke", "bestrid", "begat", "begot",
    "quoth", "saith", "speakest", "hearest", "knowest",
    "naught", "aught", "ought", "wot",
    "ado", "albeit", "anent", "haply", "natheless", "withal",
    "morrow", "yestreen", "eventide", "tarry",
    "varlet", "knave", "wench", "swain", "damsel", "yeoman", "burgess",
    "smite", "smote", "smitten", "wrought",
    "thrice", "fortnight", "sennight",
    "perforce", "betimes", "withal", "albeit",
    "ado", "bade", "clad", "spake", "girt", "girded",
}


def book_archaic_words(pg_id: str, top: int = 30,
                       min_book_count: int = 2,
                       enrich_unknown: bool = False,
                       enrich_budget: int = 20) -> dict:
    """Archaic / obsolete words used in one book.

    Strategy:
      1. Walk the book's counts file.
      2. Two sources of 'archaic=True' verdicts:
         - _KNOWN_ARCHAISMS curated seed list (instant, ~80 well-known forms)
         - word_dictionary.json cache: any previously-enriched word whose
           archaic flag was set True by the LLM (D36 enrich_word prompt).
      3. Optionally (enrich_unknown=True), for the next `enrich_budget`
         mid-frequency candidates not in either source, run enrich_word and
         tag the result. This is the slow path — disabled by default; agent
         turns it on when user explicitly asks for a deeper sweep.

    Returns sorted by book_count desc.

    For #10 'архаизмы в Dracula', 'устаревшие слова у Шекспира'.
    """
    t0 = time.perf_counter()
    pg = pg_id.upper()
    if not (pg.startswith("PG") or pg.startswith("U")):
        pg = f"PG{pg}"
    f = _counts_path(pg)
    if not f.exists():
        return {"error": "counts file not found", "id": pg}

    cache = _load_word_dict()
    cache_archaic: set[str] = set()
    cache_notes: dict[str, str] = {}
    for key, info in cache.items():
        if info.get("archaic") is True:
            w = key.split("|", 1)[0]
            cache_archaic.add(w)
            note = info.get("archaic_note") or ""
            if note:
                cache_notes[w] = note

    # 1) seed pass
    book_counts: dict[str, int] = {}
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 2:
                continue
            w, c = parts[0], int(parts[1])
            if c < min_book_count:
                continue
            book_counts[w] = c

    rows: list[dict] = []
    for w, c in book_counts.items():
        if w in _KNOWN_ARCHAISMS:
            rows.append({"word": w, "book_count": c,
                         "source": "seed",
                         "note": cache_notes.get(w, "")})
        elif w in cache_archaic:
            rows.append({"word": w, "book_count": c,
                         "source": "enrich_cache",
                         "note": cache_notes.get(w, "")})

    # 2) optional enrich pass for mid-frequency candidates
    enriched_count = 0
    if enrich_unknown:
        seen = {r["word"] for r in rows}
        # Look at moderately-rare words first (most likely archaisms hide in
        # the 50-500 corpus-count band).
        candidates = []
        try:
            from rag_tools import _corpus_counts
            corpus = _corpus_counts()
        except Exception:
            corpus = {}
        for w, c in book_counts.items():
            if w in seen:
                continue
            if not w.isalpha() or len(w) < 3:
                continue
            cc = corpus.get(w, 0) if corpus else 0
            if 30 <= cc <= 5000:
                candidates.append((w, c, cc))
        candidates.sort(key=lambda t: t[1], reverse=True)
        for w, c, cc in candidates[:enrich_budget]:
            res = enrich_word(w, contexts=[""], lemma_hint=w, pos_hint="")
            enriched_count += 1
            if res.get("archaic") is True:
                rows.append({"word": w, "book_count": c,
                             "source": "enrich_live",
                             "note": res.get("archaic_note", "")})

    rows.sort(key=lambda r: -r["book_count"])
    return {
        "id": pg,
        "checked_book_vocab": len(book_counts),
        "seed_or_cache_hits": len(rows) - sum(1 for r in rows if r["source"] == "enrich_live"),
        "enriched_now": enriched_count,
        "top": rows[:top],
        "_elapsed_s": round(time.perf_counter() - t0, 2),
    }


# ============================ TOOL: export_word_list ============================
def export_word_list(words: list[dict] | list[str], format: str = "anki_csv",
                     out_path: str | None = None,
                     target_lang: str = "ru",
                     deck_name: str = "wordcracker") -> dict:
    """Export words to Anki CSV / Anki .apkg / markdown / plain JSON.

    Auto-enriches missing entries from cache. The .apkg path uses genanki
    when available — it produces a one-shot importable deck that Anki opens
    without any field-mapping dialog (vs CSV which requires manual import
    + field selection). genanki is optional: if it's not installed, the
    apkg request falls back to anki_csv with a warning."""
    t0 = time.perf_counter()
    if format not in ("anki_csv", "anki_apkg", "markdown", "json"):
        return {"error": "format must be anki_csv / anki_apkg / markdown / json"}

    # accept either list of strings or list of dicts (from learning_words.results)
    normalized = []
    for w in words:
        if isinstance(w, str):
            normalized.append({"word": w})
        elif isinstance(w, dict):
            normalized.append(w)
    cache = _load_word_dict()

    enriched = []
    for w in normalized:
        word = w.get("word") or w.get("lemma") or ""
        if not word:
            continue
        key = f"{word.lower()}|{target_lang}"
        info = cache.get(key, {})
        if info.get("proper_noun"):
            continue
        enriched.append({**w, **info})

    _ext = {"anki_csv": "csv", "anki_apkg": "apkg",
            "markdown": "md", "json": "json"}[format]
    out_path = out_path or str(DERIVED_DIR / f"export_{int(time.time())}.{_ext}")

    if format == "anki_apkg":
        try:
            import genanki
        except ImportError:
            _log("genanki not installed — falling back to anki_csv")
            format = "anki_csv"
            out_path = out_path.replace(".apkg", ".csv")
        else:
            # Stable IDs so re-imports update the same deck instead of creating
            # a duplicate. Hash from deck_name + a project-wide constant.
            import hashlib
            seed = int(hashlib.md5(f"wordcracker:{deck_name}".encode()).hexdigest()[:9], 16)
            model = genanki.Model(
                seed + 1,
                "wordcracker basic",
                fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Tags"}],
                templates=[{
                    "name": "Card 1",
                    "qfmt": "{{Front}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Back}}",
                }],
            )
            deck = genanki.Deck(seed, deck_name)
            for r in enriched:
                front = r.get("word", "")
                back_lines = [
                    f"<b>{r.get('translation_'+target_lang,'')}</b>",
                    f"<i>{r.get('pos','')}</i> · {r.get('cefr_estimate','')}",
                    f"{r.get('definition_en','')}",
                    f"<small>{r.get('example_sentence','')}</small>",
                    f"<small>etym: {r.get('etymology','')}</small>",
                ]
                back = "<br>".join(s for s in back_lines if s.strip("<>/biemall, "))
                tags_list = [t for t in ("wordcracker",
                                         r.get("pos", "").lower(),
                                         r.get("cefr_estimate", "").lower())
                             if t]
                deck.add_note(genanki.Note(
                    model=model, fields=[front, back, " ".join(tags_list)],
                    tags=tags_list,
                ))
            genanki.Package(deck).write_to_file(out_path)
            _log(f"export_word_list (apkg) -> {out_path} ({len(enriched)} entries) "
                 f"in {time.perf_counter()-t0:.2f}s")
            return {"out_path": out_path, "format": "anki_apkg",
                    "entries": len(enriched),
                    "skipped_proper_nouns": len(normalized) - len(enriched)}

    if format == "anki_csv":
        with open(out_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["front", "back", "tags"])
            for r in enriched:
                front = r.get("word", "")
                back_lines = [
                    f"<b>{r.get('translation_'+target_lang,'')}</b>",
                    f"<i>{r.get('pos','')}</i> · {r.get('cefr_estimate','')}",
                    f"{r.get('definition_en','')}",
                    f"<small>{r.get('example_sentence','')}</small>",
                    f"<small>etym: {r.get('etymology','')}</small>",
                ]
                back = "<br>".join(s for s in back_lines if s.strip("<>/biemall, "))
                tags = " ".join(["wordcracker", r.get("pos","").lower(), r.get("cefr_estimate","").lower()]).strip()
                w.writerow([front, back, tags])
    elif format == "markdown":
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("# Vocabulary export\n\n")
            for r in enriched:
                fh.write(f"## {r.get('word','')}\n")
                fh.write(f"- **lemma**: {r.get('lemma','')}  · **POS**: {r.get('pos','')}  · **CEFR**: {r.get('cefr_estimate','')}\n")
                fh.write(f"- **translation ({target_lang})**: {r.get('translation_'+target_lang,'')}\n")
                fh.write(f"- **definition**: {r.get('definition_en','')}\n")
                fh.write(f"- **example**: *{r.get('example_sentence','')}*\n")
                fh.write(f"- **etymology**: {r.get('etymology','')}\n\n")
    else:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(enriched, fh, ensure_ascii=False, indent=2)

    _log(f"export_word_list -> {out_path} ({len(enriched)} entries) in {time.perf_counter()-t0:.2f}s")
    return {"out_path": out_path, "format": format, "entries": len(enriched),
            "skipped_proper_nouns": len(normalized) - len(enriched)}


# ============================ Ollama tool schemas ============================
LEARNING_TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "affinity_by_book",
        "description": (
            "Фирменные слова конкретной книги (vs корпус). "
            "Используй для «топ слов из книги X», «характерные слова конкретного произведения», "
            "«слова используемые в этой книге намного чаще, чем в среднем по библиотеке». "
            "По умолчанию отфильтровывает имена персонажей/мест через min_corpus_count=200. "
            "Принимает PG id (например PG1342 — Pride and Prejudice)."
        ),
        "parameters": {"type": "object", "properties": {
            "pg_id": {"type": "string", "description": "PG id (с префиксом PG или без)"},
            "top": {"type": "integer", "description": "сколько вернуть (default 50)"},
            "min_author_count": {"type": "integer", "description": "минимум встреч в книге (default 3)"},
            "min_corpus_count": {"type": "integer", "description":
                "минимум встреч в корпусе (default 200). Поднимай чтобы сильнее отсечь имена/места (500/1000+)."},
            "pos_filter": {"type": "array", "items": {"type": "string"},
                "description":
                "POS: ['ADJ']/['NOUN']/['VERB']. Например 'характерные прилагательные книги'. При pos_filter автоматически min_corpus_count>=1000."},
        }, "required": ["pg_id"]},
    }},
    {"type": "function", "function": {
        "name": "learning_words",
        "description": (
            "Слова уровня B1-C1 для изучения из заданного источника (книги, автора или всего корпуса). "
            "Band-pass фильтр по частоте + лемматизация + POS-фильтр. "
            "Используй для «дай 100 слов из книги Х для изучения», «какие слова стоит выучить из Doyle». "
            "Параметр level: basic (>=10k частота), intermediate (100-10000, дефолт), advanced (10-100), rare (<10)."
        ),
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "object", "description": "{'book': 'PG1342'} или {'author': '^Doyle,'} или строка 'all_corpus'"},
            "level": {"type": "string", "enum": ["basic","intermediate","advanced","rare"]},
            "top": {"type": "integer", "description": "сколько слов (default 50)"},
            "lemmatize": {"type": "boolean", "description": "объединить формы (default true)"},
            "pos_filter": {"type": "array", "items": {"type": "string"}, "description": "напр. ['NOUN','VERB','ADJ']"},
        }, "required": ["scope"]},
    }},
    {"type": "function", "function": {
        "name": "enrich_word",
        "description": (
            "LLM-обогащение одного слова: перевод, POS, определение, пример, этимология, CEFR. "
            "Кеш на диске — повторные вызовы мгновенные. "
            "Используй после learning_words для деталей по конкретному слову, или напрямую: «что значит слово ajar в литературе»."
        ),
        "parameters": {"type": "object", "properties": {
            "word": {"type": "string"},
            "contexts": {"type": "array", "items": {"type": "string"}, "description": "до 3 sample фрагментов из книги"},
            "target_lang": {"type": "string", "description": "default 'ru'"},
            "lemma_hint": {"type": "string"},
            "pos_hint": {"type": "string"},
        }, "required": ["word"]},
    }},
    {"type": "function", "function": {
        "name": "book_archaic_words",
        "description": (
            "🆕 Архаичные / устаревшие слова конкретной книги (Sprint 9 #10). "
            "Использует два источника: curated seed list (thee/thou/hath/ere/anon/...) "
            "и enrich_word cache (LLM-tagged archaic). По умолчанию работает "
            "мгновенно по cache+seed; `enrich_unknown=true` дополнительно запускает "
            "enrich_word на mid-frequency кандидатах для глубокого sweep'а. "
            "Используй для «архаизмы в Dracula», «устаревшая лексика у Шекспира»."
        ),
        "parameters": {"type": "object", "properties": {
            "pg_id": {"type": "string", "description": "PG/U id"},
            "top":   {"type": "integer", "description": "default 30"},
            "min_book_count": {"type": "integer", "description":
                "минимум встреч в книге (default 2)"},
            "enrich_unknown": {"type": "boolean", "description":
                "default false — instant. true = LLM прогон mid-freq candidates, медленно"},
            "enrich_budget":  {"type": "integer", "description":
                "сколько кандидатов прогнать через enrich_word при enrich_unknown=true (default 20)"},
        }, "required": ["pg_id"]},
    }},
    {"type": "function", "function": {
        "name": "export_word_list",
        "description": (
            "Экспорт списка слов в Anki CSV / Markdown / JSON. "
            "Тянет данные из word_dictionary cache (запусти enrich_word на каждом слове предварительно). "
            "Используй для «экспортируй слова в Anki», «сохрани список для повторения»."
        ),
        "parameters": {"type": "object", "properties": {
            "words": {"type": "array", "items": {"type": "object"}, "description": "список слов (можно результат learning_words.results напрямую)"},
            "format": {"type": "string",
                       "enum": ["anki_csv", "anki_apkg", "markdown", "json"]},
            "out_path": {"type": "string"},
            "target_lang": {"type": "string", "description": "default 'ru'"},
        }, "required": ["words"]},
    }},
]

LEARNING_TOOL_DISPATCH = {
    "affinity_by_book":   affinity_by_book,
    "learning_words":     learning_words,
    "enrich_word":        enrich_word,
    "book_archaic_words": book_archaic_words,
    "export_word_list":   export_word_list,
}


if __name__ == "__main__":
    print("Learning tools:")
    for name in LEARNING_TOOL_DISPATCH:
        print(f"  - {name}")
    print()
    print("Levels:", list(LEVELS))
