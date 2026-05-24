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
from scripts.v2.tools.authors._toponym_filter import filter_toponyms
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1LearningWords


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
            # Phase 2 / W-19 (2026-05-25) — bad_answers id 6d864f676266
            # showed the LLM emitting `scope: "corpus"` against the
            # pre-fix `{"type": "object"}` declaration. Two structural
            # issues fixed in this oneOf:
            #   1. v1 (learning_tools.py:439) accepts {'book':PGid} OR
            #      {'author':regex} OR the literal string 'all_corpus'.
            #      The old schema declared only `type: object`, so it
            #      both LIED about the type (string is valid too) and
            #      hid the all_corpus option from the LLM's view of the
            #      contract.
            #   2. Description didn't list 'all_corpus' as a valid value,
            #      so when the LLM wanted "whole corpus" it improvised
            #      `"corpus"` and the wrapper rejected with «bad scope».
            # The oneOf below is the actual contract v1 enforces; every
            # branch is independently dispatchable.
            "scope": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"book": {"type": "string",
                                                 "description": "PGid e.g. 'PG1342'"}},
                        "required": ["book"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"author": {"type": "string",
                                                   "description": "author regex e.g. '^Doyle,'"}},
                        "required": ["author"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "string",
                        "enum": ["all_corpus"],
                        "description": "literal 'all_corpus' for whole-corpus scope",
                    },
                ],
                "description":
                    "ONE of: {'book': 'PG1342'} | {'author': '^Doyle,'} | 'all_corpus'. "
                    "Pick 'all_corpus' for level-banded vocabulary across the entire "
                    "library when no specific book or author is named.",
            },
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
    # R-23 Tier 0 — wrapper had silently read raw["words"] until
    # 2026-05-21 (B-R14-7 root cause). The fix landed but cached results
    # from the broken period still poisoned downstream renders.
    # Bumping version busts the stale cache.
    # Phase 3 W-4 (2026-05-22) — wired toponym filter (GPE/LOC) +
    # extended character-surname blocklist.
    # W-3 (2026-05-24) — wrapper now stamps `_render_columns` +
    # `_render_note` explaining that this tool emits ONLY word /
    # lemma / pos / scope_count / corpus_count / affinity / level
    # (no translation / example / IPA). Stops the LLM from inventing
    # «Перевод» / «Пример» columns that render «—» on every row.
    # Bumping invalidates cached rows that lack the new hints.
    wrapper_version="v5-w3-columns-hint",
)
@v1_contract(v1_fn="scripts.learning_tools.learning_words",
             schema=V1LearningWords)
