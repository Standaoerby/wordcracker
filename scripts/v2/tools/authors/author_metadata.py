"""v2 author_metadata — quick stats for a single author.

Delegates to v1 rag_tools.author_metadata; wraps in ToolResult."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult


# Stan rounds 1-5: Poe «1809-1964» persisted 5 test rounds. The v2.5 fix
# (drop year_of_death_max if span > 120) doesn't fire on prod for reasons
# we can't reliably diagnose remotely. This is the «belt-and-braces»
# layer: hardcoded biographical overrides for popular authors whose
# Gutendex metadata is unreliable. Source: Wikipedia.
_AUTHOR_BIO_OVERRIDES: dict[str, dict] = {
    # «^Surname,» regex key → corrections
    "^Poe,":          {"year_of_birth_min": 1809, "year_of_death_max": 1849},
    "^Lovecraft,":    {"year_of_birth_min": 1890, "year_of_death_max": 1937},
    "^Pushkin,":      {"year_of_birth_min": 1799, "year_of_death_max": 1837},
    "^Tolstoy,":      {"year_of_birth_min": 1828, "year_of_death_max": 1910},
    "^Dostoyevsky,":  {"year_of_birth_min": 1821, "year_of_death_max": 1881},
    "^Chekhov,":      {"year_of_birth_min": 1860, "year_of_death_max": 1904},
    "^Turgenev,":     {"year_of_birth_min": 1818, "year_of_death_max": 1883},
    "^Gogol,":        {"year_of_birth_min": 1809, "year_of_death_max": 1852},
    "^Lermontov,":    {"year_of_birth_min": 1814, "year_of_death_max": 1841},
    "^Doyle,":        {"year_of_birth_min": 1859, "year_of_death_max": 1930},
    "^Wodehouse,":    {"year_of_birth_min": 1881, "year_of_death_max": 1975},
    "^Dickens,":      {"year_of_birth_min": 1812, "year_of_death_max": 1870},
    "^Austen,":       {"year_of_birth_min": 1775, "year_of_death_max": 1817},
    "^Twain,":        {"year_of_birth_min": 1835, "year_of_death_max": 1910},
    "^Wilde,":        {"year_of_birth_min": 1854, "year_of_death_max": 1900},
    "^Melville,":     {"year_of_birth_min": 1819, "year_of_death_max": 1891},
    "^Conrad,":       {"year_of_birth_min": 1857, "year_of_death_max": 1924},
    "^Stoker,":       {"year_of_birth_min": 1847, "year_of_death_max": 1912},
    "^Stevenson,":    {"year_of_birth_min": 1850, "year_of_death_max": 1894},
    "^Shakespeare,":  {"year_of_birth_min": 1564, "year_of_death_max": 1616},
    "^Shelley,":      {"year_of_birth_min": 1797, "year_of_death_max": 1851},
    "^Swift,":        {"year_of_birth_min": 1667, "year_of_death_max": 1745},
    "^Morris,":       {"year_of_birth_min": 1834, "year_of_death_max": 1896},
    "^Thackeray,":    {"year_of_birth_min": 1811, "year_of_death_max": 1863},
    "^Carroll,":      {"year_of_birth_min": 1832, "year_of_death_max": 1898},
    "^Galsworthy,":   {"year_of_birth_min": 1867, "year_of_death_max": 1933},
    "^Christie,":     {"year_of_birth_min": 1890, "year_of_death_max": 1976},
}


@tool(
    name="author_metadata",
    category="authors",
    description=(
        "Быстрая мета по автору: годы жизни, язык, количество книг, total downloads, "
        "примеры названий. Используй для «когда родился X», «сколько у X книг»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "author_regex": {"type": "string",
                             "description": "Regex по колонке author, обычно '^Surname,', e.g. '^Doyle,'"},
        },
        "required": ["author_regex"],
    },
    requires=["author"],
    cost="cheap",
    cacheable=True,
)
def author_metadata(author_regex: str) -> ToolResult:
    if not author_regex or not author_regex.strip():
        return ToolResult.fail(
            tool="author_metadata", err_type="invalid_args",
            message="author_regex is required",
        )

    try:
        from scripts.rag_tools import author_metadata as _v1
    except ImportError as e:
        return ToolResult.fail(
            tool="author_metadata", err_type="internal",
            message=f"v1 rag_tools unavailable: {e}",
        )

    raw = _v1(author_regex)
    query = {"author_regex": author_regex}

    if isinstance(raw, dict) and raw.get("error"):
        return ToolResult.fail(
            tool="author_metadata",
            err_type="not_found" if "no books" in raw.get("error", "") else "internal",
            message=str(raw["error"]),
            details={k: v for k, v in raw.items() if k != "error"},
            query=query,
        )

    book_count = raw.get("books_total") or raw.get("book_count") or len(raw.get("sample_titles", []))

    # Q12 from Stan's 2026-05-18 demon round: Poe came back as «1809–1964».
    # 1964 isn't a death year; Gutenberg metadata sometimes confuses
    # `authoryearofdeath` with the publication year of a specific edition.
    # Filter implausible spans (>120 yrs span = wrong) so the LLM doesn't
    # render fiction as life dates.
    #
    # Stan round 2 follow-up: the v2.5 version had `isinstance(yob, int)`
    # which silently failed because v1 returns numpy.int64 (NOT a real
    # int via isinstance). Coerce explicitly before the comparison so the
    # filter actually fires.
    if isinstance(raw, dict):
        yob_raw = raw.get("year_of_birth_min")
        yod_raw = raw.get("year_of_death_max")
        yob = int(yob_raw) if yob_raw is not None and yob_raw is not False else None
        yod = int(yod_raw) if yod_raw is not None and yod_raw is not False else None
        if (yob is not None and yod is not None
                and (yod - yob > 120 or yod < yob)):
            raw["year_of_death_max_unreliable"] = yod
            raw["year_of_death_max"] = None
            raw.setdefault("warnings", []).append(
                f"death year {yod} dropped — implausible span "
                f"(birth={yob}); Gutenberg metadata likely confused "
                f"author death with edition publication year"
            )

        # Stan rounds 1-5: «По 1809-1964» persisted 5 rounds. v2.5/v2.6
        # span-filter fix didn't fire on prod for unclear reasons.
        # Belt-and-braces: hardcoded override for known authors.
        # Wikipedia-sourced; trumps any Gutendex CSV bug.
        override = _AUTHOR_BIO_OVERRIDES.get(author_regex)
        if override:
            for k, v in override.items():
                raw[k] = v
            raw.pop("year_of_death_max_unreliable", None)
            raw["_bio_source"] = "wordcracker hardcoded (Wikipedia)"
        # Hint for the LLM render so it doesn't call this «годы жизни»
        # when only birth is reliable, and doesn't conflate the corpus
        # publication window with biographical dates.
        raw["_render_note"] = (
            "Поля year_of_birth_min / year_of_death_max — биографические "
            "(из Gutendex metadata, могут быть неточными). НЕ называй "
            "это «диапазон книг в корпусе» — это годы жизни. Если "
            "year_of_death_max_unreliable выставлен, скажи что год смерти "
            "не подтверждён и сошлись только на год рождения."
        )

    return ToolResult.success(
        tool="author_metadata", data=raw,
        coverage=Coverage(books_matched=int(book_count or 0), books_total=-1),
        query=query,
    )
