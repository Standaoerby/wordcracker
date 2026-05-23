"""Word-level plan builders.

`word_contexts`, `word_collocates`, `word_timeline`, `word_pos`,
`word_etymology`, `word_emotion`, `word_dialogue`, `word_movement`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import (
    PlanStep,
    QueryPlan,
    _need_word,
    _scope_dict_or_clarify,
    _scope_from,
    _with_copyright_check,
)


def _plan_word_contexts(e: Entities) -> QueryPlan:
    if not e.word:
        return _need_word(e)
    if e.author_regex:
        # Phase 4 — fan-out invariant. Single primary step + marker;
        # router clones for `multi_author_regex[:3]`. (E5 root cause:
        # Sprint 17 Round 7 Q8 — «примеры ajar у Остин/Диккенса/Дойла»
        # used to dispatch only to Austen because each builder re-
        # implemented fan-out inline and forgot the secondaries.)
        steps = [PlanStep(
            tool="word_contexts",
            args={"author_regex": e.author_regex,
                  "word": e.word, "max_samples": 8},
            fan_out="author_regex",
        )]
        explain = f"word_contexts({e.author_regex}, {e.word})"
        if e.multi_author_regex:
            explain += f" + fan-out [{len(e.multi_author_regex[:3])} more]"
        return QueryPlan(
            intent="word_contexts", entities=e, steps=steps,
            expected_cost="cheap",
            explain=explain,
        )
    # No author scope → hybrid_search if FTS5 is available, else legacy
    # word_contexts_global. hybrid pulls per_retriever from each side,
    # RRF-merges to top 12, optionally reranks with BGE cross-encoder
    # before slicing the final k. Sprint 18: rerank ON by default for
    # the no-author path — bi-encoder ranking surfaces lots of marginal
    # mentions; cross-encoder eliminates them.
    #
    # Sprint 21 B101: also fan out enrich_word in parallel so the
    # renderer can surface translation + IPA + POS + definition +
    # etymology alongside the contexts. enrich_word is Wiktionary-
    # cached (~1.5s first call, <5ms cached); independent of contexts
    # so the router runs them in parallel. enrich is optional — single-
    # word queries without author scope are the «расскажи мне про
    # слово X» case (Stan B101: «отдавать перевод слова, примеры в
    # контексте и этимологию» вместе).
    # Sprint 22+ B4: pass lang_hint through so «английская классика»
    # / «русский корпус» actually filter (Round 12 Q5 regression — 8/10
    # results were Finnish/Hungarian/Italian without filter).
    hs_args = {"query": e.word, "k": 12,
               "per_retriever": 50,
               "rerank_with": "bge_reranker"}
    if e.lang_hint:
        hs_args["lang"] = e.lang_hint
    return QueryPlan(
        intent="word_contexts", entities=e,
        steps=[
            PlanStep(tool="hybrid_search", args=hs_args),
            PlanStep(tool="enrich_word",
                     args={"word": e.word, "target_lang": "ru"},
                     optional=True),
        ],
        expected_cost="medium",
        explain=(f"hybrid_search({e.word}, lang={e.lang_hint or '*'}) "
                 f"— FTS5+Chroma RRF + BGE rerank, "
                 f"+ enrich_word in parallel (translation+etymology)"),
    )


def _plan_word_collocates(e: Entities) -> QueryPlan:
    if not e.word:
        return _need_word(e)
    scope_or_plan = _scope_dict_or_clarify(
        e, intent="word_collocates",
        hint=("Уточни — у какого автора или книги ищем соседей слова? "
              "Или укажи период (например «викторианский»)."),
    )
    if isinstance(scope_or_plan, QueryPlan):
        return scope_or_plan

    # Phase 4 — fan-out invariant. Single primary step + marker; router
    # clones per `multi_author_regex[:3]`. E5 root cause was builders
    # re-implementing the fan-out branch separately and dropping it on
    # «соседи fog у Лавкрафта и По одновременно».
    fan_out_marker = (
        "scope_author"
        if e.author_regex and isinstance(scope_or_plan, dict)
        else None
    )
    return QueryPlan(
        intent="word_collocates", entities=e,
        steps=[PlanStep(tool="word_collocates",
                        args={"scope": scope_or_plan, "word": e.word,
                              "window": 4, "top": e.top_n or 20},
                        fan_out=fan_out_marker)],
        expected_cost="medium",
        explain=f"word_collocates({scope_or_plan}, {e.word})"
                + (f" + fan-out [{len(e.multi_author_regex[:3])} more]"
                   if fan_out_marker and e.multi_author_regex else ""),
    )


_MULTI_WORD_TIMELINE_RE = None


def _detect_multi_word_timeline(raw: str, primary: str | None) -> list[str]:
    """Sprint 18 — Round 8 C5: «timeline telephone+automobile+aeroplane»
    or «timeline telephone, automobile, aeroplane» — capture all bare
    lowercase Latin tokens chained by «+» or «,». Cap at 5 to bound
    wall-clock. Returns deduped list ordered by appearance, with the
    primary entity word first if present."""
    import re
    global _MULTI_WORD_TIMELINE_RE
    if _MULTI_WORD_TIMELINE_RE is None:
        # Triggers: «timeline X+Y+Z», «X, Y и Z по эпохам», «частота X+Y»
        _MULTI_WORD_TIMELINE_RE = re.compile(
            r"\b([a-z]{3,30})(?:\s*[+,]\s*([a-z]{3,30}))+",
            re.IGNORECASE,
        )
    if not raw:
        return [primary] if primary else []
    # Find the «X+Y» or «X, Y» span anywhere in the query
    m = _MULTI_WORD_TIMELINE_RE.search(raw)
    if not m:
        return [primary] if primary else []
    # Walk the matched span and split on + / , — captures every token
    span = m.group(0)
    tokens = re.split(r"\s*[+,]\s*", span)
    out: list[str] = []
    seen: set[str] = set()
    if primary and primary not in seen:
        out.append(primary.lower())
        seen.add(primary.lower())
    for t in tokens:
        t = t.strip().lower()
        if not t or t in seen:
            continue
        if len(t) < 3 or len(t) > 30:
            continue
        if not t.isascii() or not t.isalpha():
            continue
        out.append(t)
        seen.add(t)
        if len(out) >= 5:
            break
    return out


def _plan_word_timeline(e: Entities) -> QueryPlan:
    if e.year_from and not e.year_to:
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=[PlanStep(tool="words_disappearing_after",
                            args={"year": e.year_from - 1, "top": e.top_n or 25})],
            expected_cost="medium",
            explain=f"words_disappearing_after({e.year_from - 1})",
        )
    # Sprint 18 — multi-word timeline (Round 8 C5). Emit N parallel
    # word_freq_timeline calls; renderer plots them side by side.
    raw = (e.raw_misc or {}).get("raw_text") or ""
    multi_words = _detect_multi_word_timeline(raw, e.word)
    if len(multi_words) > 1:
        steps = [PlanStep(
            tool="word_freq_timeline",
            args={"word": w, "bucket_years": 25},
            optional=(i > 0),  # primary required, secondaries best-effort
        ) for i, w in enumerate(multi_words[:5])]
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=steps,
            expected_cost="medium",
            explain=(f"word_freq_timeline × {len(multi_words[:5])} "
                     f"({', '.join(multi_words[:5])})"),
        )
    if e.word:
        return QueryPlan(
            intent="word_timeline", entities=e,
            steps=[PlanStep(tool="word_freq_timeline",
                            args={"word": e.word, "bucket_years": 25})],
            expected_cost="medium",
            explain=f"word_freq_timeline({e.word})",
        )
    return QueryPlan(
        intent="word_timeline", entities=e,
        steps=[PlanStep(tool="words_disappearing_after",
                        args={"year": 1920, "top": e.top_n or 25})],
        expected_cost="medium",
        explain="words_disappearing_after default",
    )


def _plan_word_pos(e: Entities) -> QueryPlan:
    if not e.word and not e.book_id:
        # default sample word that v1 prompt uses
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question="Уточни — какое слово проверить на полисемию? И в какой книге/у какого автора?",
            explain="word_pos needs target word",
        )
    scope = _scope_from(e)
    # word_pos_distribution rejects "all_corpus" string (line 1577 in
    # rag_tools.py): "bad scope; use {'book':PGid} | {'author':regex}".
    # When user asks generic «polysemy для set» with no scope, widen to
    # global-author regex which v1 _select_books treats as "all English
    # books"; max_occurrences=200 caps runtime to first 200 matches.
    if scope == "all_corpus":
        scope = {"author": ".*"}
    # Phase 4 — opt-in to the fan-out invariant when the user named a
    # primary author. «POS для light у Wodehouse и Twain» now fans out
    # at the router level, just like word_emotion / word_collocates do.
    fan_out_marker = (
        "scope_author"
        if e.author_regex and isinstance(scope, dict)
        and scope.get("author") == e.author_regex
        else None
    )
    return QueryPlan(
        intent="word_pos", entities=e,
        steps=[PlanStep(tool="word_pos_distribution",
                        args={"scope": scope, "word": e.word or "light"},
                        fan_out=fan_out_marker)],
        expected_cost="cheap",
        explain=f"word_pos_distribution({scope}, {e.word or 'light'})"
                + (f" + fan-out [{len(e.multi_author_regex[:3])} more]"
                   if fan_out_marker and e.multi_author_regex else ""),
    )


@_with_copyright_check
def _plan_word_etymology(e: Entities) -> QueryPlan:
    if e.author_regex and e.etymology_family:
        scope = {"author": e.author_regex}
        # Heavy tool — each candidate word triggers a Wiktionary HTTP call.
        # Cap top at 20 and bump min_corpus_count to 1000 so the candidate
        # pool stays small enough to finish under the 90s chat timeout.
        # Phase 4 — opt-in to the fan-out invariant: «германские слова у
        # Tolkien и Morris» now fans out at the router level uniformly.
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[PlanStep(tool="find_words_by_etymology",
                            args={"scope": scope, "family": e.etymology_family,
                                  "top": min(e.top_n or 15, 20),
                                  "min_corpus_count": 1000},
                            fan_out="scope_author")],
            expected_cost="heavy",
            explain=f"find_words_by_etymology({scope}, family={e.etymology_family}, top≤20)"
                    + (f" + fan-out [{len(e.multi_author_regex[:3])} more]"
                       if e.multi_author_regex else ""),
        )
    if e.word:
        # Sprint 21 B101: fan out word_contexts in parallel — when user
        # asks etymology of a word, they often want примеры + перевод
        # together. word_etymology returns enrich_word data (translation
        # + IPA + POS + definition + etymology); word_contexts adds the
        # corpus examples. Both independent → router runs in parallel.
        # word_contexts is optional — Wiktionary outage doesn't kill
        # the etymology answer.
        return QueryPlan(
            intent="word_etymology", entities=e,
            steps=[
                PlanStep(tool="word_etymology", args={"word": e.word}),
                PlanStep(tool="hybrid_search",
                         args={"query": e.word, "k": 6,
                               "per_retriever": 30,
                               "rerank_with": "bge_reranker"},
                         optional=True),
            ],
            expected_cost="cheap",
            explain=(f"word_etymology({e.word}) + hybrid_search({e.word}, k=6)"
                     f" parallel — Stan B101 bundle"),
        )
    return QueryPlan(
        intent="clarify", entities=e, steps=[],
        needs_clarify=True,
        clarify_question="Этимологию какого слова — или нужно «германские/латинские слова у автора X»?",
        explain="etymology needs word or (author, family)",
    )


def _plan_word_emotion(e: Entities) -> QueryPlan:
    scope_or_plan = _scope_dict_or_clarify(
        e, intent="word_emotion",
        hint=("Уточни scope: у какого автора/книги/в какой эпохе искать "
              "эмоциональный контекст? Пример: «слова страха у По» или "
              "«мрачные слова у викторианцев»."),
    )
    if isinstance(scope_or_plan, QueryPlan):
        return scope_or_plan
    emotion = e.emotion or "fear"

    # Phase 4 — fan-out invariant. Single primary step + marker; the
    # router clones per `multi_author_regex[:3]`. Closes E5 root cause
    # («слова страха у По и Лавкрафта одновременно» used to drop the
    # secondary author silently — builders re-implemented fan-out per-
    # tool and missed cases).
    fan_out_marker = (
        "scope_author"
        if e.author_regex and isinstance(scope_or_plan, dict)
        else None
    )
    return QueryPlan(
        intent="word_emotion", entities=e,
        steps=[PlanStep(tool="emotion_collocates",
                        args={"scope": scope_or_plan, "emotion": emotion,
                              "window": 4, "top": e.top_n or 25},
                        fan_out=fan_out_marker)],
        expected_cost="medium",
        explain=f"emotion_collocates({scope_or_plan}, {emotion})"
                + (f" + fan-out [{len(e.multi_author_regex[:3])} more]"
                   if fan_out_marker and e.multi_author_regex else ""),
    )


def _plan_word_dialogue(e: Entities) -> QueryPlan:
    return QueryPlan(
        intent="word_dialogue", entities=e, steps=[],
        out_of_scope_reason=(
            "Корпус не размечен на диалоги vs нарратив. Это требует "
            "отдельной аннотации, которой пока нет."
        ),
        explain="word_dialogue → out_of_scope для v2-alpha",
    )


def _plan_word_movement(e: Entities) -> QueryPlan:
    # Top-ngrams with author_regex='.*' over a 100-year window scans
    # ~20k books per token (5GB+ of token files). Even with top≤30 cap
    # and POS filter it consistently blows past the 120s chat budget.
    # Without an author or smaller scope we can't satisfy this query
    # in chat — be honest about that. Suggest a narrower scope.
    if not e.author_regex and not e.country and not e.book_id:
        return QueryPlan(
            intent="word_movement", entities=e, steps=[],
            out_of_scope_reason=(
                "Запрос «глаголы движения в XIX веке» требует сканирования "
                "20k+ книг — это превышает бюджет чата (90-120с). Сузь "
                "scope: укажи автора («у Диккенса»), страну («британские»), "
                "или конкретную книгу. Можно также спросить про глаголы "
                "у конкретного автора через `affinity_by_author(pos_filter=['VERB'])`."
            ),
            explain="word_movement без scope → too expensive for chat",
        )
    yf, yt = e.year_from, e.year_to
    if not yf and not yt:
        yf, yt = 1800, 1899
    return QueryPlan(
        intent="word_movement", entities=e,
        steps=[PlanStep(tool="top_ngrams_by_author",
                        args={"author_regex": e.author_regex or ".*",
                              "n": 1, "top": min(e.top_n or 25, 30),
                              "pos_filter": ["VERB"],
                              # E2 (R-22 P2): semantic-class filter applies
                              # motion-verb lexicon (closed list ~200 verbs)
                              # over the top-affinity VERB results. Without
                              # this, «глаголы движения у Диккенса» returned
                              # said/replied/cried — top affinity but NOT
                              # motion.
                              "semantic_class": "motion",
                              "year_from": yf, "year_to": yt,
                              "country": e.country})],
        expected_cost="heavy",
        explain=f"top_ngrams_by_author over {yf}-{yt}, POS=VERB+motion-lexicon, top≤30",
    )
