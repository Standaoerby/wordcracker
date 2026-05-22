"""v2 emotion_collocates — NRC-anchored emotional context."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


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
    # E15 P0 FIX (2026-05-22): v1 returns key «top_collocates» (line 2052
    # of rag_tools.py), NOT «top». Old wrapper read wrong key → rows
    # always empty → view always EMPTY_EXPECTED. Same class as B-R14-7
    # (learning_words «results» vs «words»), E9 (word_contexts «context»
    # vs «snippet»), E14b (affinity_by_book «top» vs «top_words»).
    # R-22 P5 («слова страха у По и Лавкрафта одновременно») got
    # nothing meaningful — both authors silently empty. Fix: read v1's
    # actual key; legacy «top» fallback for test mocks.
    rows = None
    if isinstance(raw, dict):
        rows = raw.get("top_collocates") or raw.get("top")
    rows = rows or []
    result = ToolResult.success(
        tool="emotion_collocates", data=raw,
        coverage=Coverage(
            books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=[ToolWarning("no_collocates", "no words near emotion anchors")]
                 if not rows else [],
        query=query,
    )

    # v5 Phase 2.5 — COLLOCATES view (treating emotion anchor as
    # the "word" anchor for the collocate view).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {scope.get('book') or scope.get('pg_id')}"
                     if scope.get("book") or scope.get("pg_id")
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        collocates = []
        for c in (rows or [])[:top]:
            if not isinstance(c, dict):
                continue
            collocates.append({
                "token": c.get("word") or c.get("token") or "—",
                "npmi": c.get("npmi") or c.get("score"),
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
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.emotion").warning(
            "emotion_collocates view emission failed: %s", e,
        )
    return result
