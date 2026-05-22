"""v2 word_etymology + find_words_by_etymology — Wiktionary-backed."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import (
    V1WordEtymology, V1FindWordsByEtymology,
)


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
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.word_etymology",
             schema=V1WordEtymology)
def word_etymology(word: str) -> ToolResult:
    from scripts.rag_tools import word_etymology as _v1
    raw = _v1(word=word)
    query = {"word": word}
    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(tool="word_etymology", err_type="not_found",
                               message=str(raw["error"]), query=query)
    chain = (raw.get("family_chain") if isinstance(raw, dict) else None) or []
    result = ToolResult.success(
        tool="word_etymology", data=raw,
        coverage=Coverage(),
        warnings=[ToolWarning("no_etymology", "Wiktionary has no etymology section")]
                 if not chain else [],
        query=query,
    )

    # v5 Phase 2.5 — ETYMOLOGY_BUNDLE view. Even standalone etymology
    # call emits a bundle view (other slots None → slots_available
    # accurate, renderer says "etymology only" not fabricates ipa/pos).
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity
        if not isinstance(raw, dict):
            return result
        # Phase 2 — V1WordEtymology canonical key is `primary_family`.
        view = vb.build_etymology_bundle(
            word=word,
            etymology={
                "primary_family": raw.get("primary_family"),
                "family_chain": chain,
            } if chain or raw.get("primary_family") else None,
            language="ru",
        )
        validity = DataValidity.OK if chain else DataValidity.EMPTY_EXPECTED
        vb.attach_view(result, view, data_validity=validity)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.etymology").warning(
            "word_etymology view emission failed: %s", e,
        )
    return result


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
    wrapper_version="v2-phase2-contract",
)
@v1_contract(v1_fn="scripts.rag_tools.find_words_by_etymology",
             schema=V1FindWordsByEtymology)
def find_words_by_etymology(scope, family: str, top: int = 30,
                            min_corpus_count: int = 500) -> ToolResult:
    from scripts.rag_tools import find_words_by_etymology as _v1
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
    # Sprint 18+ Round 9 bug: v1 returns `matched` (not `matches`).
    # Old wrapper read the wrong key → rows always empty → no_matches
    # warning fired even when v1 had found 15 words. Stan caught this
    # in «германские слова Толкина» where 15 ME words showed alongside
    # a false «no_matches» warning.
    rows = (raw.get("matched") if isinstance(raw, dict) else None) or []

    # Render hint: family expansion includes Old/Middle English + Proto-
    # Germanic, so «germanic words» returns common ME function words
    # (wite/ich/u/hi/sei) alongside content words. Tell the renderer to
    # set user expectations honestly so the answer doesn't look broken.
    if rows and isinstance(raw, dict):
        raw["_render_note"] = (
            f"family={family} расширяется через ETYMOLOGY_FAMILY_GROUPS — "
            f"для germanic это включает Old/Middle English функциональные "
            f"слова (wite, ich, hi, u, sei) И content words. Если user "
            f"ожидал redакторские content words (sword, blade, dread) — "
            f"скажи прямо: «вот всё что имеет germanic etymology в "
            f"корпусе X с min_corpus_count={min_corpus_count}; повысь "
            f"min_corpus_count или используй pos_filter=ADJ для более "
            f"специфичной выборки»."
        )

    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            "no_matches",
            f"no words of family={family} above min_corpus_count="
            f"{min_corpus_count}",
        ))

    # v1 find_words_by_etymology has no books_total in its declared
    # contract — leave coverage opaque.
    result = ToolResult.success(
        tool="find_words_by_etymology", data=raw,
        coverage=Coverage(books_matched=-1, books_total=-1),
        warnings=warnings,
        query=query,
    )

    # v5 Phase 2.5 — TOP_N_TABLE view of found words.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason
        if not isinstance(raw, dict):
            return result
        scope_str = (str(scope) if not isinstance(scope, dict)
                     else f"книга {scope.get('book') or scope.get('pg_id')}"
                     if scope.get("book") or scope.get("pg_id")
                     else f"автор {scope.get('author')}"
                     if scope.get("author") else "корпус")
        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "corpus_count"],
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=(
                    f"Не нашлось слов family={family} в {scope_str} "
                    f"при min_corpus_count={min_corpus_count}."
                ),
                empty_message_en=f"No {family} words above threshold.",
                empty_filters_applied={"family": family,
                                        "min_corpus_count": min_corpus_count},
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1FindWordsByEtymology rows: word, affinity, occurrences,
            # corpus_count, family_chain, raw_codes. No `count`/`lemma`.
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
                "corpus_count": r.get("corpus_count") or "—",
            })
        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "corpus_count"],
            headline=f"Слова family={family} — {scope_str}",
            requested_n=top,
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except Exception as e:
        import logging
        logging.getLogger("wordcracker.v2.tools.words.etymology").warning(
            "find_words_by_etymology view emission failed: %s", e,
        )
    return result
