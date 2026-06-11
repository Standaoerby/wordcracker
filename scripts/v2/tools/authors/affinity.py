"""v2 affinity_by_author + compare_authors — author-level stylistic stats."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("wordcracker.v2.tools.authors.affinity")

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning
from scripts.v2.tools.authors._surname_filter import filter_surnames
from scripts.v2.tools.authors._corpus_artifacts import filter_corpus_artifacts
from scripts.v2.tools.authors._toponym_filter import filter_toponyms
from scripts.v2.tools.authors._propn_dominance import filter_propn_dominance
from scripts.v2.tools.authors._propn_gazetteer import (
    filter_proper_names,
    filter_by_cap_ratio,
    find_proper_names,
)
from scripts.v2.contracts import v1_contract
from scripts.v2.contracts.schemas import V1AffinityByAuthor, V1CompareAuthors


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
    # Sprint 22+ Round 12 Q10 — Wilde character names that slipped through:
    # goring (Lord Goring — Ideal Husband), worthing (Jack Worthing —
    # Earnest), chasuble (Canon Chasuble — Earnest). Plain surname forms.
    "goring", "worthing", "chasuble", "prism",  # Miss Prism
    "moncrieff", "fairfax",                       # other Earnest names
    "wotton", "hallward", "sibyl",               # Dorian Gray
    "windermere", "darlington", "berwick",       # Lady Windermere
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


def _normalize_compare_shape(raw):
    """E15 P0 FIX (2026-05-22): v1 compare_authors (rag_tools.py:857)
    returns NESTED shape:
        {"author1": {"regex", "slug", "top_unique": [...]},
         "author2": {"regex", "slug", "top_unique": [...]},
         "shared_high_affinity": [...],
         "cosine_similarity": float,
         "cosine_note": str,
         "min_corpus_count": int}
    NOT flat «top_unique_a / top_unique_b / books_a / books_b /
    burrows_delta». Old wrapper read flat keys → top_unique_a/b always
    empty → retry chain triggered every time → all views silently empty.
    Phantom «burrows_delta» reference (never in v1 output) made the
    metric row always None.

    This normalizes the v1 shape to expose flat aliases the rest of
    the wrapper (and downstream renderer + view) already expects.
    Mutates raw in place; legacy flat-key test mocks pass through
    unchanged because we only set keys that are missing.
    Same class as B-R14-7 / E9 / E14b / E15.
    """
    if not isinstance(raw, dict):
        return raw
    a1 = raw.get("author1") if isinstance(raw.get("author1"), dict) else None
    a2 = raw.get("author2") if isinstance(raw.get("author2"), dict) else None
    # Phase 2 — read only the canonical v1 row keys per V1CompareAuthors.
    # v1 nests `top_unique` inside author1/author2 (rag_tools.py:858).
    if a1 is not None and "top_unique_a" not in raw:
        raw["top_unique_a"] = a1.get("top_unique") or []
        raw["slug_a"] = a1.get("slug")
    if a2 is not None and "top_unique_b" not in raw:
        raw["top_unique_b"] = a2.get("top_unique") or []
        raw["slug_b"] = a2.get("slug")
    return raw


def _author_token_files(author_regex: str) -> list:
    """SPGC tokens files for the author's books — the case-preserving
    source the cap-ratio name layer scans (R-27 WP3). Sorted for
    determinism. Returns [] when the corpus isn't mounted (local dev /
    CI) — the cap-ratio layer then reports verified=False and `clean`
    stays False instead of claiming a complete name filter."""
    try:
        from scripts.rag_tools import _select_books, _tokens_path
        sel = _select_books(author_regex)
        ids = sorted(str(b) for b in sel["id"].tolist())
    except Exception:
        return []
    return [_tokens_path(b) for b in ids]


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
    # Phase 0 ($s2.words[N] P0): we now expose a `words` alias on the
    # output dict so plan-spec refs like `$s2.words[0].word` resolve
    # against the affinity result (LLM planner emits this shape; v1 key
    # is `top` / `top_words`). Bumping wrapper_version busts any cached
    # results from before the alias landed. Structural fix in Phase 2
    # via v1↔v2 contracts.
    # Phase 3 W-4 (2026-05-22) — wired toponym filter + extended surname
    # blocklist (wegg/smike/toots/jip/etc) and PROPN blacklist. Bumping
    # wrapper_version invalidates cached results that still carry
    # «burger»/«uitlanders»/Boer-war GPE leakage at Doyle.
    # W-4 reconciliation (2026-05-24) — added generic PROPN-affinity
    # dominance heuristic (`filter_propn_dominance`) so rare character /
    # place names not in the curated blocklist still get caught when
    # their shape (high affinity + low corpus_count + high exclusivity)
    # matches a proper-noun dominator. Auto-discover NER GPE/LOC CSVs
    # so the production blocklist enrichment kicks in without per-call
    # paths. Bumping wrapper_version invalidates cached rows from
    # before the heuristic landed.
    # R-27 WP3 (2026-06-11) — Q7 live repro: hélène/sergius/petrovna/
    # hippolyte/nicholas survived every prior layer (lowercase given
    # names + accents fold past spaCy POS, word_dict and the surname
    # blocklists). Added the accent-folded given-name gazetteer +
    # patronymic suffixes + corpus capitalization-ratio layer, and the
    # `clean` flag (True only when the post-filter detector scan over
    # the FINAL rows finds zero names AND the cap-ratio verification
    # actually ran). Bump invalidates cached rows recorded by the leaky
    # chain.
    wrapper_version="v5-r27-propn-gazetteer",
)
@v1_contract(v1_fn="scripts.rag_tools.affinity_by_author",
             schema=V1AffinityByAuthor)
def affinity_by_author(author_regex: str, top: int = 50,
                       min_author_count: int = 5, min_corpus_count: int = 0,
                       pos_filter: list[str] | None = None) -> ToolResult:
    from scripts.rag_tools import affinity_by_author as _v1

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

    # Phase 2 — v1 affinity_by_author returns rows under the canonical
    # `top` key (rag_tools.py:747). The wrapper used to also try
    # `top_words` — a phantom key v1 never emits. Contract-bound now.
    if isinstance(raw, dict):
        rows = raw.get("top") or []
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
        # Phase 3 W-4 — drop GPE/LOC toponyms (Boer-war places at Doyle,
        # London districts, classical place names). spaCy POS on isolated
        # lowercased tokens misses them; curated blocklist catches the
        # 19th-c. literary toponym set deterministically.
        rows, toponym_dropped = filter_toponyms(rows)
        # W-4 (2026-05-24) — generic PROPN-affinity dominance heuristic.
        # Catches rare proper nouns NOT in any curated blocklist when
        # their (high affinity, low corpus_count, high exclusivity) shape
        # matches a character / minor-place name. Runs LAST so curated
        # filters get the first pass and the heuristic only sees survivors.
        rows, propn_dom_dropped = filter_propn_dominance(rows)
        # R-27 WP3 — given-name gazetteer + patronymics (hélène, sergius,
        # petrovna, hippolyte, nicholas — the Q7 leak class). Accent-folded
        # exact match, so iceberg/bergamot survive while «berg» drops.
        rows, gazetteer_dropped = filter_proper_names(rows)
        # R-27 WP3 — corpus capitalization-ratio: SPGC tokens preserve
        # case, a token predominantly Capitalized in the author's texts
        # is a name even when the counts pipeline saw it lowercased.
        # verified=False (no tokens files — local dev/CI) keeps `clean`
        # False below: we may NOT claim a complete name filter then.
        rows, cap_dropped, cap_verified = filter_by_cap_ratio(
            rows, _author_token_files(author_regex))
        if (lit_dropped or surname_dropped or artifact_dropped
                or toponym_dropped or propn_dom_dropped
                or gazetteer_dropped or cap_dropped):
            note = (raw.get("proper_noun_filter") or "") if isinstance(raw, dict) else ""
            extra = (f"; v2 literary blacklist dropped {lit_dropped}, "
                      f"v2 surname blocklist dropped {surname_dropped}, "
                      f"v2 corpus-artifact filter dropped {artifact_dropped}, "
                      f"v2 toponym filter dropped {toponym_dropped}, "
                      f"v2 propn-dominance heuristic dropped {propn_dom_dropped}, "
                      f"v2 name gazetteer dropped {gazetteer_dropped}, "
                      f"v2 capitalization-ratio dropped {cap_dropped}")
            if isinstance(raw, dict):
                raw["proper_noun_filter"] = (note + extra).lstrip("; ")
        # Propagate the filtered list back so the LLM renders only
        # the clean list, not the raw one. v1 canonical key is `top`.
        if isinstance(raw, dict):
            raw["top"] = rows
    else:
        cap_verified = False

    # R-27 WP3 (1d) — `clean` semantics: True ONLY when the post-filter
    # detector pass over the FINAL rows finds zero proper names AND the
    # cap-ratio verification actually ran (corpus mounted). Anything
    # else is a PARTIAL name filter and the renderer must not claim a
    # complete one (RENDER_PROMPT rule 21 addendum + critic
    # claimed-vs-shown back-stop both key off this flag).
    if isinstance(raw, dict):
        leftover = find_proper_names(
            [(r.get("word") or "") for r in rows]) if rows else []
        raw["clean"] = bool(not leftover and (cap_verified or not rows))
        if leftover:
            raw["_propn_leftover"] = leftover[:10]

    # Phase 0 — `$s2.words[N]` P0 bind. The LLM planner sometimes emits
    # plans that reference this step's rows as `$s2.words[N]` (instead
    # of `$s2.top[N]`). Without an alias the ref resolves to None and
    # the literal placeholder reaches enrich_word + the renderer. We
    # expose `words` as a synonym for the filtered rows. Structural fix
    # is Phase 2 (v1↔v2 contracts + loud R9 error on unresolved ref).
    if isinstance(raw, dict):
        raw["words"] = rows if rows else []

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
            # R-27 WP3 (task 3) — the suggested phrasing depends on
            # `clean`: an assertive «после фильтра имён…» is allowed
            # ONLY when the post-filter detector scan came back clean;
            # otherwise the honest wording is «применён частичный
            # фильтр — в списке могли остаться имена».
            existing = raw.get("_render_note") or ""
            if raw.get("clean"):
                phrasing = (
                    f"«запросил {top}, после фильтра имён собственных и "
                    f"редких токенов осталось {actual}»."
                )
            else:
                phrasing = (
                    f"«запросил {top}, осталось {actual}; применён "
                    f"ЧАСТИЧНЫЙ фильтр имён — в списке могли остаться "
                    f"имена». clean=false: ЗАПРЕЩЕНА утвердительная "
                    f"формулировка «после фильтрации имён собственных»."
                )
            count_note = (
                f"ACTUAL COUNT: tool returned {actual} words after "
                f"PROPN / surname / corpus-diff filtering — NOT the {top} "
                f"requested. Use {actual} in the answer. If you need to "
                f"mention the requested number, phrase it explicitly: "
                + phrasing
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
    result = ToolResult.success(
        tool="affinity_by_author", data=raw,
        coverage=Coverage(books_matched=raw.get("n_books", -1) if isinstance(raw, dict) else -1,
                          books_total=-1),
        warnings=warnings, query=query,
    )

    # v5 Phase 2 — emit TOP_N_TABLE view with caveats reflecting filter
    # drops. Closes B-R14-15 (PROPN leak class) and the count-honesty
    # class structurally: count_returned auto-set by builder; renderer
    # cannot claim "30" when actually 14.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason

        if not isinstance(raw, dict):
            return result

        author_name = author_regex.lstrip("^").rstrip(",").strip()

        if not rows:
            view = vb.build_top_n_table(
                rows=[], columns=["rank", "word", "affinity"],
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=(
                    f"Нет фирменных слов автора {author_name} при "
                    f"min_corpus_count={min_corpus_count}, "
                    f"min_author_count={min_author_count}."
                ),
                empty_message_en=(
                    f"No signature words for {author_name} at "
                    f"min_corpus_count={min_corpus_count}."
                ),
                empty_filters_applied={
                    "min_corpus_count": min_corpus_count,
                    "min_author_count": min_author_count,
                    "pos_filter": pos_filter,
                },
                empty_suggestion="Снизь min_corpus_count или поменяй POS-фильтр.",
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result

        # Build rows with rank
        view_rows = []
        for i, r in enumerate(rows[:top], start=1):
            if not isinstance(r, dict):
                continue
            # V1AffinityByAuthor canonical row key is `word` (rag_tools.py:735).
            # The pre-Phase-2 fallback to `token` was phantom — v1 never sets it.
            view_rows.append({
                "rank": i,
                "word": r.get("word") or "—",
                "affinity": (f"{r.get('affinity'):.3f}"
                              if isinstance(r.get("affinity"), (int, float))
                              else "—"),
            })

        view_caveats: list[str] = []
        pn_note = raw.get("proper_noun_filter")
        if pn_note:
            view_caveats.append(pn_note)
        # R-27 WP3 — partial-filter honesty on the template render path
        # too: clean=False means the name filter is best-effort, the
        # caveat says so instead of letting the table imply otherwise.
        if not raw.get("clean", False):
            view_caveats.append(
                "Фильтр имён частичный — в списке могли остаться имена.")

        view = vb.build_top_n_table(
            rows=view_rows,
            columns=["rank", "word", "affinity"],
            headline=f"Фирменные слова — {author_name}",
            requested_n=top,
            caveats=view_caveats,
            provenance=vb.make_provenance(
                requested={
                    "top": top, "min_corpus_count": min_corpus_count,
                    "min_author_count": min_author_count,
                    "pos_filter": pos_filter,
                },
                returned={"count": len(view_rows),
                          "n_books": raw.get("n_books")},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
        )
        vb.attach_view(result, view, data_validity=DataValidity.OK)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        log.exception("affinity_by_author view emission failed")

    return result


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
    # Sprint 22+ Stan Q15 follow-on: multi-step retry (500→200→100→50)
    # for small-corpus authors. Output schema same as v2 but the values
    # are now reliably non-empty. Bump to invalidate single-retry cache.
    # E18 (2026-05-22) — E15 added _normalize_compare_shape (nested
    # author1/author2 → flat top_unique_a/b). Phantom burrows_delta
    # field also dropped.
    # W-3 (2026-05-24) — wrapper now also exposes `raw["entities"]` as
    # a per-author row list so the LLM-render «Сравнение» table reads
    # cosine_similarity / shared_high_affinity directly per row (E38
    # «—»-in-every-cell symptom). Bump invalidates cached results that
    # lacked the `entities` key.
    wrapper_version="v6-w3-compare-entities",
)
@v1_contract(v1_fn="scripts.rag_tools.compare_authors",
             schema=V1CompareAuthors)
def compare_authors(author1_regex: str, author2_regex: str, top: int = 20,
                    min_corpus_count: int = 500) -> ToolResult:
    from scripts.rag_tools import compare_authors as _v1

    raw = _v1(author1_regex=author1_regex, author2_regex=author2_regex,
              top=top, min_corpus_count=min_corpus_count)
    # E15 — normalize v1's nested shape into flat aliases before any
    # downstream code reads top_unique_a/b. See _normalize_compare_shape.
    raw = _normalize_compare_shape(raw)
    query = {"author1_regex": author1_regex, "author2_regex": author2_regex,
             "top": top, "min_corpus_count": min_corpus_count}

    # Sprint 21+ Q15 P0 hotfix — auto-retry with lower threshold when
    # BOTH sides return empty. Stan prod 2026-05-20: «сравни По и
    # Лавкрафта» with default min_corpus_count=2000 yielded empty top
    # both sides because rare stylistic markers («cthonic», «eldritch»,
    # «raven») don't meet a 2000-occurrence corpus-wide floor for
    # authors with only 22-33 books indexed. Auto-retry once at //4
    # threshold so small-corpus authors get a real comparison instead
    # of an empty-result hallucination footer. The retry is silent —
    # we just report the threshold actually used in `min_corpus_count_used`.
    if isinstance(raw, dict) and min_corpus_count >= 1000:
        both_empty = (not raw.get("top_unique_a")
                       and not raw.get("top_unique_b"))
        if both_empty:
            # Sprint 22+ Round 12 Q15 follow-on: step-down through several
            # thresholds. Stan prod 2026-05-20 second-attempt: single retry
            # at //4 (500) for По/Лавкрафт also returned empty because rare
            # markers («raven», «cthulhu», «eldritch») need <500 to surface
            # in small-corpus (22-33 books) authors. Try descending steps
            # until we get a non-empty result OR exhaust the floor.
            for retry_threshold in (500, 200, 100, 50):
                if retry_threshold >= min_corpus_count:
                    continue
                log.info("compare_authors both-empty → retry at "
                         "min_corpus_count=%d (Q15 step-down)",
                         retry_threshold)
                try:
                    retry_raw = _v1(author1_regex=author1_regex,
                                     author2_regex=author2_regex,
                                     top=top,
                                     min_corpus_count=retry_threshold)
                    # E15 — normalize retry result too
                    retry_raw = _normalize_compare_shape(retry_raw)
                except Exception:
                    retry_raw = None
                    continue
                if isinstance(retry_raw, dict) and (
                        retry_raw.get("top_unique_a")
                        or retry_raw.get("top_unique_b")):
                    raw = retry_raw
                    raw["min_corpus_count_used"] = retry_threshold
                    raw["min_corpus_count_requested"] = min_corpus_count
                    raw["_threshold_auto_lowered"] = True
                    query["min_corpus_count_used"] = retry_threshold
                    break  # got data, stop descending

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

    # E27 (S-R4, 2026-05-29) — compare_authors leaked author surnames +
    # character names into each side's signature list. affinity_by_author
    # already scrubs its `top` rows via self-name drop → literary-PROPN
    # blacklist → surname blocklist (see ~lines 201-208); compare_authors
    # never wired the same chain. The recorded prod shape (golden fixture
    # scripts.rag_tools.compare_authors.json) proves it: top_unique_a/b
    # carry «austen»/«wilde» (the authors' own names) plus elinor/ferrars/
    # dashwood/willoughby/collins (Austen characters) and goring/worthing/
    # chasuble/algernon/cecily (Wilde characters). Stan Q3: «сравни
    # Wodehouse и Wilde» surfaced wodehouse/wilde + jeeves/goring as
    # «фирменные слова» — attribution noise, not style.
    #
    # NOTE (deviation from the literal S-R4 note, which named only
    # filter_surnames + _drop_author_self_name): the Wilde-class character
    # names (goring/worthing/chasuble/algernon/...) live in
    # _LITERARY_PROPN_BLACKLIST, NOT in the surname blocklist or PG author
    # metadata, so the two named filters alone cannot satisfy the «нет
    # персонажей» acceptance. We apply the same self-name → literary →
    # surname prefix affinity_by_author uses for this exact character class.
    # Runs BEFORE empty-side detection so a side scrubbed to empty correctly
    # trips the empty_sides path; cleaned list is written back to both the
    # flat alias and the nested author{1,2} dict the render payload exposes.
    if isinstance(raw, dict):
        a1d = raw.get("author1") if isinstance(raw.get("author1"), dict) else None
        a2d = raw.get("author2") if isinstance(raw.get("author2"), dict) else None
        for side_key, side_regex, nested in (
            ("top_unique_a", author1_regex, a1d),
            ("top_unique_b", author2_regex, a2d),
        ):
            side_rows = raw.get(side_key) or []
            if not side_rows:
                continue
            cleaned = _drop_author_self_name(side_regex, side_rows)
            cleaned = [r for r in cleaned
                       if (r.get("word") or "").lower()
                       not in _LITERARY_PROPN_BLACKLIST]
            cleaned, _ = filter_surnames(cleaned)
            raw[side_key] = cleaned
            if nested is not None:
                nested["top_unique"] = cleaned

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
                "signature words ни у одной из двух сторон при текущем "
                "threshold». ВАЖНО: НЕ интерпретируй это как «авторы "
                "разные» / «авторы похожи» / «структурная метрика "
                "показывает…» — это НЕ метрика стилистической дистанции, "
                "это empty-result артефакт фильтра. Скажи user'у "
                "буквально: «при min_corpus_count={mcc} оба top-списка "
                "пустые; попробуй явный compare с пониженным threshold» "
                "и предложи concrete alternative tool: «спроси "
                "“affinity_by_author Poe min_corpus_count=200” чтобы "
                "посмотреть signature words по одному автору». "
            ).format(mcc=min_corpus_count)
            existing = raw.get("_render_note", "")
            raw["_render_note"] = (
                (existing + " | " if existing else "")
                + f"⚠ КРИТИЧНО: у {empty_labels} top_unique=пустой массив "
                f"(0 signature words в индексе с "
                f"min_corpus_count={min_corpus_count}). "
                + present_text
                + "ЗАПРЕЩЕНО изобретать слова чтобы заполнить пустую "
                "сторону — это галлюцинация. ЗАПРЕЩЕНО рационализировать "
                "пустоту через cosine_similarity или другие метрики — "
                "это empty-result, не метрика. Q15 6-round persistent bug."
            )

    # Sprint 20+ B2 — stamp metric_explanations so renderer doesn't
    # invent direction. Stan Round 11: «чем выше delta, тем сильнее
    # влияние» — НЕВЕРНО, distance metric, lower = closer.
    # E15 — removed phantom `burrows_delta` (v1 compare_authors never
    # returns it; separate tool). Explain only metrics that are
    # actually in the output.
    if isinstance(raw, dict):
        raw.setdefault("metric_explanations", []).extend([
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
        # W-3 (2026-05-24) — expose a flat per-author `entities` row list
        # so the LLM render payload has the same shape the typed-view
        # template uses. Without this, the LLM was inferring a panel
        # from `cosine_similarity` scalar + `shared_high_affinity` list
        # + `top_unique_a/b` and frequently rendered «Cosine
        # Similarity» / «Shared High Affinity» columns with «—» on
        # every row (E38 prod symptom). With the row list present, the
        # «Сравнение» table reads each row's `cosine_similarity` and
        # `shared_high_affinity` directly.
        cosine_val = raw.get("cosine_similarity")
        shared_n_for_rows = len(raw.get("shared_high_affinity") or [])
        author1_name = author1_regex.lstrip("^").rstrip(",").strip()
        author2_name = author2_regex.lstrip("^").rstrip(",").strip()
        entities_rows: list[dict] = []
        for label, name, words_key, regex in (
                ("author1", author1_name, "top_unique_a", author1_regex),
                ("author2", author2_name, "top_unique_b", author2_regex)):
            words = raw.get(words_key) or []
            signature = [
                (w.get("word") if isinstance(w, dict) else str(w))
                for w in words[:30]
            ]
            entities_rows.append({
                "name": name,
                "regex": regex,
                "side": label,
                "cosine_similarity": cosine_val,
                "shared_high_affinity": shared_n_for_rows,
                "signature_words_count": len(signature),
                "signature_words": signature,
            })
        raw["entities"] = entities_rows
    # Phase 2 — v1 compare_authors does NOT expose flat books_a/b
    # (V1CompareAuthors contract). Books are buried in the inner
    # affinity_by_author calls and not re-exported. Use -1 (unknown).
    result = ToolResult.success(
        tool="compare_authors", data=raw,
        coverage=Coverage(books_matched=-1, books_total=-1),
        warnings=warnings, query=query,
    )

    # v5 Phase 2 — emit COMPARISON_PANEL view. Closes B-R14-3 structurally:
    # when both sides empty, view carries explicit empty_state with the
    # min_corpus_count_used reason, eliminating the renderer's chance to
    # fabricate signature words.
    try:
        from scripts.v2 import view_builders as vb
        from scripts.v2.view_types import DataValidity, EmptyReason

        mcc_used = (raw.get("min_corpus_count_used") if isinstance(raw, dict)
                    else None) or min_corpus_count

        if not isinstance(raw, dict):
            # Defensive — v1 returned non-dict; skip view emission.
            return result

        if len(empty_sides) == 2:
            view = vb.build_comparison_panel(
                entities=[], metrics=[],
                empty_reason=EmptyReason.FILTERED_OUT,
                empty_message_ru=(
                    f"Сравнение не построено: ни у «{author1_regex}», "
                    f"ни у «{author2_regex}» нет фирменных слов при "
                    f"min_corpus_count={mcc_used}."
                ),
                empty_message_en=(
                    f"Comparison empty: neither author yielded signature "
                    f"words at min_corpus_count={mcc_used}."
                ),
                empty_filters_applied={"min_corpus_count": mcc_used,
                                        "min_corpus_count_requested": min_corpus_count},
                empty_suggestion=(
                    "Снизь min_corpus_count до 50 для авторов с малым корпусом, "
                    "или используй affinity_by_author по одному автору."
                ),
                language="ru",
            )
            vb.attach_view(result, view,
                           data_validity=DataValidity.EMPTY_UNEXPECTED)
            return result

        # Build entities — include only the non-empty sides. The empty
        # side is mentioned in caveats; renderer template knows to skip
        # its signature_words block.
        # E15 — burrows_delta was a phantom field; v1 compare_authors never
        # returned it (separate `burrows_delta` tool computes it). Show only
        # cosine_similarity which v1 actually returns.
        # E38 (2026-05-22) — Stan prod: «Cosine Similarity» + «Shared High
        # Affinity» columns в entity table показывали «—» для обеих сторон
        # потому что (a) entity stored под «Cosine similarity» (lowercase
        # 's'), а template lookup'ил «Cosine Similarity» (title case из
        # metric_explanations.metric → `.title()`); (b) entity вообще не
        # имел ключа «Shared High Affinity». Названия теперь exact-match
        # title-case + добавлен shared count.
        cosine_val = raw.get("cosine_similarity")
        shared_n = len(raw.get("shared_high_affinity") or [])
        per_entity_metrics = {
            "Cosine Similarity": cosine_val,
            "Shared High Affinity": shared_n,
        }
        entities = []
        if raw.get("top_unique_a"):
            entities.append({
                "name": author1_regex.lstrip("^").rstrip(",").strip(),
                "metrics": dict(per_entity_metrics),
                "signature_words": [
                    w.get("word") if isinstance(w, dict) else str(w)
                    for w in (raw.get("top_unique_a") or [])[:30]
                ],
            })
        if raw.get("top_unique_b"):
            entities.append({
                "name": author2_regex.lstrip("^").rstrip(",").strip(),
                "metrics": dict(per_entity_metrics),
                "signature_words": [
                    w.get("word") if isinstance(w, dict) else str(w)
                    for w in (raw.get("top_unique_b") or [])[:30]
                ],
            })

        # Translate metric_explanations into the view's metrics schema
        metrics = []
        for me in (raw.get("metric_explanations") or []):
            metrics.append({
                "name": me.get("metric", "—").replace("_", " ").title(),
                "direction": me.get("direction", ""),
                "scale": me.get("scale", ""),
                "interpret": me.get("interpret", ""),
            })

        shared = []
        for s in (raw.get("shared_high_affinity") or [])[:30]:
            shared.append(s.get("word") if isinstance(s, dict) else str(s))

        view_caveats: list[str] = []
        if raw.get("_threshold_auto_lowered"):
            view_caveats.append(
                f"Порог автоматически снижен до "
                f"min_corpus_count={mcc_used} (запрошено {min_corpus_count}) — "
                f"иначе обе стороны были бы пустые."
            )
        if len(empty_sides) == 1:
            empty_lbl = empty_sides[0][0]
            view_caveats.append(
                f"Сторона «{empty_lbl}» пустая — нет фирменных слов "
                f"при min_corpus_count={mcc_used}. Renderer показывает "
                f"только непустую сторону."
            )
        if raw.get("cosine_is_structural_zero"):
            view_caveats.append(
                "cosine_similarity < 0.05 — это структурное свойство "
                "метрики (top-N фирменных слов почти не пересекаются), "
                "не показатель «совершенно разные»."
            )

        view = vb.build_comparison_panel(
            entities=entities,
            metrics=metrics,
            shared_signatures=shared,
            headline=f"Сравнение: {author1_regex} vs {author2_regex}",
            caveats=view_caveats,
            provenance=vb.make_provenance(
                requested={"min_corpus_count": min_corpus_count, "top": top},
                returned={"shared_n": len(shared)},
                filtered={"min_corpus_count_used": mcc_used},
                sources=["SPGC-2018-07-18"],
            ),
            language="ru",
        )
        validity = (DataValidity.PARTIAL if empty_sides else DataValidity.OK)
        vb.attach_view(result, view, data_validity=validity)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        log.exception("compare_authors view emission failed")

    return result
