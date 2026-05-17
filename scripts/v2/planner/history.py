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
    r"of (this|these|those|that)|"
    r"more (examples|of these))\b",
    re.IGNORECASE,
)


def _looks_like_followup(text: str) -> bool:
    return bool(_REF_TRIGGERS.search(text))


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
    saw."""
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        if not content:
            continue
        # Grab tokens inside **bold**, `code`, "quotes", or bracketed [ALL CAPS]
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


def infer_followup_intent(text: str) -> str | None:
    """If `text` looks like a follow-up with implicit intent, return it.
    Otherwise None — caller falls back to the regular intent classifier."""
    if not _looks_like_followup(text):
        return None
    for pat, intent in _FOLLOWUP_INTENT_RULES:
        if pat.search(text):
            return intent
    return None


def merge_with_history(current: Entities, history: list[dict] | None,
                       text: str) -> Entities:
    """Return a possibly-enriched Entities — backfill missing fields from
    prior turns iff the current text contains a follow-up trigger.

    Conservative: we don't override fields the user explicitly named in
    the current turn. We only fill what's still None."""
    if not history or not _looks_like_followup(text):
        return current
    prior = _scan_history_for_entities(history)
    if prior is None:
        return current
    backfilled = replace(current)
    if backfilled.author_regex is None and prior.author_regex:
        backfilled.author_regex = prior.author_regex
        backfilled.author_label = prior.author_label
    if backfilled.book_id is None and prior.book_id:
        backfilled.book_id = prior.book_id
    if backfilled.book_title is None and prior.book_title:
        backfilled.book_title = prior.book_title
    if backfilled.word is None and prior.word:
        backfilled.word = prior.word
    if backfilled.year_from is None and prior.year_from:
        backfilled.year_from = prior.year_from
    if backfilled.year_to is None and prior.year_to:
        backfilled.year_to = prior.year_to
    if backfilled.country is None and prior.country:
        backfilled.country = prior.country
    return backfilled
