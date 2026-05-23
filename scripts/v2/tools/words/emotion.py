"""v2 emotion_collocates — NRC-anchored emotional context."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1EmotionCollocates


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
    # E15/E17 — wrapper now reads v1's `top_collocates` key (was reading
    # `top` and silently returning empty). Bump invalidates entries from
    # all pre-E15 runs that cached the empty result.
    wrapper_version="v3-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.emotion_collocates",
             schema=V1EmotionCollocates)
def emotion_collocates(scope, emotion: str, window: int = 4,
                       top: int = 25) -> ToolResult:
    from scripts.rag_tools import emotion_collocates as _v1
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
    # Phase 2 — V1EmotionCollocates declares canonical `top_collocates`
    # (rag_tools.py:2052). Phantom `top` fallback removed per R3.
    rows = (raw.get("top_collocates")
             if isinstance(raw, dict) else None) or []
    result = ToolResult.success(
        tool="emotion_collocates", data=raw,
        coverage=Coverage(books_matched=-1, books_total=-1),
        warnings=[ToolWarning("no_collocates", "no words near emotion anchors")]
                 if not rows else [],
        query=query,
    )

    # v5 Phase 2.5 — COLLOCATES view (treating emotion anchor as
    # the "word" anchor for the collocate view).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        from scripts.v2.tools._normalize import scope_book_id
        _book = scope_book_id(scope) if isinstance(scope, dict) else None
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {_book}" if _book
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        collocates = []
        for c in (rows or [])[:top]:
            if not isinstance(c, dict):
                continue
            # Per V1EmotionCollocates row_keys — only `word`, `count`.
            # `npmi` only meaningful if a metric was applied; v1 returns
            # raw counts so this stays None at the view layer.
            collocates.append({
                "token": c.get("word") or "—",
                "npmi": None,
                "count": c.get("count"),
            })
        view = vb.build_collocates(
            word=f"эмоция:{emotion}",
            collocates=collocates,
            window=window,
            scope_label=scope_str,
            headline=f"Слова рядом с маркерами эмоции «{emotion}» — {scope_str}",
            language="ru",
        )
        validity = DataValidity.OK if collocates else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.emotion").exception(
            "emotion_collocates view emission failed"
        )
    return result
