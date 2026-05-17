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
    # Russian classics (English translations live in the SPGC corpus).
    # Stem-forms cover Russian cases: "Достоевского/Достоевскому/Достоевским"
    # all match "достоевск" prefix.
    "достоевск":        "^Dostoyevsky,",
    "достоевский":      "^Dostoyevsky,",
    "толсто":           "^Tolstoy,",
    "толстой":          "^Tolstoy,",
    "чехов":            "^Chekhov,",
    "тургенев":         "^Turgenev,",
    "пушкин":           "^Pushkin,",
    "гогол":            "^Gogol,",
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
    "свифт":            "^Swift,",
    "свифта":           "^Swift,",   # stem-form for Russian genitive Свифта
    "джонатан свифт":   "^Swift,",
    "swift":            "^Swift,",
    "теккерей":         "^Thackeray,",
    "теккерея":         "^Thackeray,",
    "thackeray":        "^Thackeray,",
    "моррис":           "^Morris,",
    "морриса":          "^Morris,",
    "morris":           "^Morris,",
    "уильям моррис":    "^Morris,",
    "стивенсон":        "^Stevenson,",  # (already had via stevenson, add cyrillic)
    "конрад":           "^Conrad,",
    "конрада":          "^Conrad,",
    "уэллс":            "^Wells,",
    "h. g. wells":      "^Wells,",
    "уайлд":            "^Wilde,",
    "уайлда":           "^Wilde,",
    "морриса":          "^Morris,",
    "льюис кэрролл":    "^Carroll,",
    "кэрролл":          "^Carroll,",
    "carroll":          "^Carroll,",
    "шелли":            "^Shelley,",
    "shelley":          "^Shelley,",
    "льюис":            "^Lewis,",   # M.G. Lewis for «The Monk»
    "lewis":            "^Lewis,",
    "брёнте":           "^Bront",     # Brontë sisters — without trailing ,
    "бронте":           "^Bront",
    "bronte":           "^Bront",
    "brontë":           "^Bront",
}


