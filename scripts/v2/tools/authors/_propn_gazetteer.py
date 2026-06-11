"""Proper-NAME detector + filter layers (R-27 WP3, 2026-06-11).

Stan live repro 2026-06-10 (prod 2.7.3, Q7/Q8): «лексика Льва Толстого» +
followup «убери русские имена и фамилии» rendered «Запрошено 30 слов, но
после фильтрации имён собственных и редких токенов осталось 24» — while
the visible table carried hélène, sergius, petrovna, hippolyte, nicholas.
The claim was technically legal (rule 21: data disclosed word_dict drops)
but the filter itself was leaky: «правда, что фильтровали — ложь, что
отфильтровали».

Why the existing stack misses lowercase given names:

  1. spaCy POS/NER on ISOLATED lowercased tokens systematically mistags
     given names («hélène», «sergius» → NOUN, not PROPN).
  2. v1 word_dict `proper_noun` flag is populated only for words seen in
     prior enrich_word runs — cold names pass.
  3. v1 `_book_propn_set` NER samples 400k chars of raw text and feeds a
     lowercased cache — the case signal is destroyed before filtering.
  4. Curated blocklists (`_surname_filter`, `_LITERARY_PROPN_BLACKLIST`)
     cover SURNAMES; given names / patronymics were never curated, and
     none of the lists fold accents («hélène» ≠ «helene», «iván» ≠ «ivan»).

This module adds the two missing layers + the shared category detector:

  * `is_proper_name_token` / `find_proper_names` — PURE deterministic
    detector (no corpus, no LLM): accent-folded gazetteer of given names
    from the corpus classics, Russian patronymic / feminine-surname
    suffixes, curated character surnames. The SAME function defines the
    `clean` semantics on tool results and powers the critic's
    «заявлено vs показано» check, so the filter and the verifier cannot
    drift apart silently.
  * `filter_proper_names` — row filter with API parity with the other
    `_*_filter.py` modules.
  * `capitalization_stats` / `filter_by_cap_ratio` — corpus signal: SPGC
    tokens files preserve the original case, so a token that is
    predominantly Capitalized in the texts is a name even when the
    counts pipeline only ever saw it lowercased. Gracefully degrades to
    a no-op when tokens files are absent (local dev / CI have no corpus).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

from scripts.v2.tools.authors._surname_filter import (
    _CURATED_CHARACTER_SURNAMES,
)

# ---------------------------------------------------------------------
# Accent folding — hélène→helene, pávlovna→pavlovna, iván→ivan, márya→marya.
# Every list below is stored FOLDED + lowercase; every probe is folded
# before lookup so accented forms cannot bypass an ascii gazetteer.
# ---------------------------------------------------------------------


def fold_accents(token: str) -> str:
    """Strip combining diacritics: NFD-decompose, drop Mn marks."""
    if not token:
        return ""
    decomposed = unicodedata.normalize("NFD", token)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------------
# Gazetteer of given names from the corpus classics (translit variants
# included). Lowercase + accent-folded. Extend pragmatically — same
# policy as _CURATED_CHARACTER_SURNAMES: the cost of over-blocking is one
# less signature/learning word, the cost of under-blocking is the Q7
# leak class. Deliberately EXCLUDED: given names that are common English
# lexemes (grace, mark, rose, violet, frank, bill, daisy, may…) — those
# would over-filter real vocabulary.
# ---------------------------------------------------------------------
_GIVEN_NAME_GAZETTEER: frozenset[str] = frozenset({
    # Tolstoy — War and Peace / Anna Karenina / Resurrection (translit
    # variants as they appear in the 19th-c. English translations).
    "natasha", "pierre", "andrei", "andrey", "andrew",
    "nikolai", "nikolay", "nikolenka", "nicholas",
    "sonya", "helene", "anatole", "boris", "petya", "vera",
    "liza", "lise", "marya", "mary", "anna", "kitty", "dolly",
    "stiva", "betsy", "missy", "seryozha", "sergei", "sergey", "sergius",
    "alexei", "alexey", "aleksey", "vasili", "vasily", "vassily",
    "hippolyte", "ippolit", "julie", "berg",  # Adolf Berg — W&P surname,
    # listed here because it leaks as a bare token; «iceberg»/«bergamot»
    # are different exact tokens and are NOT affected (exact match only).
    "denisova", "karenin", "nekhlyudov", "maslova", "katusha",
    # Dostoevsky / Gogol / Turgenev / Chekhov given names
    "ivan", "vanya", "dmitri", "dmitry", "mitya", "grushenka",
    "katerina", "ekaterina", "katya", "avdotya", "dunya",
    "pavel", "arkady", "yevgeny", "evgeny", "mikhail",
    "taras", "ostap", "akaky", "varenka", "varvara",
    "praskovya", "pelageya", "agafya", "fenya", "fedya", "fedor",
    "theodore",
    # Austen / Brontë / Dickens / Hardy given names that read as names
    # only (no common-noun homograph).
    "emma", "elizabeth", "lydia", "georgiana", "charlotte", "harriet",
    "fanny", "catherine", "marianne", "eleanor", "jane", "bingley",
    "oliver", "agnes", "dora", "nell", "arabella", "eustacia",
})

# Russian patronymics + feminine -skaya surnames, generically. petrovna,
# pavlovna, ivanovich, andreevna, bolkonskaya — all match without being
# curated one-by-one. The suffix set is chosen to have NO common-English
# collisions (unlike -ova: «casanova», or -sky: «whisky», which are
# deliberately not matched).
_PATRONYMIC_RE = re.compile(
    r"^[a-z]{2,}(?:ovna|evna|ichna|ovich|evich|ovitch|evitch|yevich|yevna"
    r"|skaya)$",
)


def is_proper_name_token(token: str) -> bool:
    """PURE deterministic name detector (the shared «детектор категории»).

    True iff the accent-folded lowercase token is in the given-name
    gazetteer, matches a patronymic/-skaya suffix, or is a curated
    literary character surname. No corpus, no PG metadata, no LLM —
    same result on prod, CI and local, so the `clean` flag on tool
    results and the critic's claimed-vs-shown check share one truth.
    """
    if not token:
        return False
    folded = fold_accents(token.strip().lower())
    if not folded or not folded.replace("-", "").replace("'", "").isalpha():
        return False
    if folded in _GIVEN_NAME_GAZETTEER:
        return True
    if _PATRONYMIC_RE.match(folded):
        return True
    return folded in _CURATED_CHARACTER_SURNAMES


def find_proper_names(words: Iterable[str]) -> list[str]:
    """Subset of `words` the detector flags as proper names (order kept)."""
    return [w for w in words if isinstance(w, str) and is_proper_name_token(w)]


def filter_proper_names(rows: list[dict], *,
                        word_key: str = "word") -> tuple[list[dict], int]:
    """Drop rows whose `word_key` value the detector flags as a name.

    Returns (kept_rows, dropped_count) — API parity with
    `filter_surnames` / `filter_toponyms` / `filter_corpus_artifacts`.
    """
    if not rows:
        return rows, 0
    kept = [r for r in rows
            if not is_proper_name_token(r.get(word_key) or "")]
    return kept, len(rows) - len(kept)


# ---------------------------------------------------------------------
# Capitalization-ratio layer — corpus signal for names the curated sets
# don't know. SPGC tokens files keep the original case («Hélène»), so
# counting how often a candidate token appears Capitalized separates
# names (≈1.0 — every occurrence is the name) from common words (≲0.2 —
# only sentence starts). Deterministic: files are sorted and capped.
# ---------------------------------------------------------------------

DEFAULT_CAP_RATIO_THRESHOLD = 0.7
DEFAULT_CAP_MIN_OCCURRENCES = 10
DEFAULT_CAP_MAX_FILES = 8


def capitalization_stats(words: Sequence[str],
                         token_files: Sequence[Path],
                         *, max_files: int = DEFAULT_CAP_MAX_FILES,
                         ) -> dict[str, tuple[int, int]]:
    """Per-word (total, capitalized) occurrence counts across token files.

    Matching is accent-folded + case-insensitive so «hélène» in the row
    aggregates «Hélène» from the text. Unreadable files are skipped —
    callers must treat an all-zero result as «no signal», not «no name».
    """
    stats: dict[str, list[int]] = {w: [0, 0] for w in words if w}
    if not stats:
        return {}
    key_of = {fold_accents(w.lower()): w for w in stats}
    for path in sorted(Path(p) for p in token_files)[:max_files]:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    t = line.strip()
                    if not t:
                        continue
                    w = key_of.get(fold_accents(t.lower()))
                    if w is None:
                        continue
                    entry = stats[w]
                    entry[0] += 1
                    if t[:1].isupper():
                        entry[1] += 1
        except (OSError, UnicodeDecodeError):
            continue
    return {w: (tot, cap) for w, (tot, cap) in stats.items()}


def filter_by_cap_ratio(rows: list[dict],
                        token_files: Sequence[Path],
                        *, word_key: str = "word",
                        threshold: float = DEFAULT_CAP_RATIO_THRESHOLD,
                        min_occurrences: int = DEFAULT_CAP_MIN_OCCURRENCES,
                        max_files: int = DEFAULT_CAP_MAX_FILES,
                        ) -> tuple[list[dict], int, bool]:
    """Drop rows whose token is predominantly Capitalized in the texts.

    Returns (kept_rows, dropped_count, verified) where `verified` is True
    iff at least one token file was actually scanned — False means the
    corpus signal was unavailable (local dev / CI) and the caller must
    NOT claim a complete name filter (`clean` stays False).
    """
    if not rows:
        return rows, 0, False
    existing = [Path(p) for p in token_files if Path(p).exists()]
    if not existing:
        return rows, 0, False
    words = [(r.get(word_key) or "") for r in rows]
    stats = capitalization_stats(words, existing, max_files=max_files)
    kept: list[dict] = []
    dropped = 0
    for r in rows:
        total, cap = stats.get(r.get(word_key) or "", (0, 0))
        if total >= min_occurrences and (cap / total) >= threshold:
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped, True


__all__ = [
    "fold_accents",
    "is_proper_name_token",
    "find_proper_names",
    "filter_proper_names",
    "capitalization_stats",
    "filter_by_cap_ratio",
    "DEFAULT_CAP_RATIO_THRESHOLD",
    "DEFAULT_CAP_MIN_OCCURRENCES",
    "DEFAULT_CAP_MAX_FILES",
]
