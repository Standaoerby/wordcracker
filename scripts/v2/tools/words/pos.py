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
    result = ToolResult.success(
        tool="word_pos_distribution", data=raw,
        coverage=Coverage(books_matched=1 if isinstance(scope, dict) and scope.get("book") else -1,
                          books_total=-1),
        query=query,
    )

    # v5 Phase 2.5 — TOP_N_TABLE view of POS distribution.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return result
        dist = (raw.get("distribution") or raw.get("pos_distribution")
                or raw.get("counts") or {})
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {scope.get('book') or scope.get('pg_id')}"
                     if scope.get("book") or scope.get("pg_id")
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        view_rows = []
        if isinstance(dist, dict) and dist:
            total = sum(v for v in dist.values()
                        if isinstance(v, (int, float)))
            sorted_items = sorted(dist.items(), key=lambda kv: -float(kv[1] or 0))
            for i, (pos, cnt) in enumerate(sorted_items, start=1):
                share = (float(cnt) / total) if total else 0
                view_rows.append({
                    "rank": i,
                    "pos": pos,
                    "share": f"{share * 100:.1f}%",
                    "count": cnt,
                })
        if not view_rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "pos", "share", "count"],
                empty_reason=EmptyReason.NO_RECORDS_IN_CORPUS,
                empty_message_ru=f"Слово «{word}» не встретилось в {scope_str}.",
                empty_message_en=f"«{word}» not found in {scope_str}.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "pos", "share", "count"],
            headline=f"POS-распределение «{word}» — {scope_str}",
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.pos").warning(
            "word_pos_distribution view emission failed: %s", e,
        )
    return result
