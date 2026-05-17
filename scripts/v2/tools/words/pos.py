"""v2 word_pos_distribution — polysemy probe."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


@tool(
    name="word_pos_distribution",
    category="words",
    description="Распределение POS-тегов конкретного слова в scope (NOUN / VERB / ADJ).",
    input_schema={
        "type": "object",
        "properties": {
            "scope": {"type": "object"},
            "word":  {"type": "string"},
        },
        "required": ["scope", "word"],
    },
    requires=["word", "scope"],
    cost="cheap",
    cacheable=True,
)
def word_pos_distribution(scope, word: str) -> ToolResult:
    try:
        from scripts.rag_tools import word_pos_distribution as _v1
    except ImportError as e:
        return ToolResult.fail(tool="word_pos_distribution", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope, word=word)
    query = {"scope": scope, "word": word}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="word_pos_distribution",
            err_type=("invalid_args" if "scope" in err.lower() else "not_found"),
            message=err, query=query,
        )
    return ToolResult.success(
        tool="word_pos_distribution", data=raw,
        coverage=Coverage(books_matched=1 if isinstance(scope, dict) and scope.get("book") else -1,
                          books_total=-1),
        query=query,
    )
