"""v2 corpus_stats_by_author — quick aggregate stats for one author."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1CorpusStatsByAuthor


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
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.corpus_stats_by_author",
             schema=V1CorpusStatsByAuthor)
def corpus_stats_by_author(author_regex: str) -> ToolResult:
    from scripts.rag_tools import corpus_stats_by_author as _v1
    raw = _v1(author_regex=author_regex)
    query = {"author_regex": author_regex}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="corpus_stats_by_author",
            err_type=("not_found" if "no books" in err.lower() else "internal"),
            message=err, query=query,
        )
    # V1CorpusStatsByAuthor canonical key: books_matched.
    n_books = (raw.get("books_matched") if isinstance(raw, dict) else -1) or -1
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
        # Phase 2 — V1CorpusStatsByAuthor canonical keys:
        # books_matched, books_with_counts, total_tokens, unique_words,
        # avg_book_length_words, longest_book, shortest_book, languages.
        rows = []
        for k_human, k_key in [
            ("Книг", "books_matched"),
            ("Книг с тексто-данными", "books_with_counts"),
            ("Токенов", "total_tokens"),
            ("Уникальных слов", "unique_words"),
            ("Среднее слов на книгу", "avg_book_length_words"),
            ("Самая длинная книга", "longest_book"),
            ("Самая короткая книга", "shortest_book"),
        ]:
            v = raw.get(k_key)
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
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.corpus_meta.stats_by_author").exception(
            "corpus_stats_by_author view emission failed"
        )
    return result
