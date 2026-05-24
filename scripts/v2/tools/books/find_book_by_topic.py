"""v2 find_book_by_topic — semantic book search by topic.

Sprint 16 Phase F. Sprint 19+ patches:
  - min_rerank_score filter — drops irrelevant matches after BGE rerank
    (Stan 2026-05-19 «магическая школа» surfaced PG29178 Little Red
    Riding Hood at rerank_score ~0.12 alongside legit hits at 0.6+)
  - Force-translate Russian queries → English before semantic search.
    Cross-lingual MiniLM is OK but precision lifts when the query is
    in the corpus's native language.
  - rerank_score in renderer output via _render_note.

Wraps `hybrid_search` and dedupes by pg_id so we return one entry per
BOOK (not per chunk). Useful for «найди книгу про викторианский Лондон»
/ «посоветуй роман о море» / «book about Victorian gas lamps» style
queries where the user wants candidate books, not specific passages.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import dispatch as v2_dispatch, tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.find_book_by_topic")


def _has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)


# E26 (2026-05-22) — Persona-beginner Q9 «что почитать после Дракулы»
# returned «Project Gutenberg (1971-2009)» as TOP result + a 1866
# bookseller's catalogue + Greek-myths textbook + «History of English
# Romanticism». Semantic search finds these because they contain lots
# of mentions of other books. They are valid corpus items but useless
# as REC results — meta-documents, bibliographies, catalogues, indexes.
# Apply a lightweight title-substring blocklist post-dedup so they
# drop OUT of recommendation lists. Filter is case-insensitive and
# matches partial titles. Keep the list short and high-precision —
# adding too many phrases risks false-positive drops of legitimate
# titles (e.g. «The Catalogue of Sins» is fiction).
_META_TITLE_BLOCKLIST: tuple[str, ...] = (
    "project gutenberg",          # «Project Gutenberg (1971-2009)»
    "gutenberg ebook",
    "gutenberg literary archive",
    "catalogue of",               # «Catalogue of London Books, 1866»
    "catalog of",
    "bibliograph",                # «Bibliography of …»
    "index of the project",
    "the index of",
    "table of contents",
    "list of titles",
    "list of works",
    "list of authors",
    "annual report",
    "encyclopædia",
    "encyclopedia of",
    "dictionary of",              # mostly reference works
    "manual of",                  # «Manual of English Literature»
    "history of english literat", # bibliography-class entries
    "history of english romant",
    "outline of",
    "primer of english",
    "the cambridge history",
    # Phase 4 W-14 (2026-05-23) — Stan test bench «что почитать после
    # Дракулы» surfaced «Греческие мифы» / «Anthology of …» in the top
    # alongside legit fiction. Expand blocklist with mythology /
    # textbook / anthology surface forms. KEEP narrow: «Tales from
    # Shakespeare» IS Lamb's children's fiction adaptation, so we
    # don't blocklist the bare «tales from». We anchor on mythology /
    # treasury / studies / lectures / essays / pageant — these surface
    # forms almost never name a novel.
    "myths and legends",
    "myths of the",
    "myths of greek",
    "myths of greece",
    "myths of rome",
    "greek mythology",
    "roman mythology",
    "norse mythology",
    "ancient mythology",
    "mythology of",
    "tales of the greek",
    "stories of greek",
    "stories from greek",
    "old greek stories",
    "history of literature",
    "history of fiction",
    "history of the novel",
    "studies in english",
    "studies in literature",
    "lectures on",
    "essays on english",
    "essays in literature",
    "guide to english",
    "treasury of english",
    "treasury of literature",
    "anthology of english",
    "anthology of literature",
    "selections from english",
    "readings from english",
    "english literature, an",       # «English Literature, An Illustrated…»
    "introduction to english",
    "introduction to literature",
    "schools of english",
    "course of english",
    "pageant of",
    "library of literary",
)


def _is_meta_title(title: str | None) -> bool:
    """Returns True if title smells like a bibliography/catalogue/index —
    bad as a recommendation, even if it ranked high semantically."""
    if not title:
        return False
    low = str(title).lower().strip()
    return any(phrase in low for phrase in _META_TITLE_BLOCKLIST)


def _translate_topic(topic: str) -> tuple[str, str | None]:
    """Force-translate Cyrillic topics to English via v1 helper.

    Returns (topic_to_use, original_if_translated). When the input is
    already ASCII / English, returns (topic, None) and skips the
    network round-trip.
    """
    if not _has_cyrillic(topic):
        return topic, None
    try:
        from scripts.rag_tools import _maybe_translate
    except ImportError:
        return topic, None
    try:
        translated = _maybe_translate(topic.strip())
        if translated and translated.lower() != topic.strip().lower():
            return translated, topic
    except Exception:
        pass
    return topic, None


@tool(
    name="find_book_by_topic",
    category="books",
    description=(
        "Семантический поиск книг по теме / topical match. «Найди книгу про "
        "викторианский Лондон», «book about gothic horror», «роман о море». "
        "Возвращает top-k уникальных книг с лучшим chunk-snippet и pg_id для "
        "цепочки в book-scoped tools. Не путать с find_book (title lookup). "
        "Sprint 19+: BGE rerank threshold + RU→EN translation для precision."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic":             {"type": "string", "description": "topical query на любом языке"},
            "top":               {"type": "integer", "description": "сколько уникальных книг (default 8)"},
            "per_retriever":     {"type": "integer", "description": "k for each retriever before dedup (default 30 — W-13 tightened from 60 to fit BGE rerank in ≤30s)"},
            "author_filter":     {"type": "string",  "description": "опциональный regex для фильтра автора"},
            "rerank_with":       {"type": "string",  "description": "опциональный плагин из scoring.REGISTRY, e.g. 'bge_reranker'"},
            "min_rerank_score":  {"type": "number",  "description": "drop matches with rerank_score below this (default 0.4, BGE normalized)"},
            "translate":         {"type": "boolean", "description": "auto-translate RU topic→EN before semantic search (default true)"},
        },
        "required": ["topic"],
    },
    requires=[],
    cost="heavy",
    # Phase 5 (REFACTOR_BRIEF): per-tool timeout overrides removed.
    # Effective cap = min(DEFAULT_TOOL_TIMEOUT_S, request_budget.remaining)
    # is enforced by tool_registry.dispatch chokepoint. Was timeout_s=45
    # (E11 fix). Cost stays «heavy» so the budget estimator still
    # downsizes / clarifies on heavy queries before dispatch.
    cacheable=True,
    # W-13 (Phase 5 P2, 2026-05-23) — bump wrapper_version so cache
    # entries computed under the old per_retriever=60 (which made
    # book_similar reliably 300+s with BGE rerank) get recomputed at
    # the tighter per_retriever=30 / k=max(top*3,30) budget. Previous
    # comment kept for context.
    # E26 (2026-05-22) / W-14 (2026-05-23) — META blocklist drops
    # bibliography / catalogue / mythology / textbook / anthology entries
    # from recommendation results. R-23 Tier 0: bump wrapper_version so
    # cached results recompute through the extended blocklist.
    # W-14 follow-up (Phase 5 P2, 2026-05-24) — META filter moved from
    # post-truncate to pre-dedup so it cannot get squeezed out by a
    # META-heavy top of the v1 rank (the actual Дракула case). Cached
    # results from before this change can include a META-only topN under
    # the old order; bump invalidates them.
    wrapper_version="v5-w14-pre-trunc-meta",
)
def find_book_by_topic(
    topic: str,
    top: int = 8,
    per_retriever: int = 30,
    author_filter: str | None = None,
    rerank_with: str | None = None,
    min_rerank_score: float = 0.4,
    translate: bool = True,
) -> ToolResult:
    if not topic or not topic.strip():
        return ToolResult.fail(
            tool="find_book_by_topic", err_type="invalid_args",
            message="topic is required",
            query={"topic": topic},
        )

    # Sprint 19+ — RU→EN translation pass. Cross-lingual MiniLM works
    # but precision improves substantially when the query is in EN
    # (the corpus is EN, embeddings cluster around English semantics).
    original_topic = topic
    translated_from = None
    if translate:
        topic, translated_from = _translate_topic(topic)

    # Delegate to hybrid_search — it handles RRF + optional rerank.
    # We always pull more chunks than `top` to leave room for dedup
    # AND for the rerank-threshold filter below.
    #
    # W-13 (Phase 5 P2, 2026-05-23) — k formula tightened from
    # max(top*4, 40) to max(top*3, 30). The old budget passed 40-80
    # chunks to BGE rerank → wall clock 30-300s on hot path. With
    # top=8 the new pool is 30 chunks which keeps BGE under 5s on
    # the lightweight model and still leaves enough headroom for
    # rerank-threshold drops + edition-dedup (W-17).
    sub_args: dict[str, Any] = {
        "query": topic,
        "k": max(top * 3, 30),
        "per_retriever": per_retriever,
    }
    if author_filter:
        sub_args["author_filter"] = author_filter
    if rerank_with:
        sub_args["rerank_with"] = rerank_with

    sub = v2_dispatch("hybrid_search", sub_args)
    query = {"topic": original_topic, "top": top, "per_retriever": per_retriever,
             "author_filter": author_filter, "rerank_with": rerank_with,
             "min_rerank_score": min_rerank_score, "translate": translate,
             "translated_from": translated_from}

    if not sub.ok:
        return ToolResult.fail(
            tool="find_book_by_topic",
            err_type=(sub.error.type if sub.error else "internal"),
            message=(sub.error.message if sub.error
                     else "hybrid_search failed"),
            query=query,
        )

    chunks = sub.data.get("matches", []) if isinstance(sub.data, dict) else []
    if not chunks:
        return ToolResult(
            ok=False, tool="find_book_by_topic", query=query,
            data={"matches": [], "total_chunks_seen": 0,
                  "books_returned": 0},
            warnings=[ToolWarning(
                code="no_topical_matches",
                message=f"no books matched topic {topic!r}",
            )],
            coverage=Coverage(books_matched=0, books_total=-1),
            error=None,
        )

    # Sprint 19+ — rerank-score threshold filter. BGE cross-encoder
    # gives a normalized 0-1 relevance score; <0.4 is typically noise
    # (Stan's «магическая школа» query landed Little Red Riding Hood
    # at ~0.12 because semantic search caught «school» without
    # «magic» context). Skip the filter when rerank didn't run.
    reranked_by = sub.data.get("reranked_by")
    if reranked_by and min_rerank_score > 0:
        chunks = [
            ch for ch in chunks
            if not isinstance(ch.get("rerank_score"), (int, float))
            or ch["rerank_score"] >= min_rerank_score
        ]

    # W-14 follow-up (Phase 5 P2, 2026-05-24) — drop META chunks BEFORE
    # the pg_id dedup truncates to `top`. Pre-fix order was: dedup-and-
    # truncate → dedup duplicates → META blocklist. When v1 ranked META-
    # docs at the top (Stan's «Дракула» case: PG-Gutenberg-index at
    # rerank=1.000 plus a bookseller's catalogue + Greek-myths textbook
    # right behind), the top-N truncation grabbed all META rows first
    # and fiction never made it into `seen` — META blocklist then
    # dropped everything, leaving an empty recommendation list. Filtering
    # at the chunk level fixes the squeeze: META chunks never compete
    # for a slot in the top-N. Cheap — `_is_meta_title` is a frozenset
    # substring check over a ~50-phrase list, near-instant even at the
    # per_retriever=30 candidate pool.
    meta_chunks_dropped = 0
    if chunks:
        before_meta = len(chunks)
        chunks = [ch for ch in chunks if not _is_meta_title(ch.get("title"))]
        meta_chunks_dropped = before_meta - len(chunks)

    # Dedup by pg_id — keep the best-scored chunk per book. Chunks are
    # already ordered by rrf_score (and rerank_score if reranker ran).
    seen: dict[str, dict] = {}
    for ch in chunks:
        pg = ch.get("pg_id")
        if not pg or pg in seen:
            continue
        seen[pg] = {
            "pg_id":         pg,
            "title":         ch.get("title"),
            "author":        ch.get("author"),
            "rrf_score":     ch.get("rrf_score"),
            "rerank_score":  ch.get("rerank_score"),
            "lexical_rank":  ch.get("lexical_rank"),
            "semantic_rank": ch.get("semantic_rank"),
            "snippet":       ch.get("snippet"),
        }
        if len(seen) >= top:
            break

    books = list(seen.values())
    # Sprint 20+ B18 — same book under different PG ids with identical
    # snippet (Stan Round 11 Q13: PG775 = PG12163 identical snippet,
    # rerank_score 0.416241). Dedup by snippet hash before truncation.
    # B10 — also collapse «Moby Dick» + «Moby Dick; Or, The Whale».
    filter_drops: dict = {}
    if books:
        from scripts.v2.tools._result_filters import (
            apply_filters, dedup_by_key, dedup_book_editions,
        )
        books, filter_drops = apply_filters([
            lambda r: dedup_by_key(r, key="snippet"),
            dedup_book_editions,
        ], books)
    # E26 (2026-05-22) — META blocklist defence-in-depth. Most META
    # rows already dropped at the chunk level above; this second pass
    # catches anything that survived (e.g., a title that only revealed
    # its META nature after edition-dedup merged a subtitle).
    meta_dropped = meta_chunks_dropped
    if books:
        before = len(books)
        books = [b for b in books if not _is_meta_title(b.get("title"))]
        meta_dropped += before - len(books)
    if meta_dropped:
        filter_drops["meta_blocklist"] = meta_dropped
    warnings = list(sub.warnings) if sub.warnings else []
    if len(books) < top:
        warnings.append(ToolWarning(
            code="few_unique_books",
            message=f"only {len(books)} unique books survived threshold "
                    f"(min_rerank_score={min_rerank_score}); "
                    f"lower threshold or increase per_retriever",
        ))
    if filter_drops:
        warnings.append(ToolWarning(
            code="dedup",
            message=f"deduped {sum(filter_drops.values())} duplicate "
                    f"book(s) — same content under multiple PG ids "
                    f"or edition variants",
        ))

    # Sprint 19+ — render hint: show rerank_score in the answer table
    # so the user sees confidence per row. Plus disclose translation
    # if it happened so the user can adjust phrasing.
    notes: list[str] = []
    if reranked_by:
        notes.append(
            "В таблице вывода ОБЯЗАТЕЛЬНО покажи колонку `rerank_score` "
            f"(0-1, BGE cross-encoder). Threshold отбора был "
            f"min_rerank_score={min_rerank_score} — все оставшиеся "
            "строки выше этого порога. Это сигнал доверия модели."
        )
    if translated_from:
        notes.append(
            f"Запрос пользователя был на русском («{translated_from}»), "
            f"переведён в semantic search как «{topic}». Сообщи это "
            "пользователю одной строкой («поиск выполнялся по "
            f"английскому запросу «{topic}» — корпус EN»)."
        )

    result = ToolResult.success(
        tool="find_book_by_topic",
        data={
            "topic":               original_topic,
            "topic_searched_as":   topic,
            "translated_from":     translated_from,
            "matches":             books,
            "books_returned":      len(books),
            "total_chunks_seen":   sub.data.get("matches", []) and len(sub.data["matches"]),
            "reranked_by":         reranked_by,
            "min_rerank_score":    min_rerank_score if reranked_by else None,
            # convenience for chaining into book-scoped tools
            "first_id":            books[0]["pg_id"] if books else None,
            "_render_note":        " ".join(notes) if notes else None,
        },
        warnings=warnings,
        coverage=Coverage(books_matched=len(books), books_total=-1),
        query=query,
    )

    # v5 Phase 2 — emit RECOMMENDATION_LIST view. Carries the books +
    # rerank_score + translation provenance so template_executor can
    # render a self-contained table without renderer prompt interpretation.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason

        if not books:
            view = vb.build_recommendation_list(
                items=[],
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=(
                    f"По теме «{original_topic}» не нашлось книг с "
                    f"уверенностью ≥ {min_rerank_score}."
                ),
                empty_message_en=(
                    f"No books matched topic «{topic}» above min_rerank_score={min_rerank_score}."
                ),
                provenance=vb.make_provenance(
                    requested={"topic": original_topic, "top": top,
                               "min_rerank_score": min_rerank_score},
                    sources=["SPGC-2018-07-18", "ChromaDB semantic + FTS5 lexical"],
                ),
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result

        items = []
        for b in books:
            reasons_bits = []
            if b.get("rerank_score") is not None:
                reasons_bits.append(f"rerank: {b['rerank_score']:.3f}")
            if b.get("snippet"):
                snip = b["snippet"][:120].replace("\n", " ")
                reasons_bits.append(f"_«{snip}…»_")
            items.append({
                "pg_id":   b.get("pg_id"),
                "title":   b.get("title"),
                "author":  b.get("author"),
                "reasons": " · ".join(reasons_bits),
                "rerank_score": b.get("rerank_score"),
            })

        view_caveats = []
        if translated_from:
            view_caveats.append(
                f"Запрос переведён с русского «{translated_from}» → «{topic}» "
                f"(семантический поиск работает по EN-корпусу)."
            )
        if filter_drops:
            view_caveats.append(
                f"Свёрнуто {sum(filter_drops.values())} дубликатов "
                f"(разные PG id для той же книги)."
            )

        view = vb.build_recommendation_list(
            items=items,
            headline=f"Книги по теме «{original_topic}»",
            caveats=view_caveats,
            provenance=vb.make_provenance(
                requested={"topic": original_topic, "top": top,
                           "min_rerank_score": min_rerank_score,
                           "translate": translate},
                returned={"books_n": len(items),
                          "reranked_by": reranked_by},
                filtered={"dedup_drops": filter_drops or {}},
                sources=["SPGC-2018-07-18",
                          "ChromaDB semantic + FTS5 lexical",
                          f"BGE rerank: {reranked_by}" if reranked_by else "no rerank"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        log.exception("find_book_by_topic view emission failed")

    return result
