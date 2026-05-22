"""v2 book_readability + book_archaic_words."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1BookReadability, V1BookArchaicWords,
)


@tool(
    name="book_readability",
    category="books",
    description="Flesch + FK Grade + CEFR estimate для одной книги. Используй после find_book.",
    input_schema={
        "type": "object",
        "properties": {
            "pg_id":        {"type": "string"},
            "sample_chars": {"type": "integer", "description": "default 200000"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="cheap",
    cacheable=True,
    # E23 (2026-05-22) — view now reads v1's `cefr_heuristic` key
    # (was reading non-existent `cefr`/`cefr_estimate` → always None
    # in the view). Persona Q5/Q8 «насколько Дракула сложна для
    # изучающих английский» showed CEFR empty.
    wrapper_version="v4-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.book_readability",
             schema=V1BookReadability)
def book_readability(pg_id: str, sample_chars: int = 200_000) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="book_readability", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG1342')",
            query={"pg_id": pg_id},
        )
    from scripts.rag_tools import book_readability as _v1
    raw = _v1(pg_id=pg_id, sample_chars=sample_chars)
    query = {"pg_id": pg_id, "sample_chars": sample_chars}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_readability", err_type="not_found",
                               message=str(raw["error"]), query=query)

    # Sprint 19+ — Stan 2026-05-19: «уровень сложности Pride and
    # Prejudice» вернул `words=34427` которое renderer подал как
    # «общее количество слов». Реально это count внутри первых
    # 200k chars sample (для скорости Flesch/FK). Реальная книга —
    # ~122k слов. Numeric audit не отловил т.к. 34427 действительно
    # в data.
    #
    # Fix: дочитать total_words из counts file (полный per-book
    # frequency dump, лежит в spgc/<dir>/<id>_counts.txt) и явно
    # отрендерить разницу в `_render_note`.
    if isinstance(raw, dict) and not raw.get("total_words_estimate"):
        try:
            from scripts.rag_tools import _counts_path as _v1_counts_path
            cf = _v1_counts_path(pg_id.upper())
            if cf.exists():
                total = 0
                with open(cf, encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) == 2:
                            try:
                                total += int(parts[1])
                            except ValueError:
                                continue
                if total > 0:
                    raw["total_words_estimate"] = total
                    # Mark the existing words field as the sampled one
                    raw["words_sampled_for_metric"] = raw.get("words")
                    # Render hint — keep the existing _render_note if any
                    existing = raw.get("_render_note") or ""
                    note = (
                        f"`words` ({raw.get('words')}) — это count внутри "
                        f"первых {raw.get('sampled_chars', sample_chars)} "
                        f"chars sample (используется для Flesch/FK расчёта, "
                        f"не для длины книги). Реальная длина книги ≈ "
                        f"**{total:,} слов** ({total // 250}-{total // 200} "
                        f"страниц при 200-250 слов/страница). Если "
                        f"пользователь спросил «сколько слов / страниц в "
                        f"книге» — отвечай через `total_words_estimate`, "
                        f"НЕ через `words`."
                    )
                    raw["_render_note"] = (existing + " " + note).strip()
        except Exception:
            pass

    result = ToolResult.success(
        tool="book_readability", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )

    # v5 Phase 2.5 — READABILITY_SUMMARY view emission.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        # V1BookReadability canonical keys: title, flesch_reading_ease,
        # flesch_kincaid_grade, cefr_heuristic, words. No phantom aliases.
        title = raw.get("title") or pg_id
        flesch = raw.get("flesch_reading_ease")
        fk = raw.get("flesch_kincaid_grade")
        cefr = raw.get("cefr_heuristic")
        wc = raw.get("total_words_estimate") or raw.get("words")
        if flesch is not None or fk is not None:
            view = vb.build_readability_summary(
                book_title=title,
                pg_id=pg_id,
                flesch=flesch,
                flesch_kincaid=fk,
                cefr=cefr,
                word_count=wc,
                language="ru",
            )
            vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.books.readability").exception(
            "book_readability view emission failed"
        )
    return result


@tool(
    name="book_archaic_words",
    category="books",
    description="Архаизмы/устаревшие слова в книге — seed list + enrich cache lookups.",
    input_schema={
        "type": "object",
        "properties": {
            "pg_id": {"type": "string"},
            "top":   {"type": "integer", "description": "default 30"},
        },
        "required": ["pg_id"],
    },
    requires=["book"],
    cost="medium",
    cacheable=True,
    # E41 — view now reads v1's `book_count` key (not phantom `count` /
    # `frequency` → empty column). Invalidates entries with «—» values.
    wrapper_version="v4-phase2-contract",
)
@v1_contract(v1_fn="scripts.learning_tools.book_archaic_words",
             schema=V1BookArchaicWords)
def book_archaic_words(pg_id: str, top: int = 30) -> ToolResult:
    if not pg_id or (isinstance(pg_id, str) and not pg_id.strip()):
        return ToolResult.fail(
            tool="book_archaic_words", err_type="invalid_args",
            message="pg_id is required and must be non-empty (e.g. 'PG345')",
            query={"pg_id": pg_id},
        )
    from scripts.learning_tools import book_archaic_words as _v1
    raw = _v1(pg_id=pg_id, top=top)
    query = {"pg_id": pg_id, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_archaic_words", err_type="not_found",
                               message=str(raw["error"]), query=query)
    result = ToolResult.success(
        tool="book_archaic_words", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )

    # v5 Phase 2.5 — TOP_N_TABLE view for archaic words.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return result
        # V1BookArchaicWords doesn't include title at top level — fall
        # back to pg_id for the headline.
        title = pg_id
        # Phase 2 — V1BookArchaicWords canonical key is `top`.
        rows = raw.get("top") or []
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "frequency"],
                empty_reason=EmptyReason.NO_SIGNAL_EXPECTED,
                empty_message_ru=f"В {title} не найдено архаизмов из seed-словаря.",
                empty_message_en=f"No archaic words in {title}.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_EXPECTED)
            return result
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1BookArchaicWords rows: word, book_count, source, note.
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
                "frequency": r.get("book_count") or "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "frequency"],
            headline=f"Архаизмы — {title} ({pg_id})",
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.books.readability").exception(
            "book_archaic_words view emission failed"
        )
    return result
