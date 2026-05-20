"""History-aware entity backfill + intent inference for follow-up turns.

When the user says «приведи три примера такого использования» or «у этого
автора есть ещё?», we need to remember:
  * the last resolved book_id / book_title
  * the last resolved author_regex
  * the last list of words returned (so «эти слова» / «такого использования»
    can scope to them)

Without history the planner returns clarify on every follow-up. With it,
the second turn inherits the relevant entity from the prior assistant
message and routes a real tool call.

We don't do full coreference resolution — just a lightweight backfill
guided by trigger phrases.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from scripts.v2.planner.entities import (
    AUTHOR_ALIASES, KNOWN_BOOKS, Entities, extract,
)

# Trigger phrases that signal "fill from history".
_REF_TRIGGERS = re.compile(
    r"\b(так(ого|их|ое|ая|ой|ому|им|ими)|"
    r"эти(х|м|ми)?|это|этой|этого|этих|"
    r"предыдущ\w*|"
    r"еще|ещё|"
    r"приведи (примеры|три|пример)|"
    # «отсортируй / переразложи / в другом виде / по убыванию» — re-rank.
    r"отсортируй|сортируй|пересортируй|перегруппируй|перестрой|"
    r"в другом (виде|формате|порядке)|"
    r"по убывани\w+|по возрастани\w+|"
    r"sort (them|by)|re-?rank|"
    r"of (this|these|those|that)|"
    r"more (examples|of these)|"
    # Sprint 19+ — «покажи все / полный список / все книги серии /
    # show all / list all» — expansion follow-ups. Prior intent gets
    # re-run with bumped top_n to widen the result set. Stan caught
    # this 2026-05-19: after «у тебя есть Harry Potter» returned 5
    # of 7, «покажи все книги серии» dropped to clarify.
    r"покажи\s+все\b|"
    r"полный\s+список|"
    r"все\s+(книги?|произведен\w+|works)|"
    r"список\s+(всех|всё)|"
    r"show\s+(all|me\s+all)|"
    r"list\s+all|"
    r"give\s+me\s+the\s+full)\b",
    re.IGNORECASE,
)

# Stan round 5 critical finding: «теперь у Диккенса» / «а у X» / «давай
# теперь Толстого» — explicit context-swap follow-up that the main
# `\b...\b` alternation pattern can't catch because trailing `\b`
# requires word boundary RIGHT AFTER the trigger, but the new author
# name follows immediately. Separate regex without trailing-anchor.
_CONTEXT_SWAP_TRIGGERS = re.compile(
    r"\b(теперь|сейчас|давай(\s+теперь)?|"
    r"а\s+(у|во?|про|по)\s+\w|"
    r"how\s+about|switch\s+to|change\s+to|"
    r"now\s+(with|for|let'?s))",
    re.IGNORECASE,
)

# Sprint 17 Round 8 P0: detect explicit author-name positions in a
# context-swap query. «теперь у Марло» / «а у Уэбстера» / «давай теперь
# Толстого» — capture the capitalized token after the preposition. If
# this token doesn't resolve via AUTHOR_ALIASES, merge_with_history must
# NOT silently restore the prior author — that's user-deceptive (Round
# 8 C3-2 returned Shakespeare answers for a Marlowe query).
# Critical: NOT re.IGNORECASE — we rely on the leading capital letter
# as the «proper noun candidate» signal.
_EXPLICIT_AUTHOR_AFTER_SWAP = re.compile(
    r"\b(?:[Тт]еперь|[Сс]ейчас|[Дд]авай(?:\s+теперь)?|а)\s+"
    r"(?:у\s+|с\s+|про\s+|по\s+|для\s+|со\s+|об?\s+)?"
    r"([A-ZА-ЯЁ][a-zA-Zа-яё-]{2,29})"
)

# Re-rank / re-format follow-ups: same data, different ordering. Re-run the
# previous intent so the LLM gets the same tool result back from cache and
# can render it sorted. Caught separately from «more examples» / «расскажи
# подробнее» which legitimately fire word_contexts.
_RERANK_PATTERNS = re.compile(
    r"отсортируй|сортируй|пересортируй|перегруппируй|перестрой|"
    r"в другом (виде|формате|порядке)|"
    r"по убывани\w+|по возрастани\w+|"
    r"sort (them|by)|re-?rank",
    re.IGNORECASE,
)


# Sprint 20 — result-modifier follow-up: «убери из них имена собственные»
# / «без proper nouns» / «отфильтруй фамилии» / «exclude surnames».
# These are *modifiers* over a previous turn's output. We re-classify the
# prior user message to inherit its intent, AND set a hint on the entity
# so the plan-builder can dial up the filter aggressiveness (higher
# min_corpus_count, force exclude_proper_nouns).
_PROPN_REMOVAL_PATTERNS = re.compile(
    r"(?:убери|без|выкини|выкинь|выкинуть|отфильтру\w+|исключи|"
    r"drop|exclude|filter\s+out|remove)\s+"
    r"(?:из\s+них\s+|оттуда\s+|from\s+(?:them|the\s+list)\s+)?"
    # «имена / имён / именам / именах / имена авторов / фамилии /
    # фамилий / proper nouns / surnames / character names».
    # `им[её]н\w*` covers both «имен-» (имена/именах) and «имён»
    # (genitive plural with ё).
    r"(?:им[её]н\w*\s+собственн\w+|фамил\w+|"
    r"proper\s+nouns?|surnames?|character\s+names?|"
    r"им[её]н\w*\s+(?:авторов|персонаж\w+))",
    re.IGNORECASE,
)


def _is_propn_removal(text: str) -> bool:
    return bool(_PROPN_REMOVAL_PATTERNS.search(text))


# Sprint 20 — translation-modifier follow-up. Stan 2026-05-19:
#   «Возьми слова, которые ты мне выдал и переведи на русский»
#   «Сделай перевод на русский всех слов»
# After an author_vocab / book_vocab / learning turn, user wants the
# words translated. Re-running with `learning_words` instead of
# `affinity_by_author` gives words + per-word RU translation in one
# pass (learning_words internally calls enrich_word with target_lang=ru).
_TRANSLATE_PATTERNS = re.compile(
    r"(?:переведи|переводи|"
    # Sprint 20 — allow 0-3 filler words between verb and «перевод»:
    # «дай их в переводе», «сделай мне перевод», «покажи всех слов
    # перевод».
    r"(?:сделай|дай|покажи)(?:\s+\w+){0,3}\s+перевод\w*|"
    r"в\s+переводе\s+(?:на\s+)?русск\w+|"
    r"translate(?:\s+(?:these|them|the\s+words?|the\s+list))?|"
    r"with\s+(?:russian|ru)\s+translations?)"
    # Optional follow-on words («N слов», «этих слов», «на русский»)
    r"(?:\s+\w+){0,4}?"
    r"(?:\s+(?:на\s+русск\w+|to\s+russian|to\s+ru\b|на\s+ru\b))?",
    re.IGNORECASE,
)
# Standalone «возьми слова которые ты мне выдал» — strong referring
# phrase that signals "use prior assistant output". When combined with
# a translate-pattern elsewhere in the same query, this triggers the
# translation followup.
_PRIOR_OUTPUT_REF = re.compile(
    r"(?:возьми|take)\s+(?:слова|words?|эти|the\s+(?:words?|list))|"
    r"которые\s+ты\s+(?:мне\s+)?(?:выдал|показал|дал|вернул)|"
    r"which\s+you\s+(?:gave|returned|showed)|"
    # «этих слов / эти слова / тех слов» — Russian demonstrative
    # references to the prior list.
    r"\b(?:эт(?:их|и|ими)|т(?:ех|ем|еми))\s+слов|"
    # Sprint 20 — bare «их» as accusative pronoun referring to a
    # prior list, paired with a translation verb or preposition that
    # signals «do something with them». Examples:
    #   «дай их в переводе», «переведи их», «их перевод на русский».
    # Conservative: requires the verb/preposition context to avoid
    # matching generic «у них» / «о них».
    r"\b(?:дай|сделай|покажи|переведи)(?:\s+\w+){0,2}\s+их\b|"
    r"\bих\s+перевод\w*",
    re.IGNORECASE,
)


def _is_translate_followup(text: str) -> bool:
    """True if the text asks to translate prior output to Russian.

    Requires either an explicit "на русский / to russian" target OR
    the prior-output reference pattern combined with any translate
    verb. Both signals together: high precision, low recall on
    edge phrasings.
    """
    has_translate = bool(_TRANSLATE_PATTERNS.search(text))
    if not has_translate:
        return False
    s = text.lower()
    has_target = ("на русск" in s or "to russian" in s
                  or "to ru" in s or "на ru" in s)
    has_prior_ref = bool(_PRIOR_OUTPUT_REF.search(text))
    return has_target or has_prior_ref

# Sprint 19+ — expansion patterns: «покажи все», «полный список»,
# «все книги серии», «show all», «list all», «give me the full
# list». User wants the prior intent re-run with a wider top_n
# limit. Distinct from rerank (same data, different order) and from
# context-swap («теперь у X», new entity).
_EXPAND_PATTERNS = re.compile(
    r"покажи\s+(все|всё|всех)\b|"
    r"полный\s+список|"
    r"все\s+(книги?|произведен\w+|works)|"
    r"список\s+(всех|всё)|"
    r"show\s+(all|me\s+all)|"
    r"list\s+all|"
    r"give\s+me\s+the\s+full",
    re.IGNORECASE,
)


def _is_export_followup(text: str) -> bool:
    """True if the text asks to export prior output to a known format.

    Stan Round 11 B3 — «выгрузи в anki», «csv pls», «save as markdown».
    Combined with a prior word-list assistant turn, this routes to the
    export_word_list intent whose plan formats without new tool calls.
    """
    if not text:
        return False
    # «выгрузи / export / save / dump / convert» + format token.
    if re.search(
        r"\b(выгрузи|выгрузить|выгрузь|сохрани|конвертируй|дай(те)?|"
        r"export|save|convert|dump|format)\b"
        r".{0,40}\b(anki|csv|json|markdown|\.md\b|tsv|"
        r"excel|spreadsheet|таблиц|obsidian|обсидиан|notion)",
        text, re.IGNORECASE,
    ):
        return True
    # Bare «csv pls» / «anki» when very short.
    if re.match(
        r"^\s*(в\s+|to\s+|as\s+|in\s+)?"
        r"(anki|csv|json|markdown|tsv)\b"
        r"(\s+(pls|please|пж|пжл))?\s*[?.!]?\s*$",
        text, re.IGNORECASE,
    ):
        return True
    return False


def _looks_like_followup(text: str) -> bool:
    return bool(_REF_TRIGGERS.search(text) or _CONTEXT_SWAP_TRIGGERS.search(text)
                or _PROPN_REMOVAL_PATTERNS.search(text)
                or _is_translate_followup(text)
                or _is_export_followup(text))


def _is_context_swap(text: str) -> bool:
    """True iff this is an author/book swap follow-up («теперь у X»,
    «а у X»). Used to inherit prior intent while overriding entities
    with what the new turn explicitly named."""
    return bool(_CONTEXT_SWAP_TRIGGERS.search(text))


def is_expansion_followup(text: str) -> bool:
    """Sprint 19+ — «покажи все / полный список / show all»: user wants
    the prior intent re-run with a wider top_n. Caller (plan builder
    or rag_v2 pre-pass) reads this signal and bumps e.top_n
    accordingly before re-running the prior plan."""
    return bool(_EXPAND_PATTERNS.search(text))


def _scan_history_for_entities(history: list[dict]) -> Entities | None:
    """Re-extract entities from the most recent user message that had any.

    Walks history from newest to oldest; returns the first message whose
    extracted Entities has at least one resolved field (author, book, word).
    Skips assistant messages — only user phrasings count, since assistant
    answers are usually narrative prose."""
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        if not content.strip():
            continue
        e = extract(content)
        if (e.author_regex or e.book_id or e.book_title or e.word
                or e.year_from or e.year_to):
            return e
    return None


def _last_word_list_from_assistant(history: list[dict]) -> list[str]:
    """Pull a likely word list from the latest assistant turn.

    Looks for the most recent assistant message and extracts capitalized or
    quoted/bracketed tokens that look like vocabulary entries. Crude but
    enough for «приведи три примера» to use the same N words the user just
    saw.

    Sprint 20 — extended to parse markdown tables (column 1) so
    translate-followup can chain enrich_word over the prior list. Word-
    list-shaped tables look like:

        | Слово    | count_X | count_Y | affinity |
        |----------|---------|---------|----------|
        | tuppence | 613     | 1230    | 5230.24  |
        | priors   | 8       | 1463    | 57.39    |
    """
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        if not content:
            continue

        # Sprint 20 — markdown table column 1. Catches «Word | count | …»
        # patterns. Skip header row (text like «Слово», «Word», etc).
        table_words = _extract_table_column1(content)
        if len(table_words) >= 3:
            return table_words

        # Grab tokens inside **bold**, `code`, [ALL CAPS], or in
        # comma-separated lowercase lists ("wicket, blighter, hullo").
        candidates = re.findall(
            r"\*\*([a-zA-Z-]{3,30})\*\*"
            r"|`([a-zA-Z-]{3,30})`"
            r"|\[([A-Z][A-Z-]{2,29})\]",
            content,
        )
        words = []
        for tup in candidates:
            for g in tup:
                if g:
                    words.append(g.lower())
        # Plain comma-separated list (assistant's compact summary form).
        # Only consider lines that look like a list: ≥3 comma-separated
        # tokens, each 3-20 lowercase letters.
        for line in content.splitlines():
            tokens = [t.strip().lower() for t in line.split(",")]
            simple = [t for t in tokens
                      if 3 <= len(t) <= 20 and re.fullmatch(r"[a-zа-я-]+", t)]
            if len(simple) >= 3:
                words.extend(simple)
        # Dedupe while preserving order, cap at 30
        seen = set()
        out = []
        for w in words:
            if w in seen:
                continue
            seen.add(w)
            out.append(w)
            if len(out) >= 30:
                break
        if out:
            return out
    return []


_TABLE_ROW_RE = re.compile(r"^\s*\|(.+?)\|", re.MULTILINE)
_TABLE_HEADER_CELLS = frozenset({
    # Skip the header row (column titles, not data)
    "слово", "word", "лексема", "lemma",
    "слово (англ.)", "слово (англ)", "english word", "english",
    "название", "title", "term", "термин", "название (англ.)",
    # Separator-like cells: «-----», «:---:», etc — handled by regex too
})


def _extract_table_column1(content: str) -> list[str]:
    """Sprint 20 — Stan 2026-05-19: parse markdown table column 1 from
    a rendered assistant message. Used by translate-followup to grab
    the same words the user just saw and chain enrich_word over them.

    Returns a list of cleaned word tokens (lowercase, ≤30 chars, alpha
    only, deduped, capped at 50). Skips header row + separator row.
    Returns [] if no table or fewer than 3 data rows found.
    """
    if "|" not in content:
        return []
    words: list[str] = []
    seen: set[str] = set()
    for m in _TABLE_ROW_RE.finditer(content):
        # First pipe-separated cell value. Markdown tables look like:
        # «| cell1 | cell2 | … |» — the regex captures up to but not
        # including the second pipe.
        cell = m.group(1).strip()
        # Drop separator rows like `:---:` / `---`
        if not cell or set(cell) <= set("-: "):
            continue
        # Drop header-like cells
        if cell.lower() in _TABLE_HEADER_CELLS:
            continue
        # Drop numbered-row indices «1», «12.» — these are decorative
        if re.fullmatch(r"\d+\.?", cell):
            continue
        # Strip leading «**», `` ` ``, etc.
        token = re.sub(r"[\*`\[\]]+", "", cell).strip()
        # Keep alpha-only tokens 2-30 chars (English vocab words)
        if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,29}", token):
            continue
        token_lc = token.lower()
        if token_lc in seen:
            continue
        seen.add(token_lc)
        words.append(token_lc)
        if len(words) >= 50:
            break
    return words


# Map follow-up phrasing → inferred intent. The user is responding to a
# prior assistant message and the literal trigger usually maps cleanly:
#   «приведи примеры использования» → word_contexts
#   «расскажи подробнее про эти слова» → word_contexts
#   «ещё/еще N слов» → re-run author_vocab/learning with bigger top
#   «more examples» → word_contexts
_FOLLOWUP_INTENT_RULES = [
    (re.compile(r"приведи (примеры|три пример|пример)|"
                r"more examples?|"
                r"\bдай примеры?\b|"
                r"в контексте", re.IGNORECASE), "word_contexts"),
    (re.compile(r"расскажи (подробнее|больше)|"
                r"что значит\b|"
                r"что означа\w+", re.IGNORECASE), "word_contexts"),
    (re.compile(r"ещё (\d+|больше)|еще (\d+|больше)|"
                r"give me (more|another)", re.IGNORECASE), "author_vocab"),
]


def infer_followup_intent(text: str,
                          history: list[dict] | None = None) -> str | None:
    """If `text` looks like a follow-up with implicit intent, return it.
    Otherwise None — caller falls back to the regular intent classifier.

    When the follow-up is a re-rank / re-format request («отсортируй»,
    «по убыванию», «sort by»), look back at the last user message and
    re-classify it; we want to re-run the same plan so the LLM gets the
    same data back and can re-render it sorted. Without this, «отсортируй
    их по количеству упоминаний» after a fresh `affinity_by_author` table
    used to clarify-out.
    """
    if not _looks_like_followup(text):
        return None
    if _RERANK_PATTERNS.search(text) and history:
        # Re-classify the most recent user message that had a non-clarify
        # intent — that's the data we want re-rendered.
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label not in ("clarify", ""):
                return prior_intent.label
    # Sprint 20 — proper-noun removal modifier («убери имена собственные»).
    # Same shape as re-rank: inherit the prior intent so the plan re-runs,
    # but `merge_with_history` will set `_propn_strict=True` on the entity
    # so the plan-builder can dial filter aggressiveness up.
    if _PROPN_REMOVAL_PATTERNS.search(text) and history:
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label not in ("clarify", ""):
                return prior_intent.label
    # Sprint 20 — translation modifier («переведи на русский слова»).
    # Stan 2026-05-19 prod retrospective: previously we routed this to
    # `learning` intent so learning_words would combine band-pass +
    # per-word enrich_word translation. Two problems with that approach:
    #
    #   (a) learning_words returns a DIFFERENT word list (CEFR-banded,
    #       not affinity-ranked). User asks «переведи те 96 слов» —
    #       gets a fresh list of 30 different words. Mismatch.
    #   (b) translating the SAME 96 words would mean re-running prior
    #       affinity_by_author (cache hit, instant) + 96 enrich_word
    #       calls × 1.5s each (Wiktionary) ≈ 150s — way past chat
    #       timeout.
    #
    # Honest fix: when translate-followup detected AND prior turn was
    # a word-list intent, return "translate_word_list" — a NEW intent
    # whose plan-builder surfaces a smart clarify with actionable advice
    # («перечисли явно слова которые хочешь перевести: tuppence,
    # stitching, embroidery — 5-10 слов влезут в chat timeout»).
    # No fake results, no different word list.
    #
    # Long-term: v4 LLM-planner sees the full conversation, can extract
    # words from the prior assistant message, and emit a DAG with
    # parallel enrich_word steps. v3 rules-path lacks that mechanism.
    if _is_translate_followup(text) and history:
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label in ("author_vocab", "book_vocab",
                                        "learning", "word_etymology",
                                        "author_top_words"):
                return "translate_word_list"
            if prior_intent.label not in ("clarify", ""):
                # Prior wasn't a word-list intent (e.g. readability,
                # compare_authors). Keep the prior intent — the
                # renderer will see _translate_to=ru and can add
                # translations to whatever text it generates.
                return prior_intent.label
    # Sprint 20+ B3 — export-followup. «выгрузи в anki/csv/markdown/json»
    # over the prior word-list. Same shape as translate_word_list: when
    # the prior turn was a word-list intent, return export_word_list and
    # let merge_with_history extract prior words below.
    if _is_export_followup(text) and history:
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label in ("author_vocab", "book_vocab",
                                        "learning", "word_etymology",
                                        "author_top_words", "topic_words",
                                        "translate_word_list"):
                return "export_word_list"
            if prior_intent.label not in ("clarify", ""):
                # Prior wasn't a word-list intent → fall back to standalone
                # export_word_list which will surface an honest clarify
                # asking for the explicit word list.
                return "export_word_list"
        # No prior user turn at all — still route to export_word_list;
        # plan builder will surface clarify.
        return "export_word_list"
    # Sprint 19+ — expansion follow-up («покажи все», «полный список»).
    # Same re-classify-prior logic as rerank — same plan, but the
    # plan-builder will see e.top_n bumped (set in merge_with_history).
    if _EXPAND_PATTERNS.search(text) and history:
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label not in ("clarify", ""):
                return prior_intent.label
    # Stan round 5: «теперь у Диккенса» / «а у пушкина?» — context-swap
    # follow-ups. The current text has the NEW entity (Диккенс/пушкин)
    # but no explicit intent, just a referring particle. Inherit intent
    # from the prior user message so the new entity is queried with the
    # same operation. Without this, every «теперь у X» falls to clarify.
    if _is_context_swap(text) and history:
        from scripts.v2.planner.intent import classify as _classify
        for msg in reversed(history):
            if msg.get("role") != "user":
                continue
            prior_intent = _classify(msg.get("content") or "")
            if prior_intent.label not in ("clarify", ""):
                return prior_intent.label
    for pat, intent in _FOLLOWUP_INTENT_RULES:
        if pat.search(text):
            return intent
    return None


def merge_with_history(current: Entities, history: list[dict] | None,
                       text: str) -> Entities:
    """Return a possibly-enriched Entities — backfill missing fields from
    prior turns iff the current text contains a follow-up trigger.

    Conservative: we don't override fields the user explicitly named in
    the current turn. We only fill what's still None.

    Sprint 17 Round 8 P0 safety: when the current turn is a context-swap
    («теперь у X») AND the user clearly named an author but it didn't
    resolve via AUTHOR_ALIASES, REFUSE to backfill the author from
    prior. That's the difference between «implicit follow-up, reuse
    prior context» (good) and «user explicitly switched to X, we don't
    know X, silently pretend they meant the previous author» (bad —
    surfaces as user-deceptive wrong answers, Round 8 C3-2 returned
    Shakespeare for «теперь у Марло»). The unresolved name is parked
    in raw_misc for the planner / clarify renderer to surface."""
    if not history or not _looks_like_followup(text):
        return current
    prior = _scan_history_for_entities(history)
    if prior is None:
        return current
    backfilled = replace(current)

    # Detect user-named-but-unresolved author. If we see that pattern,
    # block the author backfill — the user explicitly switched, we just
    # didn't recognize them, and using prior would lie about the source.
    block_author_backfill = False
    if current.author_regex is None and _is_context_swap(text):
        m = _EXPLICIT_AUTHOR_AFTER_SWAP.search(text)
        if m:
            named = m.group(1)
            # Verify it really isn't a known alias (regex caught the
            # surname but maybe AUTHOR_ALIASES has it under a different
            # form — re-extract on the bare name to be sure).
            verify = extract(f"у {named}")
            if verify.author_regex is None:
                block_author_backfill = True
                backfilled.raw_misc = {
                    **(backfilled.raw_misc or {}),
                    "unresolved_author_named": named,
                }

    if (not block_author_backfill
            and backfilled.author_regex is None and prior.author_regex):
        backfilled.author_regex = prior.author_regex
        backfilled.author_label = prior.author_label
    if backfilled.book_id is None and prior.book_id:
        backfilled.book_id = prior.book_id
    if backfilled.book_title is None and prior.book_title:
        backfilled.book_title = prior.book_title
    if backfilled.word is None and prior.word:
        backfilled.word = prior.word
    # If still no word — look at the last assistant message for a likely
    # vocabulary entry («the first one» / «эти слова» style follow-ups).
    if backfilled.word is None:
        words = _last_word_list_from_assistant(history)
        if words:
            backfilled.word = words[0]
    if backfilled.year_from is None and prior.year_from:
        backfilled.year_from = prior.year_from
    if backfilled.year_to is None and prior.year_to:
        backfilled.year_to = prior.year_to
    if backfilled.country is None and prior.country:
        backfilled.country = prior.country
    # Sprint 19+ — expansion follow-up bumps top_n so the re-run plan
    # gets a wider result set. «покажи все» after «у тебя есть HP»
    # (top=5 → 7 matches existed) now re-runs with top=30 to surface
    # the full set.
    if is_expansion_followup(text) and (backfilled.top_n is None
                                          or backfilled.top_n < 20):
        backfilled.top_n = 30
    # Sprint 20 — propn-removal modifier: stamp a hint so the plan
    # builder can crank up filter aggressiveness on the re-run.
    # _propn_strict is a soft flag; plan templates that respect it
    # (e.g. _plan_author_vocab) raise min_corpus_count and force
    # exclude_proper_nouns.
    if _is_propn_removal(text):
        rm = dict(backfilled.raw_misc or {})
        rm["_propn_strict"] = True
        backfilled.raw_misc = rm
    # Sprint 20 — translate modifier: stamp _translate_to=ru AND
    # extract the prior assistant's word list (typically a markdown
    # table column 1) so the plan-builder can chain enrich_word steps
    # over those exact words. v3 finally gets prior-result hand-off for
    # this specific use case — without waiting on v4 LLM planner.
    if _is_translate_followup(text):
        rm = dict(backfilled.raw_misc or {})
        rm["_translate_to"] = "ru"
        prior_words = _last_word_list_from_assistant(history)
        if prior_words:
            # Cap at 10 — enrich_word is ~1.5s per Wiktionary call;
            # 10 × 1.5s = 15s, fits comfortably under chat timeout
            # alongside resolver + render + critic.
            rm["_prior_words"] = prior_words[:10]
            rm["_prior_words_total"] = len(prior_words)
        backfilled.raw_misc = rm
    # Sprint 20+ B3 — export-followup: extract prior word list from the
    # last assistant markdown table so _plan_export_word_list can format
    # without re-running the original tool. Larger cap (50) than the
    # translate path because formatting is local — no Wiktionary RT.
    if _is_export_followup(text):
        rm = dict(backfilled.raw_misc or {})
        prior_words = _last_word_list_from_assistant(history)
        if prior_words:
            rm["_prior_words"] = prior_words[:50]
            rm["_prior_words_total"] = len(prior_words)
        backfilled.raw_misc = rm
    return backfilled
