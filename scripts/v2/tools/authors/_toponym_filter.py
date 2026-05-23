"""Toponym blocklist for affinity / learning_words outputs (W-4).

Stan 2026-05-22 (Phase 3, W-4): «фирменные слова Конан Дойла» вернуло
boer-war топонимы:
    burger (Burgher / Burger — citizens of Transvaal/Orange Free State),
    uitlanders (foreign workers in Boer republics),
    belmont (Battle of Belmont, 1899),
    colesberg (town in Cape Colony),
    kroonstad (town in Orange Free State).

Все они — GPE / LOC в NER-разметке, но в спайси POS-тегировании на
изолированных лоуэркейс-токенах часто проскакивают как NOUN/ADJ. Same
attack vector как у `_surname_filter.py` (PERSON-leakage), только для
геообъектов.

Архитектурно зеркалит surname-фильтр:
    - curated set 19th-c. literary toponyms — cheap, immediate, ships in code.
    - Optional NER-derived enrichment from `*_affinity_ner.csv` cache.
      Если файл доступен — подмешиваем GPE/LOC сурфейсы; иначе fallback на
      curated.

Why not just rely on spaCy NER online?
    Спайси `en_core_web_sm` (быстрый CPU-режим) не имеет надёжного NER
    для коротких токенов вне контекста. Прецизионный NER требует `_trf`
    GPU pipeline (см. `ner_filter_affinity.py`), это батч-офлайн, не для
    request-time.

Conservatism note:
    Топонимы могут совпадать с реальными словами (например,
    «brighton» — британский город + просто прилагательное «bright».
    «belmont» — собственное место + просто фамилия). Мы err on the
    side of dropping noise, так как:
      1. для стилистических маркеров автора эти токены почти всегда
         имена собственные (Doyle описывает Boer-войну → топонимы);
      2. real-lexeme leak — лишь снижает покрытие top-N на 1-2 слова.
"""
from __future__ import annotations

from pathlib import Path

# Curated 19th-c. literary toponyms.
# Группировка по корпусам, в которых они доминируют. Расширять
# прагматично, по факту утечки в production.
_CURATED_TOPONYMS: frozenset[str] = frozenset({
    # Boer War / South Africa (Conan Doyle: «The Great Boer War», 1900;
    # «The War in South Africa», 1902 — military / non-fiction works)
    "burger", "burgers", "uitlander", "uitlanders",
    "belmont", "colesberg", "kroonstad", "bloemfontein",
    "ladysmith", "mafeking", "kimberley", "pretoria",
    "transvaal", "natal", "boer", "boers",
    "magersfontein", "spionkop", "elandslaagte", "stormberg",
    "modder", "tugela", "vaal", "drakensberg",
    "krugersdorp", "johannesburg", "durban",
    "orange",  # Orange Free State / Orange River — DROP only when it's
               #   evidently the place; ambiguous with fruit. В контексте
               #   Doyle SA-corpus = всегда place.
    "rhodesia", "transvaal",
    # Phase 3 W-4 reconciliation against tz_claude_code_fixes_2026-05-22:
    # Stan flagged «conflans» and «donga» as additional Boer-war leaks
    # in Doyle's affinity list. «conflans» is a Napoleonic / late-19th-c.
    # battle place (and a Paris commune). «donga» is a South African
    # English loanword from Zulu — strictly a place-feature term
    # («steep-sided ravine»). Both behave as place-tied vocabulary that
    # doesn't belong in a stylistic-markers list for the author.
    "conflans",
    "donga", "dongas",
    # Other Boer-war / SA-corpus narrow-locality vocabulary that surfaces
    # in Doyle and Kipling but isn't general English: «kopje» (small
    # hill), «veldt» (open grassland), «kraal» (village/enclosure),
    # «sjambok» (hide whip), «laager» (encampment), «drift» (river
    # ford) — all place-feature jargon, not stylistic markers.
    "kopje", "kopjes",
    "veldt", "veld",
    "kraal", "kraals",
    "sjambok",
    "laager", "laagers",
    # «drift» is ambiguous (psychological vs. physical drift), so
    # we DON'T blocklist it — let it pass and accept the noise.

    # British Isles toponyms common in 19th-c. fiction
    "thames", "westminster", "kensington", "chelsea",
    "soho", "marylebone", "bloomsbury", "lambeth",
    "whitechapel", "limehouse", "wapping", "deptford",
    "putney", "richmond", "twickenham",
    "yorkshire", "lancashire", "northumberland", "cumberland",
    "shropshire", "wessex", "wessex",
    "tyne", "humber", "severn", "trent",
    "edinburgh", "glasgow", "aberdeen",
    "cardiff", "swansea",
    "dublin", "belfast", "cork",
    # Sherlock Holmes locales
    "baskerville", "grimpen", "dartmoor",
    "reigate", "ross", "salisbury",

    # Russia / Russian Empire (Tolstoy, Dostoevsky, Pushkin translated +
    # English authors writing about Russia)
    "petersburg", "moscow", "tsarskoye", "kazan", "siberia",
    "crimea", "sevastopol", "odessa", "kiev",
    "volga", "neva", "ural", "caucasus",

    # France / Napoleonic (Hugo, Dumas; English authors covering France)
    "marseilles", "marseille", "toulon", "rouen", "lyon",
    "bordeaux", "nantes", "calais", "strasbourg",
    "vendee", "alsace", "lorraine", "burgundy", "normandy",
    "champagne", "provence", "brittany", "auvergne",
    "seine", "loire", "rhone", "garonne",
    "waterloo", "austerlitz", "borodino",

    # Italy / classical
    "venice", "florence", "naples", "milan", "turin",
    "genoa", "verona", "padua", "siena", "pisa",
    "tuscany", "lombardy", "piedmont", "umbria",
    "tiber", "arno", "po", "etna", "vesuvius",
    "ostia", "ravenna", "pompeii", "herculaneum",
    "carthage", "syracuse", "alexandria", "thebes",

    # Greece / classical mythology places
    "athens", "sparta", "corinth", "delphi", "olympia",
    "attica", "peloponnese", "thessaly", "macedonia",
    "ithaca", "crete", "lesbos", "rhodes",
    "aegean", "ionian", "hellespont", "bosphorus",

    # USA / colonies (Twain, Melville, Hawthorne; English authors writing
    # about Americas)
    "boston", "philadelphia", "richmond", "savannah",
    "nantucket", "concord", "lexington",
    "mississippi", "missouri", "ohio", "hudson",
    "vermont", "carolina", "virginia",
    "louisiana", "carolina",

    # India / colonial setting (Kipling, post-colonial English fiction)
    "punjab", "bengal", "bombay", "madras", "delhi",
    "calcutta", "lahore", "peshawar", "kashmir",
    "ganges", "indus", "himalaya", "himalayas",
    "afghan", "afghanistan",  # Kipling, Doyle (Watson's wound)
    "khyber", "kabul", "kandahar",

    # Australia / Pacific / Africa (frontier fiction)
    "sydney", "melbourne", "tasmania", "queensland",
    "cairo", "alexandria", "khartoum", "nile",
    "zanzibar", "congo", "sahara",

    # Far East (Conrad, Stevenson, RLS)
    "shanghai", "canton", "hongkong", "yokohama", "edo",
    "siam", "java", "sumatra", "borneo",
    "samoa", "tahiti", "fiji", "tonga",

    # Mid East / Holy Land (biblical setting common in 19th-c. fiction)
    "jerusalem", "bethlehem", "nazareth", "galilee",
    "judea", "samaria", "gilead",
    "damascus", "aleppo", "baghdad", "basra", "mosul",
    "mecca", "medina", "yemen", "oman",

    # Fictional / generic place suffixes that surface as words
    # «-shire», «-ville», «-bury», «-borough» — too generic to blocklist
    # standalone, but specific tokens above cover the common bleeds.
})


