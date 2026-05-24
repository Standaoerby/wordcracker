"""v2 lexical_richness_authors — Phase 3 W-8 (Stan 2026-05-22).

Stan demon: «у какого автора самый богатый словарный запас» was answered
by `top_authors_by(metric='tokens')` — that's text VOLUME, not richness.
Wells (1.4M tokens) topped the list because the SPGC corpus has more
Wells text indexed, not because Wells uses more unique words. Same as
asking «who's the smartest» and answering «who talked the longest».

What real lexical richness measures
-----------------------------------
We compute several length-normalised metrics:

  * Types (unique words across all books)
  * Tokens (total words across all books)
  * Hapax (count of types that appear exactly once)
  * TTR = Types / Tokens               — naive, biased downward with size
  * Hapax-ratio = Hapax / Types        — sensitive to one-off neologisms
  * Guiraud-R = Types / √Tokens        — classic length-normalised
                                          richness; stable across sizes
                                          (Guiraud 1954)
  * Yule-K  = 10^4 × (Σm²·V(m) − N) / N²
                                       — independent of text length;
                                          LOWER means richer vocabulary
                                          (Yule 1944)

We rank by Guiraud-R as the headline. It strikes the best precision /
interpretability trade-off and doesn't require lemmatisation (the
counts files store raw tokens).

Why not MTLD / vocd-D?
    Both require chunk-windowed scans of the original token sequence,
    which would mean re-tokenising every book per request. Out of
    scope for v1-on-counts-files architecture. Guiraud-R + Yule-K
    capture the same intuition with one-pass counts.

Cache strategy
--------------
Per-author types / hapax / Yule-K computation requires reading every
counts file per author once. With 75k books across ~10k authors that's
~3 minutes cold. The cache file `author_richness.json` is built offline
by `scripts/v2/build_author_richness.py` (analogous to author_tokens).

If the cache is missing the wrapper falls back to live scan with a
warning — same pattern as `top_authors_by`.

W-4 application
---------------
The author column from SPGC metadata is filtered through `drop_null_authors`
so commission / agency aggregates don't dominate (CIA, Warren Commission,
Library of Congress).
"""
from __future__ import annotations

import json
import logging
import math
import sys
from collections import Counter
from pathlib import Path

log = logging.getLogger("wordcracker.v2.tools.authors.lexical_richness")

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.tools._result_filters import (
    apply_filters,
    drop_null_authors,
)

_AUTHOR_RICHNESS_CACHE_PATH = Path("/workspace/spgc/derived/author_richness.json")
_author_richness_cache: dict | None = None


def _load_author_richness() -> dict | None:
    """Returns the JSON cache or None when file missing — caller falls back
    to live scan."""
    global _author_richness_cache
    if _author_richness_cache is not None:
        return _author_richness_cache
    if not _AUTHOR_RICHNESS_CACHE_PATH.exists():
        return None
    try:
        _author_richness_cache = json.loads(
            _AUTHOR_RICHNESS_CACHE_PATH.read_text(encoding="utf-8"))
        return _author_richness_cache
    except (OSError, json.JSONDecodeError):
        return None


def compute_richness_from_counts(counts: Counter[str]) -> dict:
    """Compute length-normalised vocabulary richness metrics.

    Given a Counter mapping word -> total count over the author's books,
    return a dict with tokens / types / hapax / TTR / hapax_ratio /
    guiraud_R / yule_K.

    The function is pure — no I/O — so the cache builder and on-demand
    live-scan both use the same math.
    """
    if not counts:
        return {
            "tokens": 0, "types": 0, "hapax": 0,
            "ttr": 0.0, "hapax_ratio": 0.0,
            "guiraud_r": 0.0, "yule_k": 0.0,
        }
    tokens = int(sum(counts.values()))
    types = int(len(counts))
    hapax = int(sum(1 for c in counts.values() if c == 1))
    # Yule-K — V(m) = number of types occurring m times.
    spectrum: Counter[int] = Counter(counts.values())
    sum_m2_vm = sum((m * m) * vm for m, vm in spectrum.items())
    if tokens > 0:
        yule_k = 10_000.0 * (sum_m2_vm - tokens) / (tokens * tokens)
    else:
        yule_k = 0.0
    return {
        "tokens": tokens,
        "types": types,
        "hapax": hapax,
        "ttr": round(types / max(1, tokens), 6),
        "hapax_ratio": round(hapax / max(1, types), 6),
        "guiraud_r": round(types / math.sqrt(max(1, tokens)), 4),
        "yule_k": round(yule_k, 4),
    }


