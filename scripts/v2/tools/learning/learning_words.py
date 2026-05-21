"""v2 learning_words — vocab band-pass + lemmatization + POS + self-learning skip."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.tools.authors._surname_filter import filter_surnames
from scripts.v2.tools.authors._corpus_artifacts import filter_corpus_artifacts


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
                   _capped_from: int | None = None,
                   _translate_followup_disclose: bool = False) -> ToolResult:
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
    # B-R14-7 ROOT CAUSE FIX (2026-05-21): v1 scripts/learning_tools.py
    # returns rows under "results" key (line 564), NOT "words". The v2
    # wrapper had been reading raw.get("words") since Sprint 22+ → always
    # got None → empty rows → looks_broken=True → renderer reported
    # «0 слов уровня B2» on EVERY query. Unit-tests passed because
    # test_router.py:97 mock returns {"words": [...]} (the wrong shape),
    # accidentally matching the wrong read key. Live behavioural golden
    # catches it but is skipUnless(WC_GOLDEN_LIVE) gated.
    # Fix: read "results" (true v1 key) with "words" fallback for
    # backwards-compat with test mocks + any legacy callers.
    rows = None
    if isinstance(raw, dict):
        rows = raw.get("results") or raw.get("words")
    rows = rows or []
    # Drop literary locations (Stan round 2 Q9: Lambton/Shire in P&P
    # learning list). The v1 NER misses some place names; this hardcoded
    # backstop keeps the user-facing study list clean.
    if rows:
        before = len(rows)
        rows = [r for r in rows
                if (r.get("lemma") or r.get("word") or "").lower()
                not in _LITERARY_LOCATION_BLACKLIST]
        if len(rows) < before and isinstance(raw, dict):
            raw["results"] = rows
            raw["words"] = rows
    # Sprint 20 — Stan 2026-05-19: translate-followup routes here, output
    # included character names (quin/kettering/giraud/lorraine — all
    # Christie characters), and the LLM "translator" hallucinated
    # definitions for them. Mirror the v3.1.1 surname filter from
    # affinity_by_author so learning_words doesn't leak character names
    # into a study list. filter_surnames uses curated literary character
    # set + PG-metadata author surnames.
    if rows:
        # The `word_key` argument in filter_surnames defaults to "word",
        # but learning_words rows use either "lemma" or "word" → use
        # whichever is present per row by normalising before the filter.
        for r in rows:
            if "word" not in r and "lemma" in r:
                r["_word_for_filter"] = r["lemma"]
            else:
                r["_word_for_filter"] = r.get("word") or r.get("lemma") or ""
        rows, surname_dropped = filter_surnames(
            rows, word_key="_word_for_filter",
        )
        # Re-apply the marker since filter_surnames returns subset
        for r in rows:
            if "_word_for_filter" not in r:
                r["_word_for_filter"] = (
                    r.get("lemma") or r.get("word") or ""
                )
        rows, artifact_dropped = filter_corpus_artifacts(
            rows, word_key="_word_for_filter",
        )
        for r in rows:
            r.pop("_word_for_filter", None)
        if (surname_dropped or artifact_dropped) and isinstance(raw, dict):
            raw["results"] = rows
            raw["words"] = rows
            prev = raw.get("_render_note", "")
            notes = []
            if surname_dropped:
                notes.append(f"v2 surname filter dropped {surname_dropped} "
                              f"character/author names from learning list")
            if artifact_dropped:
                notes.append(f"v2 corpus-artifact filter dropped "
                              f"{artifact_dropped} markup tokens (Roman "
                              f"numerals, single chars)")
            raw["_render_note"] = (prev + " " + "; ".join(notes)).strip()
    # Sprint 22+ Round 12 post-deploy: stamp count-honesty fields just
    # like affinity_by_author / affinity_by_book do. Stan prod 2026-05-20:
    # renderer hallucinated «возвращено 0 слов» footer over a 30-row
    # table because learning_words didn't surface top_returned and the
    # LLM invented the number. Adding the field gives the renderer a
    # ground truth to anchor on (per RENDER_PROMPT rule 14).
    actual = len(rows) if rows else 0
    if isinstance(raw, dict):
        raw["top_requested"] = top
        raw["top_returned"] = actual
        if actual < top:
            existing = raw.get("_render_note") or ""
            count_note = (
                f"ACTUAL COUNT: learning_words returned {actual} words "
                f"after filtering — NOT the {top} requested. Use "
                f"{actual} in the answer."
            )
            raw["_render_note"] = (
                (existing + " | " if existing else "") + count_note
            )

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
    # Sprint 20 — translate-followup honest disclosure. When the user
    # said «переведи слова» after an `author_vocab` turn, history layer
    # routed here. learning_words returns a DIFFERENT list (CEFR-banded,
    # not affinity-ranked), so the user's expectation «те же 96 слов»
    # would be wrong. Front-load the disclosure in _render_note so the
    # renderer tells the user explicitly.
    if _translate_followup_disclose and isinstance(raw, dict):
        prev = raw.get("_render_note", "")
        disclosure = (
            "ВАЖНО: список слов ПЕРЕФОРМИРОВАН — это learning_words "
            "(CEFR-band intermediate), НЕ тот же affinity-список из "
            "предыдущего хода. Если пользователь хотел перевод "
            "конкретных слов («tuppence», «stitching», «embroidery») "
            "из прошлого ответа — попроси его явно перечислить слова: "
            "«переведи tuppence, stitching, embroidery». "
            "v3 rules-path не передаёт prior tool output между ходами."
        )
        raw["_render_note"] = (prev + " | " + disclosure if prev else disclosure)
        warnings.append(ToolWarning(
            "translate_followup_list_changed",
            "learning_words returns CEFR-band selection, not prior affinity list",
        ))
    result = ToolResult.success(
        tool="learning_words", data=raw,
        coverage=Coverage(
            books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )

    # v5 Phase 2 — emit LEARNING_WORDS view. Closes B-R14-7 (P0) at the
    # data_validity layer: empty result for canonical inputs (level=B2
    # on Pride and Prejudice) gets data_validity=BROKEN, golden tests
    # gate the regression in CI, renderer surfaces a friendly
    # "feature temporarily unavailable" instead of pretending nothing
    # was wrong.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity

        if not isinstance(raw, dict):
            return result

        # Heuristic for "broken": empty result with level in {B1, B2,
        # intermediate} on a non-trivial scope is suspicious (every R14
        # test reproduced this).
        looks_broken = (
            not rows
            and (level or "").lower() in {"intermediate", "b1", "b2"}
            and isinstance(scope, (dict, str))
            and scope not in (None, "", {})
        )

        scope_label = _scope_label(scope)
        view = vb.build_learning_words(
            words=rows or [],
            requested_level=level,
            requested_count=top,
            scope_label=scope_label,
            headline=f"Слова уровня {level} — {scope_label}",
            caveats=(["v5 detect: feature appears broken — golden test "
                       "expected ≥1 word at this level for this scope."]
                      if looks_broken else []),
            provenance=vb.make_provenance(
                requested={"level": level, "top": top, "scope": scope,
                           "pos_filter": pos_filter},
                returned={"count": len(rows or []),
                          "n_books": raw.get("n_books")},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
            is_broken=looks_broken,
            broken_reason=(
                f"learning_words(level={level}) вернул 0 слов для "
                f"{scope_label}. Это похоже на сбой фильтра уровня — "
                f"для канонических входов (P&P / Treasure Island / "
                f"Sherlock Holmes) ожидаемо ≥10 слов."
            ) if looks_broken else None,
        )
        validity = (DataValidity.BROKEN if looks_broken
                    else DataValidity.OK if rows
                    else DataValidity.EMPTY_EXPECTED)
        vb.attach_view(result, view, data_validity=validity)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.learning").warning(
            "learning_words view emission failed: %s", e,
        )

    return result


def _scope_label(scope) -> str:
    """Human-readable label for the scope arg. Used in view headlines."""
    if scope is None or scope == "" or scope == "all_corpus":
        return "весь корпус"
    if isinstance(scope, str):
        return scope
    if isinstance(scope, dict):
        if scope.get("book") or scope.get("pg_id"):
            return f"книга {scope.get('book') or scope.get('pg_id')}"
        if scope.get("author"):
            return f"автор {scope['author']}"
        if scope.get("user_id"):
            return f"загрузка {scope['user_id']}"
    return str(scope)
