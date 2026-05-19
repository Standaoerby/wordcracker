"""v2 book_readability + book_archaic_words."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


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
)
def book_readability(pg_id: str, sample_chars: int = 200_000) -> ToolResult:
    try:
        from scripts.rag_tools import book_readability as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_readability", err_type="internal",
                               message=f"v1 unavailable: {e}")
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

    return ToolResult.success(
        tool="book_readability", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )


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
)
def book_archaic_words(pg_id: str, top: int = 30) -> ToolResult:
    try:
        from scripts.learning_tools import book_archaic_words as _v1
    except ImportError as e:
        return ToolResult.fail(tool="book_archaic_words", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(pg_id=pg_id, top=top)
    query = {"pg_id": pg_id, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="book_archaic_words", err_type="not_found",
                               message=str(raw["error"]), query=query)
    return ToolResult.success(
        tool="book_archaic_words", data=raw,
        coverage=Coverage(books_matched=1, books_total=1),
        query=query,
    )