def _live_scan_author_richness(top: int, lang: str,
                                include_generic: bool) -> list[dict]:
    """Cold path — compute richness on-demand by scanning per-book counts.

    Heavy (~3 min on full SPGC). Returns top-N rows by Guiraud-R after
    applying the null-authors / org filter. Used only when the cache file
    is absent.
    """
    try:
        from scripts.rag_tools import _metadata_df, _counts_path
    except ImportError:
        return []

    df = _metadata_df()
    if df is None or not len(df):
        return []
    # Same lang filter as v1 top_authors_by
    df = df[df["language"].fillna("").str.contains(f"'{lang}'", regex=False)]
    df = df[df["author"].notna() & (df["author"].str.strip() != "")]
    if not include_generic:
        from scripts.rag_tools import (
            GENERIC_AUTHOR_FIRSTNAMES,
            GENERIC_AUTHOR_SUBSTRINGS,
        )
        head = df["author"].str.split(",").str[0].str.strip().str.lower()
        mask_set = head.isin(GENERIC_AUTHOR_FIRSTNAMES)
        mask_sub = df["author"].str.lower().apply(
            lambda s: any(sub in s for sub in GENERIC_AUTHOR_SUBSTRINGS)
        )
        df = df[~(mask_set | mask_sub)]

    per_author: dict[str, Counter[str]] = {}
    for row in df.itertuples(index=False):
        author = getattr(row, "author", None)
        pg = getattr(row, "id", None)
        if not author or not pg:
            continue
        f = _counts_path(pg)
        if not f.exists():
            continue
        c = per_author.setdefault(author, Counter())
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    try:
                        c[parts[0]] += int(parts[1])
                    except ValueError:
                        continue
        except OSError:
            continue

    rows = []
    for author, counts in per_author.items():
        m = compute_richness_from_counts(counts)
        m["author"] = author
        m["books_with_counts"] = sum(1 for _ in df[df["author"] == author].itertuples())
        rows.append(m)
    rows.sort(key=lambda r: r.get("guiraud_r", 0.0), reverse=True)
    return rows[:top]


@tool(
    name="lexical_richness_authors",
    category="authors",
    description=(
        "Ранжирование авторов по нормированному богатству словарного запаса "
        "(Guiraud R = types / √tokens), а НЕ по объёму текста. "
        "Используй для «у кого самый богатый словарь», «лексическое богатство», "
        "«разнообразие лексики у авторов». Возвращает также hapax_ratio, "
        "yule_k, ttr — каждый ранжирует по-своему, headline-метрика Guiraud-R."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "top":             {"type": "integer",
                                 "description": "default 20"},
            "lang":            {"type": "string",
                                 "description": "default 'en'"},
            "include_generic": {"type": "boolean",
                                 "description": "включать ли 'Various/Anonymous' (default false)"},
            "min_books":       {"type": "integer",
                                 "description": "min books per author to qualify (default 5)"},
            "min_tokens":      {"type": "integer",
                                 "description": "min total tokens per author (default 100k)"},
        },
        "required": [],
    },
    requires=[],
    cost="heavy",
    cacheable=True,
    # W-4 reconciliation (2026-05-24) — extended _NULL_AUTHOR_TOKENS /
    # _NULL_AUTHOR_SUBSTRINGS with additional government / aggregate
    # buckets (World Bank, UN, Patent Office, encyclopaedia editors).
    # Bumping invalidates cached topcharts where these still ranked.
    wrapper_version="v2-w4-orgfilter-extended",
)
def lexical_richness_authors(top: int = 20, lang: str = "en",
                              include_generic: bool = False,
                              min_books: int = 5,
                              min_tokens: int = 100_000) -> ToolResult:
    query = {"top": top, "lang": lang, "include_generic": include_generic,
             "min_books": min_books, "min_tokens": min_tokens}

    cache = _load_author_richness()
    cache_hit = cache is not None

    if cache_hit:
        rows: list[dict] = []
        for author, m in cache.items():
            if not include_generic:
                low = (author or "").lower()
                if any(s in low for s in ("various", "anonymous", "unknown",
                                          "encyclop", "catholic church")):
                    continue
            row = dict(m)  # shallow copy
            row["author"] = author
            rows.append(row)
    else:
        rows = _live_scan_author_richness(
            top=max(top * 4, 50),  # pull a wider candidate set, filter then truncate
            lang=lang, include_generic=include_generic,
        )

    # Phase 3 W-4 — drop CIA / commission / library-of-congress aggregates
    # before ranking. Otherwise they tend to top the charts (huge agency
    # corpora with high hapax due to acronyms / case numbers / dates).
    rows, drops = apply_filters([drop_null_authors], rows)

    # Apply min-books / min-tokens floors — extremely small per-author
    # samples inflate Guiraud-R (one short text with rare jargon =>
    # tiny tokens, but rare types => artificially high R).
    qualified = [r for r in rows
                 if int(r.get("books_with_counts", 0) or 0) >= min_books
                 and int(r.get("tokens", 0) or 0) >= min_tokens]

    qualified.sort(key=lambda r: float(r.get("guiraud_r", 0.0) or 0.0),
                    reverse=True)
    top_rows = qualified[:top]

    warnings: list[ToolWarning] = []
    if not cache_hit:
        warnings.append(ToolWarning(
            "live_scan",
            "author_richness.json missing — used live scan (heavy). "
            "Run scripts/v2/build_author_richness.py to populate the cache."
        ))
    if drops:
        warnings.append(ToolWarning(
            "filtered_aggregates",
            f"dropped {drops.get('drop_null_authors', 0)} non-literary "
            f"aggregate authors (CIA / Library of Congress / commissions)",
        ))
    if not top_rows:
        warnings.append(ToolWarning(
            "empty_top",
            "no authors qualified — try lowering min_books or min_tokens",
        ))

    out = {
        "metric_primary": "guiraud_r",
        "metric_explanation": (
            "Guiraud R = types / √tokens. Length-normalised vocabulary "
            "richness. Higher = richer vocabulary. Independent of text "
            "length (mostly), unlike raw TTR."
        ),
        "metric_secondary": ["hapax_ratio", "yule_k", "ttr"],
        "yule_k_note": "Yule-K is INVERSE: lower = richer (rare-word load).",
        "lang": lang, "min_books": min_books, "min_tokens": min_tokens,
        "qualified_authors": len(qualified),
        "candidate_authors": len(rows),
        "top": top_rows,
        "_cache_hit": cache_hit,
        "_filter_drops": drops if drops else None,
    }
    result = ToolResult.success(
        tool="lexical_richness_authors", data=out,
        coverage=Coverage(books_matched=len(top_rows), books_total=-1),
        warnings=warnings, query=query,
    )
    _attach_richness_view(result, top_rows, top=top, cache_hit=cache_hit,
                          filter_drops=drops)
    return result