# spaCy NER PROPER labels surfaced as GPE / LOC. Used when reading the
# NER-derived `*_affinity_ner.csv` enrichment (см. `ner_filter_affinity.py`).
_GEO_NER_LABELS = frozenset({"GPE", "LOC", "FAC"})


def is_toponym(word: str) -> bool:
    """True iff `word` is a known toponym from the curated list.

    Conservative: only matches against the curated frozenset. NER-derived
    extensions live in `toponym_blocklist()` which reads NER CSVs if
    available; this helper is the cheap synchronous check.
    """
    if not word or not isinstance(word, str):
        return False
    return word.strip().lower() in _CURATED_TOPONYMS


def toponym_blocklist(ner_csv_paths: list[Path] | None = None) -> frozenset[str]:
    """Union of curated toponyms + (optional) NER-derived GPE/LOC surfaces.

    `ner_csv_paths` — list of `*_affinity_ner.csv` files (see
    `scripts/ner_filter_affinity.py`). If provided, rows with
    `ner_label in {GPE, LOC, FAC}` contribute their lowercased word to
    the blocklist. Missing files are silently skipped — production
    deployments may not have run NER enrichment yet.
    """
    out = set(_CURATED_TOPONYMS)
    for p in (ner_csv_paths or []):
        if not p or not p.exists():
            continue
        try:
            import csv
            with open(p, encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for row in rd:
                    label = (row.get("ner_label") or "").strip().upper()
                    if label in _GEO_NER_LABELS:
                        w = (row.get("word") or "").strip().lower()
                        if w and w.isalpha() and len(w) >= 3:
                            out.add(w)
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return frozenset(out)


def filter_toponyms(rows: list[dict], *,
                    word_key: str = "word",
                    ner_csv_paths: list[Path] | None = None,
                    ) -> tuple[list[dict], int]:
    """Drop rows whose `word_key` is a toponym.

    Returns (filtered_rows, dropped_count). Mirrors the API of
    `filter_surnames` / `filter_corpus_artifacts` so wrappers can chain
    all three with the same shape.
    """
    if not rows:
        return rows, 0
    blocklist = toponym_blocklist(ner_csv_paths)
    if not blocklist:
        return rows, 0
    kept = [r for r in rows
            if (r.get(word_key) or "").lower() not in blocklist]
    return kept, len(rows) - len(kept)


__all__ = [
    "filter_toponyms",
    "is_toponym",
    "toponym_blocklist",
    "_CURATED_TOPONYMS",
]
