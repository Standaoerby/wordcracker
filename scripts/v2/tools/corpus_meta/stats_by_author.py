"""v2 corpus_stats_by_author — quick aggregate stats for one author."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


@tool(
    name="corpus_stats_by_author",
    category="corpus_meta",
    description=(
        "Агрегированная статистика по автору: число книг, токенов, словарь, "
        "длиннейшая/короткая книга. «Дай статистику по X», «сколько у X книг»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string",
                             "description": "Regex по author column, '^Surname,'"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="cheap",
    cacheable=True,
)
def corpus_stats_by_author(author_regex: str) -> ToolResult:
    try:
        from scripts.rag_tools import corpus_stats_by_author as _v1
    except ImportError as e:
        return ToolResult.fail(tool="corpus_stats_by_author", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(author_regex=author_regex)
    query = {"author_regex": author_regex}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="corpus_stats_by_author",
            err_type=("not_found" if "no books" in err.lower() else "internal"),
            message=err, query=query,
        )
    n_books = (raw.get("books_total") if isinstance(raw, dict) else -1) or -1
    result = ToolResult.success(
        tool="corpus_stats_by_author", data=raw,
        coverage=Coverage(books_matched=n_books, books_total=-1),
        query=query,
    )

    # v5 Phase 2.5 — TOP_N_TABLE view (key-value rows for stats).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        author_name = author_regex.lstrip("^").rstrip(",").strip()
        rows = []
        for k_human, k_keys in [
            ("Книг", ("books_total", "book_count")),
            ("Токенов", ("tokens_total", "n_tokens", "tokens")),
            ("Словарь (уникальных лемм)",
             ("vocab_size", "unique_tokens", "n_unique")),
            ("TTR (lex diversity)", ("ttr", "lex_div")),
            ("Самая длинная книга", ("longest_book", "longest")),
            ("Самая короткая книга", ("shortest_book", "shortest")),
        ]:
            v = None
            for key in k_keys:
                if key in raw and raw[key] is not None:
                    v = raw[key]
                    break
            if v is not None:
                rows.append({"metric": k_human, "value": v})
        if not rows:
            return result    # fall through without view
        view = vb.build_top_n_table(
            rows=rows,
            columns=["metric", "value"],
            headline=f"Статистика — {author_name}",
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.corpus_meta.stats_by_author").warning(
            "corpus_stats_by_author view emission failed: %s", e,
        )
    return result
