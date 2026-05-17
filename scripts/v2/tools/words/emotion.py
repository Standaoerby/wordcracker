"""v2 emotion_collocates — NRC-anchored emotional context."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2.types import Coverage, ToolResult, ToolWarning


@tool(
    name="emotion_collocates",
    category="emotion",
    description=(
        "Слова в окне рядом с NRC-якорями эмоции в scope. "
        "Emotions: anger / anticipation / disgust / fear / joy / sadness / surprise / trust."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope":   {"type": "object"},
            "emotion": {"type": "string"},
            "window":  {"type": "integer", "description": "default 4"},
            "top":     {"type": "integer", "description": "default 25"},
        },
        "required": ["scope", "emotion"],
    },
    requires=["scope"],
    cost="medium",
    cacheable=True,
)
def emotion_collocates(scope, emotion: str, window: int = 4,
                       top: int = 25) -> ToolResult:
    try:
        from scripts.rag_tools import emotion_collocates as _v1
    except ImportError as e:
        return ToolResult.fail(tool="emotion_collocates", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope, emotion=emotion, window=window, top=top)
    query = {"scope": scope, "emotion": emotion, "window": window, "top": top}
    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="emotion_collocates",
            err_type=("invalid_args" if "scope" in err.lower() or "unknown emotion" in err.lower()
                      else "not_found"),
            message=err, query=query,
        )
    rows = (raw.get("top") if isinstance(raw, dict) else None) or []
    return ToolResult.success(
        tool="emotion_collocates", data=raw,
        coverage=Coverage(
            books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=[ToolWarning("no_collocates", "no words near emotion anchors")]
                 if not rows else [],
        query=query,
    )
