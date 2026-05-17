"""Entity extractor — pulls structured entities out of the user query.

Contract: docs/v2/PLANNER.md §3.

Mostly rules/dictionaries for v2-alpha. LLM fallback is gated on
WC_PLANNER_LLM_FALLBACK=on — useful when we know the canonical author isn't
in our alias dict yet but the question still wants per-author analysis.

Returns an `Entities` instance; downstream `plan.py` decides what to do with
missing fields (ask for clarification, default scope, etc).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


# Canonical author aliases. Keys are normalized (lowercased, no commas). Values
# are the `^Surname,` regex used by the v1 metadata layer. Expand as needed —
# this list covers all 40 example queries plus the popular cases from chat.
AUTHOR_ALIASES: dict[str, str] = {
    # Russian classics (English translations live in the SPGC corpus)
    "достоевский":      "^Dostoyevsky,",
    "толстой":          "^Tolstoy,",
    "чехов":            "^Chekhov,",
    "тургенев":         "^Turgenev,",
    "пушкин":           "^Pushkin,",
    "гоголь":           "^Gogol,",

    # English / American
    "конан дойл":       "^Doyle,",
    "конан дойль":      "^Doyle,",
    "дойл":             "^Doyle,",
    "doyle":            "^Doyle,",
    "wodehouse":        "^Wodehouse,",
    "ВУДХАУС":          "^Wodehouse,",
    "вудхаус":          "^Wodehouse,",
    "лавкрафт":         "^Lovecraft,",
    "lovecraft":        "^Lovecraft,",
    "уайльд":           "^Wilde,",
    "уайлд":            "^Wilde,",
    "wilde":            "^Wilde,",
    "толкин":           "^Tolkien,",
    "толкиен":          "^Tolkien,",
    "tolkien":          "^Tolkien,",
    "диккенс":          "^Dickens,",
    "dickens":          "^Dickens,",
    "хемингуэ":         "^Hemingway,",  # stem: Хемингуэй / Хемингуэя / Хемингуэем
    "хемингуэй":        "^Hemingway,",
    "hemingway":        "^Hemingway,",
    "кристи":           "^Christie,",
    "christie":         "^Christie,",
    "по":               "^Poe,",
    "edgar allan poe":  "^Poe,",
    "poe":              "^Poe,",
    "остин":            "^Austen,",
    "остен":            "^Austen,",
    "austen":           "^Austen,",
    "роулинг":          "^Rowling,",
    "rowling":          "^Rowling,",
    "оруэлл":           "^Orwell,",
    "orwell":           "^Orwell,",
    "голсуорси":        "^Galsworthy,",
    "galsworthy":       "^Galsworthy,",
    "мелвилл":          "^Melville,",
    "melville":         "^Melville,",
    "конрад":           "^Conrad,",
    "conrad":           "^Conrad,",
    "стивенсон":        "^Stevenson,",
    "stevenson":        "^Stevenson,",
    "брэдбери":         "^Bradbury,",
    "твен":             "^Twain,",
    "twain":            "^Twain,",
    "стокер":           "^Stoker,",
    "stoker":           "^Stoker,",
}


KNOWN_BOOKS: dict[str, tuple[str, str]] = {
    # title_query_normalized -> (pg_id, canonical_title)
    "1984":                       ("PG", "Nineteen Eighty-Four"),  # not in SPGC (copyright)
    "преступление и наказание":   ("PG2554", "Crime and Punishment"),
    "crime and punishment":       ("PG2554", "Crime and Punishment"),
    "pride and prejudice":        ("PG1342", "Pride and Prejudice"),
    "гордость и предубеждение":   ("PG1342", "Pride and Prejudice"),
    "dracula":                    ("PG345",  "Dracula"),
    "дракула":                    ("PG345",  "Dracula"),
    "war and peace":              ("PG2600", "War and Peace"),
    "война и мир":                ("PG2600", "War and Peace"),
}


COUNTRY_ALIASES: dict[str, str] = {
    "британск": "GB", "british": "GB", "англия": "GB", "english": "GB", "uk": "GB", "брит": "GB",
    "американск": "US", "american": "US", "американ": "US", "usa": "US", "ame": "US", "amer": "US",
    "русск": "RU", "russian": "RU", "russia": "RU",
    "французск": "FR", "french": "FR", "франция": "FR",
    "немецк": "DE", "german": "DE", "germany": "DE",
}


EMOTIONS = {
    "страх": "fear", "fear": "fear",
    "тревога": "fear", "тревожн": "fear",
    "ужас": "fear", "terror": "fear", "madness": "fear",
    "гнев": "anger", "anger": "anger",
    "радость": "joy", "joy": "joy",
    "грусть": "sadness", "sadness": "sadness",
    "любовь": "love",
    "trust": "trust", "доверие": "trust",
}


ETYMOLOGY_FAMILIES = {
    "германск":          "germanic",
    "древнегерманск":    "germanic",
    "скандинавск":       "norse",
    "norse":             "norse",
    "norse origin":      "norse",
    "germanic":          "germanic",
    "romance":           "romance",
    "романск":           "romance",
    "латинск":           "latin",
    "latin":             "latin",
    "french":            "french",
    "французск":         "french",
    "greek":             "greek",
    "греческ":           "greek",
    "celtic":            "celtic",
    "кельтск":           "celtic",
}


@dataclass
class Entities:
    author_regex: str | None = None
    author_label: str | None = None    # human-readable name as user typed
    book_id: str | None = None         # resolved canonical PG/U id
    book_title: str | None = None      # raw title query for find_book if needed
    word: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    country: str | None = None
    level: Literal["basic", "intermediate", "advanced", "rare"] | None = None
    emotion: str | None = None
    etymology_family: str | None = None
    pos_filter: list[str] | None = None
    top_n: int | None = None
    multi_author_regex: list[str] = field(default_factory=list)  # «Мелвилла, Конрада и Стивенсона»
    raw_misc: dict = field(default_factory=dict)


# ---------- author extraction ----------

# Last word matches are tricky in Russian (Конан Дойла → Дойл/Дойла/Дойлу/Дойле).
# Strategy: lower-case + alias dict on substrings. Multi-word names (Конан Дойл)
# come before single-word ones (Дойл) in the dict iteration to win the match.
_AUTHOR_KEYS_SORTED: list[str] | None = None


def _author_keys() -> list[str]:
    global _AUTHOR_KEYS_SORTED
    if _AUTHOR_KEYS_SORTED is None:
        _AUTHOR_KEYS_SORTED = sorted(AUTHOR_ALIASES, key=len, reverse=True)
    return _AUTHOR_KEYS_SORTED


def _find_authors(text: str) -> list[tuple[str, str]]:
    """Return [(label, regex)] for every alias found.

    Two safety measures:
      1. Word boundary check — 'по' must not match inside 'повторяются'. We
         require the alias to be flanked by non-word chars (or string edges).
         For Cyrillic stems we also accept the alias with any Russian case
         ending (Дойл → Дойла/Дойлу/Дойле/Дойлом) via trailing-letter tolerance.
      2. Length-desc iteration — 'конан дойл' is tried before 'дойл' so the
         multi-word alias wins on overlapping slices.
    """
    s = text.lower()
    n = len(s)
    # First pass: collect every (start, end, key) hit. Length-desc iteration
    # ensures multi-word aliases beat their substrings.
    raw_hits: list[tuple[int, int, str]] = []

    def _is_left_boundary(i: int) -> bool:
        return i == 0 or not s[i - 1].isalpha()

    def _is_right_boundary(j: int, key: str) -> bool:
        if " " in key:
            return j >= n or not s[j].isalpha()
        if len(key) <= 2:
            return j >= n or not s[j].isalpha()
        k = 0
        while k < 5 and j + k < n and s[j + k].isalpha():
            k += 1
        return k < 5

    consumed: list[tuple[int, int]] = []
    for key in _author_keys():
        k = key.lower()
        start = 0
        while True:
            i = s.find(k, start)
            if i < 0:
                break
            j = i + len(k)
            if (_is_left_boundary(i) and _is_right_boundary(j, k)
                    and not any(cs <= i < ce or cs < j <= ce for cs, ce in consumed)):
                raw_hits.append((i, j, key))
                consumed.append((i, j))
            start = j

    # Order by appearance in the text — first mentioned wins as primary author.
    raw_hits.sort(key=lambda h: h[0])
    # Dedupe by regex (a single author may have multiple aliases, e.g. "хемингуэ"
    # and "хемингуэй" both matching one occurrence — but that's prevented by
    # the consumed-span check above, so this is just a safety net).
    seen_regex: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, _, key in raw_hits:
        rgx = AUTHOR_ALIASES[key]
        if rgx in seen_regex:
            continue
        seen_regex.add(rgx)
        out.append((key, rgx))
    return out


# ---------- book / title ----------

# Match titles in «...», "...", "..." (curly), and bare «1984», «Dracula»-ish.
_BOOK_QUOTED = re.compile(r"[«\"]([^«»\"]{1,60})[»\"]")
_BOOK_PG_ID = re.compile(r"\b(PG\d{1,7}|U\d{1,7})\b", re.IGNORECASE)


def _find_book(text: str) -> tuple[str | None, str | None]:
    """Return (pg_id, title_query) or (None, None).

    Resolution order:
      1. Explicit PG/U id (PG1342, U7).
      2. Quoted title → check KNOWN_BOOKS dict, else pass raw to find_book.
      3. Unquoted substring match against KNOWN_BOOKS — handles "Уровень
         сложности Pride and Prejudice" without quotes.
    """
    m = _BOOK_PG_ID.search(text)
    if m:
        return m.group(1).upper(), None
    for tm in _BOOK_QUOTED.finditer(text):
        title = tm.group(1).strip()
        key = title.lower()
        if key in KNOWN_BOOKS:
            pg, _ = KNOWN_BOOKS[key]
            if pg.startswith("PG") and pg != "PG":
                return pg, title
            return None, title
        return None, title
    # Unquoted known-title match — sorted longest-first so "Crime and Punishment"
    # wins over "Crime".
    low = text.lower()
    for key in sorted(KNOWN_BOOKS, key=len, reverse=True):
        if key in low:
            pg, canon = KNOWN_BOOKS[key]
            if pg.startswith("PG") and pg != "PG":
                return pg, canon
            return None, canon
    return None, None


# ---------- word ----------

# Quoted single words like "fog", «ajar», 'blue'. Or "слово X" patterns.
_WORD_QUOTED = re.compile(r"[\"'«“]([a-zA-Zа-яёА-ЯЁ-]{2,30})[\"'»”]")
_WORD_AFTER_KEY = re.compile(
    r"\b(слов(?:а|ом|у|е|ах)?|слово|word)\b\s+[\"'«“]?([a-zA-Zа-яё-]{2,30})[\"'»”]?",
    re.IGNORECASE,
)


def _find_word(text: str) -> str | None:
    m = _WORD_QUOTED.search(text)
    if m:
        return m.group(1).lower()
    m = _WORD_AFTER_KEY.search(text)
    if m:
        # filter out obvious non-words (avoid matching "из «Dracula»" → "dracula" as word)
        w = m.group(2).lower()
        if len(w) >= 2 and not w.istitle():
            return w
    return None


# ---------- year range ----------

_YEAR = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_AFTER_YEAR = re.compile(r"\b(после|after)\s+(1[5-9]\d{2}|20\d{2})\b", re.IGNORECASE)
_BEFORE_YEAR = re.compile(r"\b(до|before)\s+(1[5-9]\d{2}|20\d{2})\b", re.IGNORECASE)
_YEAR_RANGE = re.compile(
    r"\b(1[5-9]\d{2}|20\d{2})\s*[–—\-]\s*(1[5-9]\d{2}|20\d{2})\b"
)
_VICTORIAN = re.compile(r"виктори[аяь]нск\w*", re.IGNORECASE)


def _find_year_range(text: str) -> tuple[int | None, int | None]:
    m = _YEAR_RANGE.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return min(a, b), max(a, b)
    m = _AFTER_YEAR.search(text)
    if m:
        return int(m.group(2)) + 1, None
    m = _BEFORE_YEAR.search(text)
    if m:
        return None, int(m.group(2)) - 1
    if _VICTORIAN.search(text):
        return 1837, 1901
    return None, None


# ---------- country ----------

def _find_country(text: str) -> str | None:
    s = text.lower()
    for k, v in COUNTRY_ALIASES.items():
        if k in s:
            return v
    return None


# ---------- level (CEFR) ----------

_LEVEL_RE = re.compile(r"\b(a1|a2|b1|b2|c1|c2)\b", re.IGNORECASE)


def _find_level(text: str) -> str | None:
    m = _LEVEL_RE.search(text)
    if m:
        code = m.group(1).upper()
        if code in ("A1", "A2"):
            return "basic"
        if code in ("B1", "B2"):
            return "intermediate"
        if code in ("C1", "C2"):
            return "advanced"
    s = text.lower()
    if "basic" in s or "базов" in s or "начальн" in s:
        return "basic"
    if "intermediate" in s or "средн" in s:
        return "intermediate"
    if "advanced" in s or "продвин" in s or "advanced" in s or "сложн" in s:
        return "advanced"
    if "rare" in s or "редк" in s:
        return "rare"
    return None


# ---------- emotion / etymology ----------

def _find_emotion(text: str) -> str | None:
    s = text.lower()
    for k, v in EMOTIONS.items():
        if k in s:
            return v
    return None


def _find_etymology(text: str) -> str | None:
    s = text.lower()
    for k, v in ETYMOLOGY_FAMILIES.items():
        if k in s:
            return v
    return None


# ---------- POS filter ----------

POS_KEYWORDS = {
    "прилагательн": "ADJ", "adjective": "ADJ", "adj": "ADJ",
    "глагол":       "VERB", "verb": "VERB",
    "существительн": "NOUN", "noun": "NOUN", "сущ": "NOUN",
    "наречи":       "ADV", "adverb": "ADV",
    "имена собственн": "PROPN", "proper noun": "PROPN",
}


def _find_pos(text: str) -> list[str] | None:
    s = text.lower()
    found = sorted({tag for k, tag in POS_KEYWORDS.items() if k in s})
    return found or None


# ---------- top_n ----------

_TOPN_RE = re.compile(
    r"\b(?:топ[\s-]?(\d{1,4})|top[\s-]?(\d{1,4})|"
    r"(?:первые|первых|first)\s+(\d{1,4})|"
    r"(\d{1,4})\s+(?:слов|word|примеров|examples?))\b",
    re.IGNORECASE,
)


def _find_top_n(text: str) -> int | None:
    m = _TOPN_RE.search(text)
    if not m:
        return None
    for g in m.groups():
        if g:
            try:
                n = int(g)
                return min(max(n, 1), 1000)
            except ValueError:
                continue
    return None


# ---------- main API ----------

def extract(text: str) -> Entities:
    """Pull every entity from the query. Missing fields stay None."""
    if not text or not text.strip():
        return Entities()

    author_hits = _find_authors(text)
    primary_author = author_hits[0] if author_hits else (None, None)
    multi = [r for _, r in author_hits[1:]] if len(author_hits) > 1 else []

    book_id, book_title = _find_book(text)
    year_from, year_to = _find_year_range(text)

    return Entities(
        author_regex=primary_author[1],
        author_label=primary_author[0],
        book_id=book_id,
        book_title=book_title,
        word=_find_word(text),
        year_from=year_from,
        year_to=year_to,
        country=_find_country(text),
        level=_find_level(text),
        emotion=_find_emotion(text),
        etymology_family=_find_etymology(text),
        pos_filter=_find_pos(text),
        top_n=_find_top_n(text),
        multi_author_regex=multi,
    )
