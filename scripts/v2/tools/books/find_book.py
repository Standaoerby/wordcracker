"""v2 find_book — title/author lookup with PG/U id resolution.

Delegates to v1 rag_tools.find_book for the heavy work (metadata_df merge,
Cyrillic auto-translate, regex/substring fallback). The v2 layer adds:
  * `ToolResult` envelope with `Coverage` (total_matches vs returned).
  * `not_found` error type when nothing matches → planner uses this to ask
    the user for clarification instead of letting the LLM guess.
  * stable `data` shape so the router can thread `matches[0].id` into the
    next plan step automatically.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make v1 modules importable from the repo root even when v2 runs from anywhere.
_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1FindBook

log = logging.getLogger("wordcracker.v2.tools.find_book")


@tool(
    name="find_book",
    category="books",
    description=(
        "Поиск книги по title (substring/regex, case-insensitive) с опциональным author hint. "
        "Возвращает PG/U id, title, author, downloads, год рождения автора. "
        "ОБЯЗАТЕЛЬНО вызови перед любым tool, который требует pg_id — корпус 75k книг, "
        "id не коррелирует с популярностью. Никогда не угадывай PG id."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title":  {"type": "string", "description": "substring или regex по title"},
            "author": {"type": "string", "description": "опциональный regex по author, e.g. '^Doyle,'"},
            "top":    {"type": "integer", "description": "сколько матчей вернуть (default 5)"},
            "lang":   {"type": "string", "description": "ISO-639-1, default 'en'"},
        },
        "required": ["title"],
    },
    requires=["book"],
    cost="cheap",
    cacheable=True,
    # W-17 (Phase 5 P2, 2026-05-23) — wire dedup_book_editions onto the
    # find_book path. Without it «найди Frankenstein» returned 5 PG-ids
    # (PG84, PG41445, …) and «найди Hard Times» 2; the W-17 acceptance
    # required schлоп. Bump invalidates cache so existing «5 row»
    # entries get recomputed.
    wrapper_version="v3-w17-edition-dedup",
)
@v1_contract(v1_fn="scripts.rag_tools.find_book", schema=V1FindBook)
def find_book(title: str, author: str = "", top: int = 5,
              lang: str = "en") -> ToolResult:
    if not title or not title.strip():
        return ToolResult.fail(
            tool="find_book", err_type="invalid_args",
            message="title is required",
            query={"title": title},
        )

    from scripts.rag_tools import find_book as _v1
    raw = _v1(title=title, author=author or "", top=top, lang=lang)
    query = {"title": title, "author": author or None, "top": top, "lang": lang}

    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="find_book", err_type="internal",
            message=str(raw["error"]),
            details={k: v for k, v in raw.items() if k != "error"},
            query=query,
        )

    matches = raw.get("matches", []) if isinstance(raw, dict) else []
    total = int(raw.get("total_matches", len(matches))) if isinstance(raw, dict) else len(matches)

    # W-17 (Phase 5 P2, 2026-05-23) — collapse edition duplicates.
    # «Frankenstein» / «Frankenstein; Or, The Modern Prometheus» /
    # «Frankenstein (1818 edition)» each had its own PG id in raw v1
    # output; user got 5 rows for the same novel. dedup_book_editions
    # groups by (author, normalized_title) and keeps the highest-
    # downloads row per group. Done AFTER v1 returned so `total_matches`
    # still reflects the underlying corpus count.
    dedup_dropped = 0
    if matches:
        try:
            from scripts.v2.tools._result_filters import (
                apply_filters, dedup_book_editions,
            )
            matches, drops = apply_filters([dedup_book_editions], matches)
            dedup_dropped = sum(drops.values()) if drops else 0
        except Exception:
            log.exception("dedup_book_editions on find_book failed; "
                          "shipping raw matches")

    warnings: list[ToolWarning] = []
    if total == 0:
        # Planner will catch this and ask for clarification rather than letting
        # the LLM hallucinate a PG id.
        return ToolResult(
            ok=False, tool="find_book", query=query,
            data={"matches": [], "title_query": raw.get("title_query", title),
                  "author_filter": author or None, "total_matches": 0},
            warnings=[ToolWarning(
                code="not_found",
                message=f"no book matched title {raw.get('title_query', title)!r}"
                        + (f" by author {author!r}" if author else ""),
            )],
            coverage=Coverage(books_matched=0, books_total=-1),
            error=None,
        )
    if total > top:
        warnings.append(ToolWarning(
            code="more_matches",
            message=f"{total} books matched, showing top {top}. Narrow with author=…",
            details={"total": total, "returned": len(matches)},
        ))
    if dedup_dropped:
        warnings.append(ToolWarning(
            code="edition_dedup",
            message=f"свёрнуто {dedup_dropped} дублирующее(их) изданий — "
                    f"одна и та же книга под разными PG id (W-17).",
        ))

    result = ToolResult.success(
        tool="find_book",
        data={
            "matches": matches,
            "title_query": raw.get("title_query", title),
            "author_filter": author or None,
            "total_matches": total,
            # convenience: first id ready for chaining into book-scoped tools
            "first_id": matches[0].get("id") if matches else None,
        },
        warnings=warnings,
        coverage=Coverage(books_matched=total, books_total=-1),
        query=query,
    )

    # v5 Phase 2.5 — BOOK_LOOKUP view emission.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        best = matches[0] if matches else {}
        candidates = []
        for m in matches[:5]:
            if isinstance(m, dict):
                candidates.append({
                    "pg_id": str(m.get("id") or ""),
                    "title": m.get("title") or "",
                    "author": m.get("author") or "",
                    "downloads": m.get("downloads"),
                })
        caveats = []
        if total > top:
            caveats.append(f"Найдено {total} книг, показано {len(candidates)}. "
                            f"Уточни через author=…")
        view = vb.build_book_lookup(
            book={
                "pg_id": str(best.get("id") or ""),
                "title": best.get("title") or title,
                "author": best.get("author") or "",
                "pub_year": best.get("pub_year"),
                "downloads": best.get("downloads"),
            },
            candidates=candidates,
            caveats=caveats,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        log.exception("find_book view emission failed")
    return result
