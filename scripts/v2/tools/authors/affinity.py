"""v2 affinity_by_author + compare_authors — author-level stylistic stats."""
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


# Stan's 2026-05-18 round 3: «характерные прилагательные Оскара Уайльда»
# returned «ernest, caliban, nazarene, parnassus» tagged as ADJ. These are
# proper nouns spaCy systematically mis-tags when fed isolated lowercased
# tokens. Hard-coded blacklist of the most common literary names /
# mythological figures / classical place names that bleed through the
# regular PROPN filter. Extend pragmatically.
_LITERARY_PROPN_BLACKLIST = frozenset({
    # Wilde characters & associates (Salome, Importance of Being Earnest,
    # Dorian Gray, Lady Windermere's Fan, Vera, A Woman of No Importance)
    "ernest", "algernon", "bunbury", "cecily", "gwendolen", "lady bracknell",
    "tetrarch", "cardew", "daubeny", "phipps", "simone", "symonds", "otis",
    "raff", "yeats", "topazes", "salome", "herodias", "lord goring",
    # «The Tempest» (Shakespeare) — Caliban, Prospero
    "caliban", "prospero", "ariel", "miranda",
    # Religious / classical proper nouns mis-tagged as ADJ
    "nazarene", "parnassus", "olympus", "elysian", "stygian", "tartarus",
    # Common literary place names
    "albion", "avalon", "camelot", "atlantis", "babylon",
    # Wodehouse character / place names
    "wrykyn", "threepwood", "blandings",
    # Misc historical figures often used as adjectives but proper-nouny
    "dorian",  # «Dorian Gray» — character name in Wilde's most famous book
    "byronic", "miltonic",  # too author-coupled for genuine ADJ usage
    # Russian translit character names from Pushkin, Tolstoy, etc.
    "gavril", "lisaveta", "korsakoff", "tomsky", "andreitch", "vassilissa",
    "alexei", "simbirsk", "kibitka", "petr", "dounia", "lenski", "lensky",
    "izaveta", "bashkirs", "marya", "polacca", "ivan", "petrovitch",
    "boyars", "desforges", "mossoo", "aleko", "beaupre", "onegin",
    "petrovich", "vladimir", "andrei", "tsarevich",
})


def _drop_author_self_name(author_regex: str, words: list[dict]) -> list[dict]:
    """Stan round 2 Q3: «фирменные слова Уайльда» returned **wilde** as a
    signature word. An author's own name shouldn't be in their signature
    vocabulary — that's their *attribution* leaking through the text, not
    their style. Extract the surname from `^Surname,` regex and drop it
    if it shows up in the result list.
    """
    if not author_regex:
        return words
    # «^Wodehouse,» → "wodehouse"
    import re
    m = re.match(r"\^([A-Za-zЀ-ӿ'-]+)", author_regex)
    if not m:
        return words
    surname = m.group(1).lower()
    return [w for w in words if (w.get("word") or "").lower() != surname]