KNOWN_BOOKS: dict[str, tuple[str, str]] = {
    # title_query_normalized -> (pg_id_or_empty, canonical_title)
    # "PG"  on its own == "not in SPGC, but a real book the user named". The
    #       planner uses canonical_title to talk back to the user instead of
    #       hallucinating a chain. Empty PG id = book_title set, book_id None.

    # Russian classics (English translations in SPGC)
    "преступление и наказание":   ("PG2554", "Crime and Punishment"),
    "crime and punishment":       ("PG2554", "Crime and Punishment"),
    "war and peace":              ("PG2600", "War and Peace"),
    "война и мир":                ("PG2600", "War and Peace"),

    # English / American canon
    "pride and prejudice":        ("PG1342", "Pride and Prejudice"),
    "гордость и предубеждение":   ("PG1342", "Pride and Prejudice"),
    "dracula":                    ("PG345",  "Dracula"),
    "дракула":                    ("PG345",  "Dracula"),
    "alice's adventures in wonderland": ("PG11", "Alice's Adventures in Wonderland"),
    "alice in wonderland":        ("PG11",  "Alice's Adventures in Wonderland"),
    "frankenstein":               ("PG84",   "Frankenstein"),
    "treasure island":            ("PG120",  "Treasure Island"),
    "the hound of the baskervilles": ("PG2852", "The Hound of the Baskervilles"),
    "hound of the baskervilles":  ("PG2852", "The Hound of the Baskervilles"),
    "jane eyre":                  ("PG1260", "Jane Eyre"),
    "the picture of dorian gray": ("PG174",  "The Picture of Dorian Gray"),
    "picture of dorian gray":     ("PG174",  "The Picture of Dorian Gray"),
    "the raven":                  ("PG1065", "The Raven"),
    "emma":                       ("PG158",  "Emma"),
    "david copperfield":          ("PG766",  "David Copperfield"),
    "bleak house":                ("PG1023", "Bleak House"),
    "the adventures of sherlock holmes": ("PG1661", "The Adventures of Sherlock Holmes"),
    "adventures of sherlock holmes": ("PG1661", "The Adventures of Sherlock Holmes"),
    "lord jim":                   ("PG5658", "Lord Jim"),
    "the monk":                   ("PG601",  "The Monk"),
    "huckleberry finn":           ("PG76",   "Adventures of Huckleberry Finn"),
    "the adventures of huckleberry finn": ("PG76", "Adventures of Huckleberry Finn"),
    "adventures of huckleberry finn": ("PG76", "Adventures of Huckleberry Finn"),

    # Late-public-domain additions (verified via find_book on the server)
    "at the mountains of madness": ("PG70652", "At the Mountains of Madness"),
    "the call of cthulhu":        ("PG68283", "The Call of Cthulhu"),
    "the murder of roger ackroyd": ("PG69087", "The Murder of Roger Ackroyd"),
    "the forsyte saga":           ("PG4397",  "The Forsyte Saga"),
    "the well at the world's end": ("PG169",  "The Well at the World's End"),
    "the house of the wolfings":  ("PG2885",  "The House of the Wolfings"),
    "heart of darkness":          ("PG219",   "Heart of Darkness"),
    "moby dick":                  ("PG2701",  "Moby Dick"),
    "moby-dick":                  ("PG2701",  "Moby Dick"),   # alias for dashed form
    "wuthering heights":          ("PG768",   "Wuthering Heights"),

    # Mentioned but absent from SPGC-2018 (copyright)
    "1984":                       ("",      "Nineteen Eighty-Four"),
    "nineteen eighty-four":       ("",      "Nineteen Eighty-Four"),
    "the lord of the rings":      ("",      "The Lord of the Rings"),
    "the hobbit":                 ("",      "The Hobbit"),
    "the old man and the sea":    ("",      "The Old Man and the Sea"),
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

# Titles can appear inside any of these quote pairs. We match each pair
# explicitly so that inside-title characters (Alice'S apostrophe U+2019)
# don't close the outer span:
#   «...»  guillemets
#   "..."  ASCII straight double
#   "..."  Curly double (U+201C / U+201D — Stan's vault style)
#   '...'  Curly single
_BOOK_QUOTED = re.compile(
    "«([^»]{1,80})»"
    "|\"([^\"]{1,80})\""
    "|“([^”]{1,80})”"
    "|‘([^’]{1,80})’"
)
_BOOK_PG_ID = re.compile(r"\b(PG\d{1,7}|U\d{1,7})\b", re.IGNORECASE)


def _find_book(text: str) -> tuple[str | None, str | None]:
    """Return (pg_id, title_query) or (None, None).

    Resolution order:
      1. Explicit PG/U id (PG1342, U7).
      2. Quoted title → check KNOWN_BOOKS dict, else pass raw to find_book.
         A quoted single short word like "fog" or "ajar" is *not* a title —
         that's a target word (Q2/Q9 «слово "fog"»). Skip those unless
         they're in KNOWN_BOOKS, in which case the dict wins.
      3. Unquoted substring match against KNOWN_BOOKS — handles
         "Crime and Punishment used more often" without quotes.
    """
    m = _BOOK_PG_ID.search(text)
    if m:
        return m.group(1).upper(), None
    for tm in _BOOK_QUOTED.finditer(text):
        # Each pair contributes its own capture group; pick the non-None one.
        title = next((g for g in tm.groups() if g), "").strip()
        if not title:
            continue
        # Normalize curly apostrophes to ASCII so the KNOWN_BOOKS dict lookup
        # works regardless of which quote style the user pasted in.
        key = title.lower().replace("’", "'").replace("‘", "'")
        if key in KNOWN_BOOKS:
            pg, _ = KNOWN_BOOKS[key]
            if pg.startswith("PG") and pg != "PG":
                return pg, title
            return None, title
        # Single short token (no spaces, < 12 chars) is almost certainly a
        # word, not a book title. Defer to _find_word; book_title stays None.
        if " " not in title and len(title) < 12:
            continue
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

# Quoted single words like "fog", «ajar», 'blue', "fog" (curly).
# Use explicit pairs so an internal apostrophe (U+2019 in Alice'S) doesn't
# accidentally close the span.
_WORD_QUOTED = re.compile(
    "«([a-zA-Zа-яёА-ЯЁ-]{2,30})»"
    "|\"([a-zA-Zа-яёА-ЯЁ-]{2,30})\""
    "|“([a-zA-Zа-яёА-ЯЁ-]{2,30})”"
    "|‘([a-zA-Zа-яёА-ЯЁ-]{2,30})’"
    "|'([a-zA-Zа-яёА-ЯЁ-]{2,30})'"
)
# Match only "слово X" (singular), not "слова X" (plural — usually a question
# phrasing like "слова чаще всего..."). For an unquoted bare word after the
# keyword we still require it to dodge the stopword set so a question phrasing
# like "слова чаще встречаются" doesn't get bucketed as e.word="чаще".
_WORD_AFTER_KEY = re.compile(
    r"\bслово\b\s+[\"'«“]?([a-zA-Zа-яё-]{2,30})[\"'»”]?",
    re.IGNORECASE,
)
# «имени Анна / имя Anna / по имени X» — Stan asks for usage examples of a
# personal name in literature. We capture the bare proper noun after the
# keyword and pass it through to word_contexts / hybrid_search; the search
# layer's `_maybe_translate` handles Russian→English on the way to ChromaDB.
_NAME_AFTER_KEY = re.compile(
    r"\b(?:им(?:я|ени|енем))\s+[\"'«“]?([A-ZА-ЯЁ][a-zA-Zа-яё-]{1,29})[\"'»”]?",
    re.IGNORECASE,
)

# Common Russian/English fillers that the legacy regex used to mis-match.
_WORD_STOPWORDS = {
    # Russian frequency / comparison adverbs
    "чаще", "реже", "всего", "обычно", "часто", "редко", "больше", "меньше",
    "много", "мало", "несколько", "разных", "примерно", "вдруг", "почти",
    # Russian pronouns / fillers
    "что", "это", "тот", "та", "те", "наш", "ваш", "ваши", "наши",
    # English fillers occasionally caught by the same regex
    "the", "and", "but", "with", "from", "than", "more", "less",
}


def _find_word(text: str) -> str | None:
    m = _WORD_QUOTED.search(text)
    if m:
        # Pick the first non-None capture group across all quote-pair alternatives.
        word = next((g for g in m.groups() if g), None)
        if word:
            return word.lower()
    m = _WORD_AFTER_KEY.search(text)
    if m:
        w = m.group(1).lower()
        if (len(w) >= 2 and not w.istitle()
                and w not in _WORD_STOPWORDS):
            return w
    # «имени X / имя X» — name probe. The original-case requirement in the
    # regex (capital first letter) sidesteps Russian/English fillers like
    # «имя автора» — we want a proper noun. Result is lowercased before
    # returning so it threads cleanly through `word_contexts`/`hybrid_search`.
    m = _NAME_AFTER_KEY.search(text)
    if m:
        w = m.group(1).lower()
        if w not in _WORD_STOPWORDS:
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
    """First-match wins, but pick the *most specific* family when the user
    listed several (e.g. 'latin/french' or 'germanic and norse'). v1
    find_words_by_etymology accepts only one family per call, so we pick
    the first canonical bucket that appears.

    The intro suggested «latin/french» — that used to extract family=
    'latin/french' as a single string and break v1. Now we pick 'latin'
    (or whichever comes first in the text) and the planner runs one
    family. Multi-family parallel chains can come later if Stan finds
    them useful."""
    s = text.lower()
    best_pos = len(s) + 1
    best_family = None
    for k, v in ETYMOLOGY_FAMILIES.items():
        pos = s.find(k)
        if 0 <= pos < best_pos:
            best_pos = pos
            best_family = v
    return best_family


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

    word = _find_word(text)
    # When the same token is also part of a resolved book title, drop it as
    # a "word" — it was just the first noun of the title that the quoted-
    # word regex hijacked. Q19 «"Alice's Adventures in Wonderland"» used to
    # yield word="alice"; we want None so the planner picks a default.
    if word and book_title and word.lower() in book_title.lower():
        word = None

    return Entities(
        author_regex=primary_author[1],
        author_label=primary_author[0],
        book_id=book_id,
        book_title=book_title,
        word=word,
        year_from=year_from,
        year_to=year_to,
        country=_find_country(text),
        level=_find_level(text),
        emotion=_find_emotion(text),
        etymology_family=_find_etymology(text),
        pos_filter=_find_pos(text),
        top_n=_find_top_n(text),
        multi_author_regex=multi,
        raw_misc={"raw_text": text},
    )
