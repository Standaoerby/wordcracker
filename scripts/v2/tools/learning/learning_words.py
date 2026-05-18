"""v2 learning_words — vocab band-pass + lemmatization + POS + self-learning skip."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning


# Stan round 2 Q9: learning_words на P&P вернул `Lambton`, `Shire` — это
# locations. spaCy NER in v1 не всегда отлавливает места, и они утекают
# в учебный список. Hard blacklist для известных литературных топонимов
# часто-цитируемых книг. Extend pragmatically.
_LITERARY_LOCATION_BLACKLIST = frozenset({
    # Pride and Prejudice + Sense and Sensibility
    "lambton", "shire", "pemberley", "longbourn", "netherfield",
    "rosings", "hunsford", "meryton", "derbyshire", "kent",
    "hertfordshire", "norland", "barton", "delaford", "cleveland",
    # Wuthering Heights
    "gimmerton", "yorkshire", "thrushcross",
    # Frankenstein / Dracula
    "ingolstadt", "geneva", "transylvania", "carfax", "whitby",
    # Treasure Island / Moby Dick / Hound of the Baskervilles
    "hispaniola", "nantucket", "dartmoor",
    # Mythology / classical
    "hades", "olympus", "valhalla", "asgard",
})


@tool(
    name="learning_words",
    category="learning",
    description=(
        "Слова для изучения — band-pass по corpus_count + lemmatize + POS filter + "
        "self-learning skip proper nouns. Уровни: basic / intermediate (B1-B2 default) "
        "/ advanced (C1+) / rare."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope":     {"type": "object", "description": "{'book': PGid} | {'author': regex}"},
            "level":     {"type": "string",
                          "enum": ["basic", "intermediate", "advanced", "rare"],
                          "description": "default 'intermediate'"},
            "top":       {"type": "integer", "description": "default 30 (capped by planner)"},
            "lemmatize": {"type": "boolean", "description": "default true"},
            "pos_filter":{"type": "array", "items": {"type": "string"}},
        },
        "required": ["scope"],
    },
    requires=["scope"],
    cost="medium",
    cacheable=True,
)
def learning_words(scope, level: str = "intermediate", top: int = 30,
                   lemmatize: bool = True,
                   pos_filter: list[str] | None = None,
                   _capped_from: int | None = None) -> ToolResult:
    try:
        from scripts.learning_tools import learning_words as _v1
    except ImportError as e:
        return ToolResult.fail(tool="learning_words", err_type="internal",
                               message=f"v1 unavailable: {e}")
    raw = _v1(scope=scope, level=level, top=top,
              lemmatize=lemmatize, pos_filter=pos_filter)
    query = {"scope": scope, "level": level, "top": top,
             "lemmatize": lemmatize, "pos_filter": pos_filter}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="learning_words",
            err_type="invalid_args" if "scope" in str(raw["error"]).lower()
                                     else "not_found",
            message=str(raw["error"]), query=query,
        )
    rows = (raw.get("words") if isinstance(raw, dict) else None) or []
    # Drop literary locations (Stan round 2 Q9: Lambton/Shire in P&P
    # learning list). The v1 NER misses some place names; this hardcoded
    # backstop keeps the user-facing study list clean.
    if rows:
        before = len(rows)
        rows = [r for r in rows
                if (r.get("lemma") or r.get("word") or "").lower()
                not in _LITERARY_LOCATION_BLACKLIST]
        if len(rows) < before and isinstance(raw, dict):
            raw["words"] = rows
    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            "empty_top",
            "no words at this level — try different scope or level"))
    # Q21 visibility fix: if the planner capped the user's requested top, tell
    # the LLM so it can mention «по запросу 300 слов вернул 30 за один проход;
    # хочешь следующие 30?» instead of silently returning fewer than asked.
    if _capped_from and _capped_from > top:
        warnings.append(ToolWarning(
            "top_capped",
            f"user asked for top={_capped_from}; per-call cap is {top} "
            f"(LLM enrichment cost). Tell the user and offer a follow-up.",
        ))
    return ToolResult.success(
        tool="learning_words", data=raw,
        coverage=Coverage(
            books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