@tool(
    name="affinity_by_author",
    category="authors",
    description=(
        "Фирменные слова автора по метрике affinity (частота у автора vs корпус). "
        "Используй для «фирменные слова X», «характерные», «маркеры стиля». "
        "POS-фильтр через pos_filter=['ADJ'/'NOUN'/'VERB']."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex":     {"type": "string"},
            "top":              {"type": "integer", "description": "default 50"},
            "min_author_count": {"type": "integer", "description": "default 5"},
            "min_corpus_count": {"type": "integer",
                                 "description": "default 0; bump to 500+ для filtering OOV/имен"},
            "pos_filter":       {"type": "array", "items": {"type": "string"}},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
)
def affinity_by_author(author_regex: str, top: int = 50,
                       min_author_count: int = 5, min_corpus_count: int = 0,
                       pos_filter: list[str] | None = None) -> ToolResult:
    try:
        from scripts.rag_tools import affinity_by_author as _v1
    except ImportError as e:
        return ToolResult.fail(tool="affinity_by_author", err_type="internal",
                               message=f"v1 unavailable: {e}")

    raw = _v1(author_regex=author_regex, top=top,
              min_author_count=min_author_count,
              min_corpus_count=min_corpus_count, pos_filter=pos_filter)
    query = {"author_regex": author_regex, "top": top,
             "min_author_count": min_author_count,
             "min_corpus_count": min_corpus_count, "pos_filter": pos_filter}

    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        return ToolResult.fail(
            tool="affinity_by_author",
            err_type=("not_found" if "no books" in err.lower() else "internal"),
            message=err, query=query,
        )

    # v1 may return rows under "top" (old) or "top_words" (new). Normalize.
    if isinstance(raw, dict):
        rows = raw.get("top_words") or raw.get("top") or []
    else:
        rows = []
    # Post-filter literary proper nouns spaCy mis-tagged as ADJ/NOUN. Quick
    # blacklist beats redoing NER per request. Stan's demon: «характерные
    # прилагательные Уайльда» came back with «ernest, caliban, nazarene,
    # parnassus» — all character / classical names.
    # Also drop the author's own surname if it leaks through (round-2 Q3:
    # Wilde signature words included «wilde»).
    # Sprint 19+ (2026-05-19): top of Conan Doyle's signature list was
    # mostly character surnames (challenger/knolles/barrymore/holmes/
    # stapleton/mcfarlane/baumgarten). The corpus-diff heuristic +
    # spaCy PROPN + word_dict propn cache all leak on ambiguous-cased
    # tokens. New layer: positive surname signal from PG metadata authors
    # + curated character set. See `_surname_filter.py`.
    if rows:
        rows = _drop_author_self_name(author_regex, rows)
        before_lit = len(rows)
        rows = [r for r in rows
                if (r.get("word") or "").lower()
                not in _LITERARY_PROPN_BLACKLIST]
        lit_dropped = before_lit - len(rows)
        rows, surname_dropped = filter_surnames(rows)
        # Sprint 20 — drop OCR/markup artifacts (Roman-numeral ordinals
        # like «xvth», single-char words, consonant-only clusters).
        # Stan 2026-05-19: «xvth» = «XV-th век» from broken text
        # rendering — not vocabulary.
        rows, artifact_dropped = filter_corpus_artifacts(rows)
        if lit_dropped or surname_dropped or artifact_dropped:
            note = (raw.get("proper_noun_filter") or "") if isinstance(raw, dict) else ""
            extra = (f"; v2 literary blacklist dropped {lit_dropped}, "
                      f"v2 surname blocklist dropped {surname_dropped}, "
                      f"v2 corpus-artifact filter dropped {artifact_dropped}")
            if isinstance(raw, dict):
                raw["proper_noun_filter"] = (note + extra).lstrip("; ")
        # Propagate the filtered list back so the LLM renders only
        # the clean list, not the raw one. Mutate `raw` in place
        # (top_words / top, whichever was the source).
        if isinstance(raw, dict):
            if "top_words" in raw:
                raw["top_words"] = rows
            elif "top" in raw:
                raw["top"] = rows

    # Sprint 20 — count-honesty signal. Stan 2026-05-19: renderer wrote
    # «вот список из 50 слов» when affinity returned 19 after filtering.
    # Critic caught it but the answer still misled the user. Surface
    # the requested-vs-returned mismatch as a structured field + a
    # mandatory _render_note so the LLM cannot quietly use the
    # original `top` figure.
    actual = len(rows) if rows else 0
    if isinstance(raw, dict):
        raw["top_requested"] = top
        raw["top_returned"] = actual
        if actual < top:
            # Always say the actual number, never the requested one,
            # when there's a delta — even by 1 word.
            existing = raw.get("_render_note") or ""
            count_note = (
                f"ACTUAL COUNT: tool returned {actual} words after "
                f"PROPN / surname / corpus-diff filtering — NOT the {top} "
                f"requested. Use {actual} in the answer. If you need to "
                f"mention the requested number, phrase it explicitly: "
                f"«запросил {top}, после фильтра имён собственных и "
                f"редких токенов осталось {actual}»."
            )
            raw["_render_note"] = (
                (existing + " | " if existing else "") + count_note
            )

    warnings: list[ToolWarning] = []
    if not rows:
        warnings.append(ToolWarning(
            code="empty_top",
            message="affinity returned no words — perhaps min_corpus_count too high",
        ))
    elif actual < top:
        warnings.append(ToolWarning(
            code="under_filled",
            message=(f"requested top={top}, returned {actual} after "
                      f"filtering — renderer must say {actual}"),
        ))
    return ToolResult.success(
        tool="affinity_by_author", data=raw,
        coverage=Coverage(books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=warnings, query=query,
    )


@tool(
    name="compare_authors",
    category="authors",
    description=(
        "Сравнение двух авторов: топ фирменных слов каждого, пересечение, "
        "cosine similarity affinity-векторов. Для «сравни X и Y»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author1_regex":    {"type": "string"},
            "author2_regex":    {"type": "string"},
            "top":              {"type": "integer", "description": "default 20"},
            "min_corpus_count": {"type": "integer", "description": "default 500"},
        },
        "required": ["author1_regex", "author2_regex"],
    },
    requires=["author"],
    cost="medium",
    cacheable=True,
)
def compare_authors(author1_regex: str, author2_regex: str, top: int = 20,
                    min_corpus_count: int = 500) -> ToolResult:
    try:
        from scripts.rag_tools import compare_authors as _v1
    except ImportError as e:
        return ToolResult.fail(tool="compare_authors", err_type="internal",
                               message=f"v1 unavailable: {e}")

    raw = _v1(author1_regex=author1_regex, author2_regex=author2_regex,
              top=top, min_corpus_count=min_corpus_count)
    query = {"author1_regex": author1_regex, "author2_regex": author2_regex,
             "top": top, "min_corpus_count": min_corpus_count}

    if isinstance(raw, dict) and raw.get("error"):
        err = str(raw["error"])
        # Surface the "no matching books" case as not_found, so the renderer
        # can suggest alternatives rather than treating it as a hard failure.
        return ToolResult.fail(
            tool="compare_authors",
            err_type=("not_found" if "no books" in err.lower() or "not produced" in err.lower()
                      else "internal"),
            message=err, query=query,
        )

    # If either author's top list is empty, flag — that's the v1.1.7 partial.
    warnings: list[ToolWarning] = []
    empty_sides: list[tuple[str, str]] = []  # filled below
    if isinstance(raw, dict):
        for label, key, regex in (
            ("author1", "top_unique_a", author1_regex),
            ("author2", "top_unique_b", author2_regex),
        ):
            if not raw.get(key):
                warnings.append(ToolWarning(
                    code=f"{label}_empty",
                    message=f"{label} produced no signature words — "
                            f"check if author exists in SPGC corpus",
                ))
                empty_sides.append((label, regex))

        # Stan's 2026-05-18 demon: cosine=0.0 between Poe and Lovecraft
        # looked suspicious. It's structurally near-zero because top-N
        # affinity vectors for distinct authors barely overlap by design.
        # Add a direct integer the LLM can quote — number of shared
        # high-affinity words between the two — and an explicit flag so
        # the render explains the zero rather than treating it as a real
        # distance metric.
        cosine = raw.get("cosine_similarity", 0.0)
        if isinstance(cosine, (int, float)) and cosine < 0.05:
            raw["cosine_is_structural_zero"] = True
            shared = raw.get("shared_high_affinity") or []
            raw["shared_top_words_count"] = len(shared)
            existing = raw.get("_render_note", "")
            raw["_render_note"] = (
                (existing + " " if existing else "")
                + "cosine_similarity ниже 0.05 — это СТРУКТУРНОЕ свойство "
                "метрики (top-N фирменных слов у разных авторов почти не "
                "пересекаются), а НЕ показатель стилистической дистанции. "
                "Не говори пользователю «авторы совершенно разные» из-за "
                "cosine=0. Расскажи про shared_high_affinity (общие "
                "стилистические маркеры) и про top_unique_a / "
                "top_unique_b — это содержательная часть ответа."
            )

        # Round 10 Q15 P0 — Lovecraft hallucination regression (6-round
        # persistent). When one side returns 0 signature words, renderer
        # historically invented words («rhyming/amended/journalism/
        # editorials/corey») to «balance» the compare. Forceful
        # _render_note + structured empty_sides field tells the LLM to
        # render only the present side with an honest explanation. Runs
        # AFTER the cosine block so both notes co-exist on the same
        # ToolResult.
        if empty_sides:
            empty_labels = ", ".join(f"{lbl} ({rx})" for lbl, rx in empty_sides)
            raw["empty_sides"] = [{"label": lbl, "regex": rx}
                                   for lbl, rx in empty_sides]
            present_text = (
                "ПОКАЖИ только signature words той стороны, у которой "
                "top_unique НЕ пустой. "
                if len(empty_sides) == 1
                else "ОБЕ стороны пусты — скажи прямо: «не нашлось "
                "signature words ни у одной из двух сторон». "
            )
            existing = raw.get("_render_note", "")
            raw["_render_note"] = (
                (existing + " | " if existing else "")
                + f"⚠ КРИТИЧНО: у {empty_labels} top_unique=пустой массив "
                f"(0 signature words в индексе с "
                f"min_corpus_count={min_corpus_count}). "
                + present_text
                + "ЗАПРЕЩЕНО изобретать слова чтобы заполнить пустую "
                "сторону — это галлюцинация. Скажи пользователю честно: "
                "«у этого автора в нашем индексе не нашлось уникальных "
                "стилистических маркеров; возможно мало книг под этим "
                "author_regex или нужно понизить min_corpus_count». "
                "6-round persistent bug Q15."
            )

    # Sprint 20+ B2 — stamp metric_explanations so renderer doesn't
    # invent direction. Stan Round 11: «чем выше delta, тем сильнее
    # влияние» — НЕВЕРНО, distance metric, lower = closer.
    if isinstance(raw, dict):
        raw.setdefault("metric_explanations", []).extend([
            {"metric": "burrows_delta",
              "direction": "LOWER = more similar style (distance metric)",
              "scale": "typically 0.3-1.5; <0.5 close, >0.8 distinct",
              "interpret": "Stevenson 0.4385 closer to Doyle than Twain 0.6021"},
            {"metric": "cosine_similarity",
              "direction": "HIGHER = more similar (affinity vectors)",
              "scale": "0-1; cosine on top-N affinity word vectors",
              "interpret": "structural near-zero common — see "
                           "cosine_is_structural_zero flag"},
            {"metric": "shared_high_affinity",
              "direction": "count of overlapping signature words; "
                           "more shared = more aligned vocabulary",
              "scale": "0+; integer",
              "interpret": "0 shared between distinct genres is normal"},
        ])
    return ToolResult.success(
        tool="compare_authors", data=raw,
        coverage=Coverage(
            books_matched=raw.get("books_a", -1) + raw.get("books_b", -1)
                          if isinstance(raw, dict) else -1,
            books_total=-1,
        ),
        warnings=warnings, query=query,
    )