def _attach_richness_view(result: ToolResult, rows: list[dict], *,
                          top: int, cache_hit: bool,
                          filter_drops: dict) -> None:
    """View emission — ranked table with the headline Guiraud-R + the
    secondary metrics column-by-column."""
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason

        columns = ["rank", "author", "guiraud_r", "ttr",
                   "hapax_ratio", "yule_k", "tokens", "types"]
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=columns,
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru=(
                    "Нет авторов, удовлетворяющих фильтрам min_books / "
                    "min_tokens — попробуй снизить пороги."
                ),
                empty_message_en="No authors qualified at given thresholds.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return

        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            view_rows.append({
                "rank": i,
                "author": r.get("author") or "—",
                "guiraud_r": (f"{r.get('guiraud_r'):.2f}"
                              if isinstance(r.get("guiraud_r"), (int, float))
                              else "—"),
                "ttr": (f"{r.get('ttr'):.4f}"
                        if isinstance(r.get("ttr"), (int, float)) else "—"),
                "hapax_ratio": (f"{r.get('hapax_ratio'):.3f}"
                                if isinstance(r.get("hapax_ratio"),
                                                (int, float)) else "—"),
                "yule_k": (f"{r.get('yule_k'):.1f}"
                           if isinstance(r.get("yule_k"), (int, float))
                           else "—"),
                "tokens": r.get("tokens") or "—",
                "types": r.get("types") or "—",
            })

        caveats = [
            "Headline-метрика: Guiraud R = types / √tokens (выше = богаче).",
            "Yule-K — обратная: НИЖЕ значит богаче (мера распределения rare words).",
            "Это НЕ ранжирование по объёму текста — это нормированное богатство словаря.",
        ]
        if filter_drops:
            n = filter_drops.get("drop_null_authors", 0)
            if n:
                caveats.append(
                    f"Из ранжирования исключены {n} аггрегатных «авторов» "
                    f"(CIA / Library of Congress / комиссии — это не литераторы)."
                )
        if not cache_hit:
            caveats.append(
                "Live-scan (cache miss) — может занять до 3 минут. "
                "Запусти scripts/v2/build_author_richness.py для warm-cache."
            )
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=columns,
            headline="Лексическое богатство авторов (Guiraud R, нормировано)",
            requested_n=top,
            caveats=caveats,
            provenance=vb.make_provenance(
                requested={"top": top},
                returned={"count": len(view_rows)},
                filtered={"null_or_aggregate_authors":
                          (filter_drops or {}).get("drop_null_authors", 0)},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        log.exception("lexical_richness_authors view emission failed")


__all__ = [
    "compute_richness_from_counts",
    "lexical_richness_authors",
]
