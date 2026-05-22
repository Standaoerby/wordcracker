"""v2 word_pos_distribution — polysemy probe."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1WordPosDistribution


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
    # E18 (2026-05-22) — E15 view now handles v1's LIST shape for
    # pos_distribution. Old cached entries built the view as dict-only
    # and silently fell into the empty path.
    wrapper_version="v3-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.word_pos_distribution",
             schema=V1WordPosDistribution)
def word_pos_distribution(scope, word: str) -> ToolResult:
    from scripts.rag_tools import word_pos_distribution as _v1
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
    # E15 P0 FIX (2026-05-22): v1 word_pos_distribution (rag_tools.py:1649)
    # returns `pos_distribution` as a LIST of dicts
    # `[{"pos": str, "count": int, "share": float, "samples": [...]}]`,
    # NOT a dict. Old wrapper read it as dict → fell into the empty path
    # every time. Same class as B-R14-7 / E9 / E14b / E15. Handle BOTH
    # shapes (real v1 = list, test mocks may use dict).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return result
        # Phase 2 — V1WordPosDistribution declares `pos_distribution` as
        # the canonical list. Phantom `distribution`/`counts` removed.
        dist = raw.get("pos_distribution") or []
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {scope.get('book') or scope.get('pg_id')}"
                     if scope.get("book") or scope.get("pg_id")
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        view_rows = []
        if isinstance(dist, list) and dist:
            # v1 actual shape: already sorted by count (Counter.most_common).
            total = sum(int(r.get("count") or 0) for r in dist
                        if isinstance(r, dict))
            for i, r in enumerate(dist, start=1):
                if not isinstance(r, dict):
                    continue
                cnt = r.get("count") or 0
                # v1 already provides `share` (rounded float 0..1)
                share = r.get("share")
                if share is None:
                    share = (float(cnt) / total) if total else 0
                view_rows.append({
                    "rank": i,
                    "pos": r.get("pos") or "—",
                    "share": f"{float(share) * 100:.1f}%",
                    "count": cnt,
                })
        elif isinstance(dist, dict) and dist:
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
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.words.pos").exception(
            "word_pos_distribution view emission failed"
        )
    return result
