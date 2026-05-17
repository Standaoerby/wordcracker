"""v2 word_etymology + find_words_by_etymology — Wiktionary-backed."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult, ToolWarning


@tool(
    name="word_etymology",
    category="words",
    description="Этимологическая цепочка слова через Wiktionary (cached). For «откуда слово X».",
    input_schema={
        "type": "object",
        "properties": {"word": {"type": "string"}},
        "required": ["word"],
    },
    requires=["word"],
    cost="cheap",
    cacheable=True,
)
def word_etymology(word: str) -> ToolResult:
    try:
        from scripts.rag_tools import word_etymology as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_etymology", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(word=word)
    query = {"word": word}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_etymology", err_type="not_found",
                               message=str(raw["error"]), query=query)
    chain = (raw.get("family_chain") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="word_etymology", data=raw,
        coverage=Coverage(),
        warnings=[ToolWarning("no_etymology", "Wiktionary has no etymology section")]
                 if not chain else [],
        query=query,
    )


@tool(
    name="find_words_by_etymology",
    category="words",
    description=(
        "Найти слова автора/книги по этимологическому происхождению. "
        "family: germanic / norse / romance / latin / greek / celtic / slavic / arabic / pie. "
        "Heavy: Wiktionary HTTP fan-out, capped via min_corpus_count и top."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope":             {"type": "object"},
            "family":            {"type": "string"},
            "top":               {"type": "integer", "description": "default 30, capped at 20 in planner"},
            "min_corpus_count":  {"type": "integer", "description": "default 500"},
        },
        "required": ["scope", "family"],
    },
    requires=["scope"],
    cost="heavy",
    cacheable=True,
    timeout_s=90,
)
def find_words_by_etymology(scope, family: str, top: int = 30,
                            min_corpus_count: int = 500) -> ToolResult:
    try:
        from scripts.rag_tools import find_words_by_etymology as _v1
    except ImportError as e:
        return ToolResult.fail(tool="find_words_by_etymology", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope, family=family, top=top, min_corpus_count=min_corpus_count)
    query = {"scope": scope, "family": family, "top": top,
             "min_corpus_count": min_corpus_count}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="find_words_by_etymology",
            err_type=("invalid_args" if "scope" in err.lower() or "family" in err.lower()
                      else "internal"),
            message=err, query=query,
        )
    rows = (raw.get("matches") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="find_words_by_etymology", data=raw,
        coverage=Coverage(books_matched=raw.get("books_total", -1)
                                       if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=[ToolWarning("no_matches",
                              f"no words of family={family} above min_corpus_count")]
                 if not rows else [],
        query=query,
    )
