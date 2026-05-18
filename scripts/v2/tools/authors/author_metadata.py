"""v2 author_metadata — quick stats for a single author.

Delegates to v1 rag_tools.author_metadata; wraps in ToolResult."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


@tool(
    name="author_metadata",
    category="authors",
    description=(
        "Быстрая мета по автору: годы жизни, язык, количество книг, total downloads, "
        "примеры названий. Используй для «когда родился X», «сколько у X книг»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string",
                             "description": "Regex по колонке author, обычно '^Surname,', e.g. '^Doyle,'"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="cheap",
    cacheable=True,
)
def author_metadata(author_regex: str) -> ToolResult:
    if not author_regex or not author_regex.strip():
        return ToolResult.fail(
            tool="author_metadata", err_type="invalid_args",
            message="author_regex is required",
        )

    try:
        from scripts.rag_tools import author_metadata as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="author_metadata", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(author_regex)
    query = {"author_regex": author_regex}

    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="author_metadata",
            err_type="not_found" if "no books" in raw.get("error", "") else "internal",
            message=str(raw["error"]),
            details={k: v for k, v in raw.items() if k != "error"},
            query=query,
        )

    book_count = raw.get("books_total") or raw.get("book_count") or len(raw.get("sample_titles", []))

    # Q12 from Stan's 2026-05-18 demon round: Poe came back as «1809–1964».
    # 1964 isn't a death year; Gutenberg metadata sometimes confuses
    # `authoryearofdeath` with the publication year of a specific edition.
    # Filter implausible spans (>120 yrs span = wrong) so the LLM doesn't
    # render fiction as life dates.
    if isinstance(raw, dict):
        yob = raw.get("year_of_birth_min")
        yod = raw.get("year_of_death_max")
        if (isinstance(yob, int) and isinstance(yod, int)
                and (yod - yob > 120 or yod < yob)):
            raw["year_of_death_max_unreliable"] = yod
            raw["year_of_death_max"] = None
            raw.setdefault("warnings", []).append(
                f"death year {yod} dropped — implausible span "
                f"(birth={yob}); Gutenberg metadata likely confused "
                f"author death with edition publication year"
            )
        # Hint for the LLM render so it doesn't call this «годы жизни»
        # when only birth is reliable, and doesn't conflate the corpus
        # publication window with biographical dates.
        raw["_render_note"] = (
            "Поля year_of_birth_min / year_of_death_max — биографические "
            "(из Gutendex metadata, могут быть неточными). НЕ называй "
            "это «диапазон книг в корпусе» — это годы жизни. Если "
            "year_of_death_max_unreliable выставлен, скажи что год смерти "
            "не подтверждён и сошлись только на год рождения."
        )

    return ToolResult.success(
        tool="author_metadata", data=raw,
        coverage=Coverage(books_matched=int(book_count or 0), books_total=-1),
        query=query,
    )