def learning_words(scope, level: str = "intermediate", top: int = 30,
                   lemmatize: bool = True,
                   pos_filter: list[str] | None = None,
                   _capped_from: int | None = None,
                   _translate_followup_disclose: bool = False) -> ToolResult:
    from scripts.learning_tools import learning_words as _v1
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
    # Phase 2 — V1LearningWords contract declares the canonical key
    # `results` (learning_tools.py line ~564). Pre-contract wrappers also
    # read phantom `words`; that fallback is gone now. Mocks via
    # mock_from_schema cannot drift from the real v1 shape (R3/R4).
    rows = raw.get("results") if isinstance(raw, dict) else None
    rows = rows or []
    # Drop literary locations (Stan round 2 Q9: Lambton/Shire in P&P
    # learning list). The v1 NER misses some place names; this hardcoded
    # backstop keeps the user-facing study list clean.
    if rows:
        before = len(rows)
        # V1LearningWords rows always carry both `word` and `lemma`
        # (learning_tools.py:530/536 — `lemma` defaults to `word` when
        # lemmatize=False). Filtering against a location blacklist is
        # case-insensitive on the surface token, so read `word` directly.
        rows = [r for r in rows
                if (r.get("word") or "").lower()
                not in _LITERARY_LOCATION_BLACKLIST]
        if len(rows) < before and isinstance(raw, dict):
            raw["results"] = rows
    # Sprint 20 — Stan 2026-05-19: translate-followup routes here, output
    # included character names (quin/kettering/giraud/lorraine — all
    # Christie characters), and the LLM "translator" hallucinated
    # definitions for them. Mirror the v3.1.1 surname filter from
    # affinity_by_author so learning_words doesn't leak character names
    # into a study list. filter_surnames uses curated literary character
    # set + PG-metadata author surnames.
    if rows:
        # V1LearningWords always sets `word` per row (learning_tools.py:530/536)
        # so the filter sees a deterministic surface token. The pre-Phase-2
        # `word or lemma` fallback chain is gone — one canonical key.
        for r in rows:
            r["_word_for_filter"] = r.get("word") or ""
        rows, surname_dropped = filter_surnames(
            rows, word_key="_word_for_filter",
        )
        # Re-apply the marker since filter_surnames returns subset
        for r in rows:
            if "_word_for_filter" not in r:
                r["_word_for_filter"] = r.get("word") or ""
        rows, artifact_dropped = filter_corpus_artifacts(
            rows, word_key="_word_for_filter",
        )
        # Phase 3 W-4 — drop GPE/LOC toponyms after surname pass. Same
        # leak vector at the learning-words level: Lambton/Shire were
        # caught by hand-coded `_LITERARY_LOCATION_BLACKLIST`, but Boer-
        # war places and London districts weren't. Curated toponym
        # blocklist handles them uniformly.
        for r in rows:
            if "_word_for_filter" not in r:
                r["_word_for_filter"] = r.get("word") or ""
        rows, toponym_dropped = filter_toponyms(
            rows, word_key="_word_for_filter",
        )
        for r in rows:
            r.pop("_word_for_filter", None)
        if (surname_dropped or artifact_dropped or toponym_dropped) and isinstance(raw, dict):
            raw["results"] = rows
            prev = raw.get("_render_note", "")
            notes = []
            if surname_dropped:
                notes.append(f"v2 surname filter dropped {surname_dropped} "
                              f"character/author names from learning list")
            if artifact_dropped:
                notes.append(f"v2 corpus-artifact filter dropped "
                              f"{artifact_dropped} markup tokens (Roman "
                              f"numerals, single chars)")
            if toponym_dropped:
                notes.append(f"v2 toponym filter dropped {toponym_dropped} "
                              f"GPE/LOC place names (cities, regions)")
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

        # W-3 (2026-05-24) — column contract for the LLM render path.
        # learning_words rows carry only the keys listed below
        # (V1LearningWords.__row_keys__ — word, lemma, pos, scope_count,
        # corpus_count, affinity, score). No translation / example /
        # IPA / definition. When the user query mentions «перевод» /
        # «пример», the LLM used to add «Перевод» / «Пример» columns
        # and render «—» on every row because nothing in the row
        # carried those values. Surface the exact columns the LLM is
        # allowed to render so the «column либо заполнена, либо не
        # показывается» rule (RENDER_PROMPT rule 19) holds even when
        # the user explicitly asked for translations.
        raw["_render_columns"] = [
            "rank", "word", "lemma", "pos",
            "scope_count", "corpus_count", "affinity", "level",
        ]
        cols_note = (
            "COLUMNS: this tool emits ONLY word/lemma/pos/scope_count/"
            "corpus_count/affinity/level per row. There is NO translation, "
            "NO example, NO IPA, NO definition — do NOT render a «Перевод» "
            "or «Пример» column (it would be «—» on every row). If the "
            "user asked for translation, append a one-line suggestion: "
            "«хочешь перевод этих слов? отправь: переведи <первые 5-10 "
            "слов>» — это вызовет enrich_word pipeline."
        )
        existing = raw.get("_render_note") or ""
        raw["_render_note"] = (
            (existing + " | " if existing else "") + cols_note
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
        # E36 (2026-05-22) — removed hardcoded dev-test words («tuppence/
        # stitching/embroidery») and internal-architecture phrasing
        # («v3 rules-path не передаёт…»). Both leaked to end users via
        # the renderer in clarify outputs. Disclosure now uses generic
        # placeholders + plain user-facing wording.
        prev = raw.get("_render_note", "")
        disclosure = (
            "ВАЖНО: список слов ПЕРЕФОРМИРОВАН — это learning_words "
            "(подборка по уровню CEFR), НЕ тот же affinity-список "
            "из предыдущего ответа. Если пользователь хотел перевод "
            "конкретных слов из прошлого ответа — попроси его "
            "перечислить эти слова явно (например: «переведи X, Y, Z»)."
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
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        import logging
        logging.getLogger("wordcracker.v2.tools.learning").exception(
            "learning_words view emission failed"
        )

    return result


def _scope_label(scope) -> str:
    """Human-readable label for the scope arg. Used in view headlines."""
    from scripts.v2.tools._normalize import scope_book_id
    if scope is None or scope == "" or scope == "all_corpus":
        return "весь корпус"
    if isinstance(scope, str):
        return scope
    if isinstance(scope, dict):
        book = scope_book_id(scope)
        if book:
            return f"книга {book}"
        if scope.get("author"):
            return f"автор {scope['author']}"
        if scope.get("user_id"):
            return f"загрузка {scope['user_id']}"
    return str(scope)
