"""Single source of truth for language-code normalization (TZ S-T2).

Stdlib-only — safe to import from anywhere (the v1 `rag_tools` query layer,
the `admin_server` upload path, the migration script) WITHOUT dragging in
pandas/numpy. Consolidates what used to be a per-module copy of the ISO map
+ ad-hoc `.split()`/`str.contains("'en'")` parsing (Phase-2 R6).
"""
from __future__ import annotations

# ISO-639-2/B (+ a few /T) → ISO-639-1. Minimal to the languages that occur
# in the SPGC corpus; unknown 3-letter codes are dropped (never guessed) so
# a bad/garbage tag can't masquerade as a real language.
ISO_639_2_TO_1 = {
    "eng": "en", "rus": "ru", "fre": "fr", "fra": "fr", "ger": "de",
    "deu": "de", "spa": "es", "ita": "it", "por": "pt", "dut": "nl",
    "nld": "nl", "lat": "la", "gre": "el", "ell": "el", "swe": "sv",
    "dan": "da", "nor": "no", "fin": "fi", "pol": "pl", "cze": "cs",
    "ces": "cs", "hun": "hu", "ara": "ar", "heb": "he", "chi": "zh",
    "zho": "zh", "jpn": "ja", "kor": "ko", "tgl": "tl",
}


def _to_iso1(token: str) -> str:
    """One token → ISO-639-1, or '' if unknown. Handles regional forms
    ('en-US' / 'en_us' → 'en') and 639-2/B 3-letter codes ('eng' → 'en')."""
    c = token.strip().lower().split("-")[0].split("_")[0]
    if len(c) == 2 and c.isalpha():
        return c
    return ISO_639_2_TO_1.get(c, "")


def _split_raw(raw) -> list[str]:
    """Strip list-repr brackets/quotes and split into raw tokens."""
    s = str(raw).strip().lower()
    if s in ("", "nan", "none"):
        return []
    s = s.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
    return [p for p in s.split(",") if p.strip()]


def lang_codes(raw) -> set[str]:
    """All ISO-639-1 codes in `raw`. Handles every shape the `language`
    column / a lang arg takes: 'en' · "['en']" · "['en', 'fr']" ·
    "['eng']" · '' · 'nan' · garbage. Returns a set so a multi-language
    book matches a query for any of its languages."""
    if not raw:
        return set()
    return {c for c in (_to_iso1(p) for p in _split_raw(raw)) if c}


def normalize_lang(raw) -> str:
    """Primary ISO-639-1 code of `raw`, or '' if unknown/absent."""
    if not raw:
        return ""
    for p in _split_raw(raw):
        code = _to_iso1(p)
        if code:
            return code
    return ""


def normalize_lang_field(raw) -> str:
    """Repair a stored SPGC `language` value to a canonical list-repr,
    order preserved + deduped:

        "['eng']"        -> "['en']"
        "['en', 'fre']"  -> "['en', 'fr']"
        "['en', 'eng']"  -> "['en']"
        "['xx']" / ""    -> ""
    """
    if raw is None:
        return ""
    out: list[str] = []
    for p in _split_raw(raw):
        code = _to_iso1(p)
        if code and code not in out:
            out.append(code)
    if not out:
        return ""
    return "[" + ", ".join(f"'{c}'" for c in out) + "]"
