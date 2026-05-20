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
from pathlib import Path
from typing import Literal


# Canonical author aliases — TWO layers (Sprint 16 / v3.0):
#
#   AUTHOR_ALIASES_CURATED  — hand-curated dict below. Russian stem forms,
#                             ambiguity guards (e.g. "по" preposition),
#                             multi-word aliases, non-standard translit.
#   AUTHOR_ALIASES_GENERATED — auto-built from corpus metadata.csv by
#                             scripts/v2/build_author_aliases.py.
#                             Stored as JSON, loaded lazily.
#
# Runtime: AUTHOR_ALIASES = {**generated, **curated} — curated wins on key
# conflict. Every entry in CURATED is auto-regression-tested:
#   tests/v2/test_aliases_regression.py
# adding a new entry → next test run verifies extraction works for it.
AUTHOR_ALIASES_CURATED: dict[str, str] = {
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
    # Stan round 3 missing aliases
    "шекспир":          "^Shakespeare,",
    "шекспира":         "^Shakespeare,",
    "шекспиру":         "^Shakespeare,",
    "шекспире":         "^Shakespeare,",
    "shakespeare":      "^Shakespeare,",
    "лермонтов":        "^Lermontov,",
    "лермонтова":       "^Lermontov,",
    "lermontov":        "^Lermontov,",
    "булгаков":         "^Bulgakov,",
    "булгакова":        "^Bulgakov,",
    "bulgakov":         "^Bulgakov,",
    "набоков":          "^Nabokov,",
    "набокова":         "^Nabokov,",
    "nabokov":          "^Nabokov,",
    # Q3 Salinger (common copyright author)
    "сэлинджер":        "^Salinger,",
    "salinger":         "^Salinger,",
    # Sprint 17 Round 8 — Elizabethan / Jacobean dramatists (Shakespeare-
    # contemporaries cohort). Round 8 C3-2 surfaced via Marlowe gap;
    # adding the rest preemptively to avoid the same silent-fallback
    # trap on related queries.
    "marlowe":          "^Marlowe,",
    "марло":            "^Marlowe,",
    "марлоу":           "^Marlowe,",
    "кристофер марло":  "^Marlowe,",
    "christopher marlowe": "^Marlowe,",
    "webster":          "^Webster, John",  # disambiguate from Webster, Noah
    "уэбстер":          "^Webster, John",
    "вебстер":          "^Webster, John",
    "jonson":           "^Jonson, Ben",
    "ben jonson":       "^Jonson, Ben",
    "джонсон":          "^Jonson, Ben",     # ambiguous w/ Samuel Johnson
    "бен джонсон":      "^Jonson, Ben",
    "dekker":           "^Dekker,",
    "деккер":           "^Dekker,",
    "kyd":              "^Kyd,",
    "томас кид":        "^Kyd,",
    "beaumont":         "^Beaumont,",
    "бомонт":           "^Beaumont,",
    "fletcher":         "^Fletcher, John",  # John Fletcher (playwright)
    "флетчер":          "^Fletcher, John",
    "middleton":        "^Middleton,",
    "мидлтон":          "^Middleton,",
    # Sprint 19+ — Gothic novelists (Stan 2026-05-19 «у Walpole в Castle
    # of Otranto»). Round 6 also asked about Radcliffe / Maturin / Lewis;
    # adding the cohort preemptively.
    "walpole":          "^Walpole, Horace",  # disambiguate from Hugh Walpole
    "horace walpole":   "^Walpole, Horace",
    "уолпол":           "^Walpole, Horace",
    "уолпола":          "^Walpole, Horace",  # gen
    "уолполу":          "^Walpole, Horace",  # dat
    "хорас уолпол":     "^Walpole, Horace",
    "хораса уолпола":   "^Walpole, Horace",  # gen
    "radcliffe":        "^Radcliffe, Ann",
    "ann radcliffe":    "^Radcliffe, Ann",
    "радклифф":         "^Radcliffe, Ann",
    "радклиффа":        "^Radcliffe, Ann",
    "анна радклифф":    "^Radcliffe, Ann",
    "matthew lewis":    "^Lewis, M",  # Matthew Gregory Lewis (The Monk)
    "м. г. льюис":      "^Lewis, M",
    "мэтью льюис":      "^Lewis, M",
    "maturin":          "^Maturin,",
    "матюрин":          "^Maturin,",
    "матьюрин":         "^Maturin,",
    # Sprint 19+ — Milton (Stan 2026-05-19 «germanic vs latinate ratio
    # в Beowulf и Paradise Lost» — author behind Paradise Lost wasn't
    # resolvable). 17th-c. English poet, anchor for Latinate vocabulary
    # contrast against Old-English-rooted Beowulf.
    "milton":           "^Milton, John",
    "john milton":      "^Milton, John",
    "мильтон":          "^Milton, John",
    "мильтона":         "^Milton, John",
    "джон мильтон":     "^Milton, John",
    # Sprint 19+ — Victorian novelists (Stan 2026-05-19 «Burrows Delta
    # между Dickens и Trollope: кто ближе к Eliot» — neither in
    # AUTHOR_ALIASES). Anthony Trollope + George Eliot (Mary Ann Evans).
    "trollope":         "^Trollope, Anthony",  # disambiguate from F. Trollope
    "anthony trollope": "^Trollope, Anthony",
    "троллоп":          "^Trollope, Anthony",
    "троллопа":         "^Trollope, Anthony",
    "энтони троллоп":   "^Trollope, Anthony",
    "eliot":            "^Eliot, George",      # disambiguate from T.S. Eliot
    "george eliot":     "^Eliot, George",
    "mary ann evans":   "^Eliot, George",
    "элиот":            "^Eliot, George",
    "элиота":           "^Eliot, George",
    "джордж элиот":     "^Eliot, George",
}


def _load_generated_aliases() -> dict[str, str]:
    """Lazy-load auto-generated alias dict produced by
    scripts/v2/build_author_aliases.py. Missing file → empty dict
    (no crash). Stale file (older than corpus metadata) is fine — the
    aliases just won't cover new authors until next rebuild.

    Generated entries are merged BENEATH curated ones so manual
    overrides always win. See docs/v2/PLUGIN.md §1.
    """
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "data" / "aliases_generated.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Accept either flat dict or {"aliases": {...}} wrapper
        if isinstance(data, dict) and "aliases" in data:
            data = data["aliases"]
        if not isinstance(data, dict):
            return {}
        # Validate shape — each value must be a `^Surname,`-style regex
        import re as _re
        out: dict[str, str] = {}
        for k, v in data.items():
            if (isinstance(k, str) and isinstance(v, str)
                    and _re.match(r"^\^[A-Za-zЀ-ӿ'-]+,?$", v)):
                out[k.lower()] = v
        return out
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


# Merge generated under curated. Curated wins on key conflict — manual
# overrides are explicit by intent.
AUTHOR_ALIASES: dict[str, str] = {
    **_load_generated_aliases(),
    **AUTHOR_ALIASES_CURATED,
}


KNOWN_BOOKS: dict[str, tuple[str, str]] = {
    # title_query_normalized -> (pg_id_or_empty, canonical_title)
    # "PG"  on its own == "not in SPGC, but a real book the user named". The
    #       planner uses canonical_title to talk back to the user instead of
    #       hallucinating a chain. Empty PG id = book_title set, book_id None.

    # Russian classics (English translations in SPGC)
    "преступление и наказание":   ("PG2554", "Crime and Punishment"),
    # Sprint 16/17 — full RU declension coverage for the most-asked-about
    # Russian-titled books. «что почитать после Преступления и наказания»
    # (gen), «как в Преступлении и наказании» (prep), «что подобное
    # Преступлению и наказанию» (dat) — all natural Russian phrasings.
    "преступления и наказания":   ("PG2554", "Crime and Punishment"),  # gen
    "преступлении и наказании":   ("PG2554", "Crime and Punishment"),  # prep
    "преступлению и наказанию":   ("PG2554", "Crime and Punishment"),  # dat
    "преступлением и наказанием": ("PG2554", "Crime and Punishment"),  # inst
    "crime and punishment":       ("PG2554", "Crime and Punishment"),
    "war and peace":              ("PG2600", "War and Peace"),
    "война и мир":                ("PG2600", "War and Peace"),
    "войны и мира":               ("PG2600", "War and Peace"),  # gen
    "войне и мире":               ("PG2600", "War and Peace"),  # prep
    "войне и миру":               ("PG2600", "War and Peace"),  # dat
    "войной и миром":             ("PG2600", "War and Peace"),  # inst
    "войну и мир":                ("PG2600", "War and Peace"),  # acc

    # Shakespeare — Sprint 17 (Stan readability-compare bug, «Сон в летнюю
    # ночь»). PG1514 is the canonical Project Gutenberg id for A Midsummer
    # Night's Dream; add nominative + genitive + EN apostrophe variants.
    "a midsummer night's dream":  ("PG1514", "A Midsummer Night's Dream"),
    "midsummer night's dream":    ("PG1514", "A Midsummer Night's Dream"),
    "midsummer nights dream":     ("PG1514", "A Midsummer Night's Dream"),  # no apostrophe
    "сон в летнюю ночь":          ("PG1514", "A Midsummer Night's Dream"),
    "сна в летнюю ночь":          ("PG1514", "A Midsummer Night's Dream"),  # gen
    "сне в летнюю ночь":          ("PG1514", "A Midsummer Night's Dream"),  # prep
    "hamlet":                     ("PG1524", "Hamlet"),
    "гамлет":                     ("PG1524", "Hamlet"),
    "гамлета":                    ("PG1524", "Hamlet"),  # gen/acc
    "romeo and juliet":           ("PG1112", "Romeo and Juliet"),
    "ромео и джульетта":          ("PG1112", "Romeo and Juliet"),
    "macbeth":                    ("PG2264", "Macbeth"),
    "макбет":                     ("PG2264", "Macbeth"),

    # Sprint 19+ — Gothic classics (Stan 2026-05-19 «архаизмы у Walpole
    # в Castle of Otranto» fell to clarify — title not in KNOWN_BOOKS).
    # The Castle of Otranto (1764) is THE founding Gothic novel; users
    # asking about Walpole almost always mean this.
    "castle of otranto":          ("PG696", "The Castle of Otranto"),
    "the castle of otranto":      ("PG696", "The Castle of Otranto"),
    "замок отранто":              ("PG696", "The Castle of Otranto"),
    "замке отранто":              ("PG696", "The Castle of Otranto"),
    # The Mysteries of Udolpho (Radcliffe) — Gothic canon
    "mysteries of udolpho":       ("PG3268", "The Mysteries of Udolpho"),
    "the mysteries of udolpho":   ("PG3268", "The Mysteries of Udolpho"),
    "удольфо":                    ("PG3268", "The Mysteries of Udolpho"),
    # The Monk (Lewis) — Gothic
    "the monk":                   ("PG601", "The Monk"),
    "monk a romance":             ("PG601", "The Monk"),
    "монах":                      ("PG601", "The Monk"),

    # Sprint 19+ — Old English / 17th-c epic poetry (Stan 2026-05-19
    # «germanic vs latinate ratio в Beowulf и Paradise Lost» — neither
    # in KNOWN_BOOKS so book_id resolution failed).
    # Beowulf: PG16328 = Gummere 1910 Modern English verse translation,
    # the most-read public-domain edition.
    "beowulf":                    ("PG16328", "Beowulf"),
    "беовульф":                   ("PG16328", "Beowulf"),
    "беовульфа":                  ("PG16328", "Beowulf"),
    # Paradise Lost: PG26 = Milton, 1667. Anchor for Latinate diction.
    "paradise lost":              ("PG26",    "Paradise Lost"),
    "потерянный рай":             ("PG26",    "Paradise Lost"),
    "потерянного рая":            ("PG26",    "Paradise Lost"),
    "потерянном рае":             ("PG26",    "Paradise Lost"),

    # English / American canon
    "pride and prejudice":        ("PG1342", "Pride and Prejudice"),
    "гордость и предубеждение":   ("PG1342", "Pride and Prejudice"),
    "гордости и предубеждения":   ("PG1342", "Pride and Prejudice"),  # gen
    "гордости и предубеждении":   ("PG1342", "Pride and Prejudice"),  # prep
    "dracula":                    ("PG345",  "Dracula"),
    "дракула":                    ("PG345",  "Dracula"),
    "дракулы":                    ("PG345",  "Dracula"),  # gen
    "дракуле":                    ("PG345",  "Dracula"),  # prep / dat
    "дракулу":                    ("PG345",  "Dracula"),  # acc
    "дракулой":                   ("PG345",  "Dracula"),  # inst
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
    # Stan round 2 Q11 — Harry Potter is post-1929 copyright
    "harry potter":               ("",      "Harry Potter"),
    "гарри поттер":               ("",      "Harry Potter"),
    "harry potter and the philosopher's stone": ("", "Harry Potter and the Philosopher's Stone"),
    # Stan round 3 Q14 — Anna Karenina has a Tolstoy translation in PG, NOT copyright
    "anna karenina":              ("PG1399", "Anna Karenina"),
    "анна каренина":              ("PG1399", "Anna Karenina"),
    "анны карениной":             ("PG1399", "Anna Karenina"),  # gen (Sprint 16 Phase G)
    "анне карениной":             ("PG1399", "Anna Karenina"),  # prep / dat
    "анну каренину":              ("PG1399", "Anna Karenina"),  # acc
    "анной карениной":            ("PG1399", "Anna Karenina"),  # inst
    # Catcher in the Rye is copyright
    "catcher in the rye":         ("",      "The Catcher in the Rye"),
    "the catcher in the rye":     ("",      "The Catcher in the Rye"),
    "над пропастью во ржи":       ("",      "The Catcher in the Rye"),
}


COUNTRY_ALIASES: dict[str, str] = {
    # Sprint 19+ — added «английск» (Stan «английских авторов XIX века»
    # caught no country signal because only «английский» = «English (lang)»
    # was in the dict, not «английский (Brit. nation)»). Both senses map
    # to GB in our corpus context (English-language ≈ British in SPGC).
    "британск": "GB", "british": "GB", "англия": "GB", "english": "GB",
    "uk": "GB", "брит": "GB", "английск": "GB",
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
    top_metric: Literal["books", "downloads", "tokens"] | None = None
    multi_author_regex: list[str] = field(default_factory=list)  # «Мелвилла, Конрада и Стивенсона»
    # Sprint 17 — multi-book extraction for compare queries («сложнее
    # читать X или Y», «слова в X и Y, но редко в Z»). multi_book_ids is
    # the secondary list (primary stays in book_id); each entry is a PG/U
    # id when resolved via KNOWN_BOOKS, otherwise None and the canonical
    # title lives at the same index in multi_book_titles.
    multi_book_ids: list[str | None] = field(default_factory=list)
    multi_book_titles: list[str] = field(default_factory=list)
    # Sprint 20+ (Stan Round 11 B4 + B9): hint fields that plan builders
    # use to constrain tool args. lang_hint defaults to None — plan
    # builders only apply when explicitly set (e.g. «английская
    # классика» → 'en'). exclude_archaic flag for book_recommendation.
    lang_hint: str | None = None
    exclude_archaic: bool = False
    # Sprint 20+ (Stan Round 11 B3): export-format hint for
    # export_word_list followups. csv / anki / markdown / json. None
    # means «no explicit format requested» (default to csv).
    export_format: str | None = None
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


# Short Cyrillic aliases that double as common Russian prepositions /
# particles. For these we require a stronger context check — the
# original «alphabetic boundary» check passes for «дай статистику по
# Wodehouse» because « » before «по» is non-alpha. Stan's round 5 found
# this collision and 100% of «по» occurrences extracted `^Poe,`.
_AMBIGUOUS_SHORT_ALIASES = frozenset({"по"})


def _is_preposition_collision(s: str, i: int, j: int, key: str) -> bool:
    """Return True iff `key` is a Russian-preposition-like short alias
    being used as a preposition in `s[i:j]` (not as a proper noun).

    Test: the preposition «по» typically appears in lowercase. When user
    means the author Poe, they almost always write capitalized («По» с
    большой) or use English form («Poe»). The original-case check
    catches that the matched span used to be capitalized in the input —
    when it's lowercase «по», it's the preposition, not the author.
    """
    if key not in _AMBIGUOUS_SHORT_ALIASES:
        return False
    # `s` is lowercased — check the ORIGINAL text at offset i:j by
    # reading from raw_text via the same offsets. We can't here — `s` is
    # what we got. Fall back to context heuristic: if «по» is followed
    # by a noun in the sentence (any word longer than 2 chars within 30
    # chars), it's almost certainly the preposition. Real «По» author
    # references usually look like «у По», «работа По», «По в 19 веке».
    rest = s[j:j + 40]
    # If next word is short (<3 chars) or text ends right after, treat
    # as proper noun usage. «у По» / «работа По» → preposition guard
    # OFF. Otherwise — treat as preposition.
    import re
    m = re.match(r"\s+(\S+)", rest)
    if not m:
        return False  # end of text — could be proper noun
    next_word = m.group(1).strip(",.!?;:»\"")
    # Author surname is followed by another word that's typically a
    # preposition-noun chain («по теме», «по стилю», «по Wodehouse»).
    # If next_word is itself a capitalized author-like token, this is
    # the preposition «по» pointing to that author — so author
    # extraction will pick the right one, and we should suppress «По».
    # Simple heuristic: if next_word has uppercase first letter OR is
    # a long Cyrillic word (likely a noun), treat «по» as preposition.
    if next_word and (next_word[0].isupper() or len(next_word) >= 4):
        return True
    return False


def _find_authors(text: str) -> list[tuple[str, str]]:
    """Return [(label, regex)] for every alias found.

    Three safety measures:
      1. Word boundary check — 'по' must not match inside 'повторяются'. We
         require the alias to be flanked by non-word chars (or string edges).
         For Cyrillic stems we also accept the alias with any Russian case
         ending (Дойл → Дойла/Дойлу/Дойле/Дойлом) via trailing-letter tolerance.
      2. Length-desc iteration — 'конан дойл' is tried before 'дойл' so the
         multi-word alias wins on overlapping slices.
      3. Preposition guard (Stan round 5 critical finding) — Russian
         preposition «по» collided with the «по» alias of «Poe» across 5
         test rounds, causing «дай статистику ПО Wodehouse» to extract
         author=^Poe,. For 2-char Cyrillic aliases that double as
         prepositions, require the match to be IMMEDIATELY at start of
         text, OR preceded by a non-«preposition-context» word. The
         honest fix is to also exclude «по» as an alias when followed
         by any subject in the same sentence — handled at the
         _AMBIGUOUS_SHORT_ALIASES guard below.
    """
    s = text.lower()
    n = len(s)
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
                    and not any(cs <= i < ce or cs < j <= ce for cs, ce in consumed)
                    and not _is_preposition_collision(s, i, j, k)):
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


# ---------- user-upload resolver (Sprint 19+ HP fix) ----------
# When the user uploaded a copyrighted book locally (HP / LOTR / 1984 /
# Catcher / etc. — listed in KNOWN_BOOKS with empty PG sentinel), we
# should resolve to the user's U-id instead of refusing with the
# copyright-OOS message. The book is now physically present on the
# admin's drive; the OOS message was «we don't have the full text»,
# which becomes a lie. Renderer adds a copyright disclosure footer
# instead — see RENDER_PROMPT rule 12.

_USER_UPLOADS_CACHE: dict[str, str] | None = None
_USER_UPLOADS_CACHE_MTIME: float = 0.0
_USER_UPLOADS_META_PATH = Path("/workspace/spgc/derived/user_uploads_metadata.csv")


def _load_user_upload_titles() -> dict[str, str]:
    """Return {normalized_title_lc: u_id}. Cached, mtime-aware so admin
    uploads after server start become visible without restart."""
    global _USER_UPLOADS_CACHE, _USER_UPLOADS_CACHE_MTIME
    p = _USER_UPLOADS_META_PATH
    if not p.exists():
        return {}
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _USER_UPLOADS_CACHE or {}
    if _USER_UPLOADS_CACHE is not None and mtime == _USER_UPLOADS_CACHE_MTIME:
        return _USER_UPLOADS_CACHE
    import csv as _csv
    out: dict[str, str] = {}
    try:
        with open(p, encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                u_id = (row.get("id") or "").strip()
                title = (row.get("title") or "").strip()
                if u_id and title:
                    out[title.lower()] = u_id
    except OSError:
        return _USER_UPLOADS_CACHE or {}
    _USER_UPLOADS_CACHE = out
    _USER_UPLOADS_CACHE_MTIME = mtime
    return out


def find_user_upload_for_title(canonical_title: str) -> str | None:
    """Match a canonical KNOWN_BOOKS title against user_uploads_metadata.csv.

    Substring-matching both ways: a user uploaded «Harry Potter and the
    Philosopher's Stone» should match canonical «Harry Potter», AND
    a user upload titled exactly «Harry Potter» should match the
    canonical too. Returns the first matching U-id (sorted by id for
    determinism)."""
    if not canonical_title:
        return None
    uploads = _load_user_upload_titles()
    if not uploads:
        return None
    target = canonical_title.lower().strip()
    # Exact match first
    if target in uploads:
        return uploads[target]
    # Substring either direction
    matches: list[tuple[str, str]] = []
    for upload_title, u_id in uploads.items():
        if target in upload_title or upload_title in target:
            matches.append((u_id, upload_title))
    if matches:
        # Deterministic — pick the lowest U-id (oldest upload)
        matches.sort(key=lambda x: int(x[0][1:]) if x[0][1:].isdigit() else 0)
        return matches[0][0]
    return None


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
            pg, canonical = KNOWN_BOOKS[key]
            if pg.startswith("PG") and pg != "PG":
                return pg, title
            # Sprint 19+ — empty-PG sentinel means «known but copyright,
            # not in SPGC». BEFORE returning (None, title) which triggers
            # the copyright-OOS refusal, check whether the admin
            # uploaded this title locally. If yes, resolve to the
            # U-id — tool chain works, renderer adds a copyright
            # disclosure footer (RENDER_PROMPT rule 12 extension).
            u_id = find_user_upload_for_title(canonical or title)
            if u_id:
                return u_id, title
            return None, title
        # Single short token (no spaces, < 12 chars) is almost certainly a
        # word, not a book title. Defer to _find_word; book_title stays None.
        #
        # Sprint 19 — context-aware override (Stan 2026-05-19 «эмоциональный
        # профиль "Politics" Аристотеля» and «"Leviathan"» fell here and
        # bounced to clarify). When the query has an explicit BOOK-context
        # trigger («профиль / архаизм / уровень сложности / стиль / vocab /
        # signature / readability / emotion»), keep the short quoted token
        # as a book title — it can chain through find_book downstream.
        if " " not in title and len(title) < 12:
            low_text = text.lower()
            book_context_triggers = (
                "эмоциональн", "профил", "архаизм",
                "уровень сложн", "сложности", "ridability", "readability",
                "стил", "лексик", "словар", "vocab", "signature",
                "характерн", "фирменн", "emotion", "архаичн",
                "сравн", "compare",
            )
            if not any(t in low_text for t in book_context_triggers):
                continue
        # Q10-style: «второго уровня» / «третьего уровня» / «второй» — these
        # are scope keywords in Russian, not book titles. Cyrillic-only
        # phrases shorter than ~25 chars almost never name a book (real
        # Russian-translated titles like «Преступление и наказание» go
        # via KNOWN_BOOKS above; rare untranslated Russian titles would
        # exceed the length threshold). Skip and let other quoted strings
        # (or the unquoted KNOWN_BOOKS scan) take over.
        is_cyrillic_only = all(
            (not ch.isalpha()) or ('а' <= ch.lower() <= 'я') or ch.lower() == 'ё'
            for ch in title
        )
        if is_cyrillic_only and len(title) < 25:
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
            # Sprint 19+ — empty-PG sentinel: try user uploads before
            # giving up to copyright OOS. Same fallback as in the
            # quoted-title branch above.
            u_id = find_user_upload_for_title(canon)
            if u_id:
                return u_id, canon
            return None, canon
    return None, None


# Sprint 17 — multi-book extractor for «X или Y» / «X и Y» / «X vs Y»
# compare queries. Returns ALL distinct books mentioned in `text`, ordered
# by first appearance, deduped by canonical title. Primary is index 0.
def _find_all_books(text: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    seen_canonicals: set[str] = set()

    # Explicit PG/U ids first
    for m in _BOOK_PG_ID.finditer(text):
        pg = m.group(1).upper()
        canon_key = pg  # no canonical title for raw ids
        if canon_key not in seen_canonicals:
            out.append((pg, pg))
            seen_canonicals.add(canon_key)

    # KNOWN_BOOKS scan — longest-first per existing convention, but
    # collect ALL distinct canonicals, not just the first match. Two
    # different alias keys («война и мир» / «war and peace») resolve to
    # the same canonical title — count those as ONE book.
    low = text.lower()
    # Track which character ranges are already claimed by a longer key
    # so «sons and lovers» doesn't also claim «sons» as a separate book.
    claimed_ranges: list[tuple[int, int]] = []
    for key in sorted(KNOWN_BOOKS, key=len, reverse=True):
        start = low.find(key)
        if start < 0:
            continue
        end = start + len(key)
        if any(s <= start < e for s, e in claimed_ranges):
            continue
        pg, canon = KNOWN_BOOKS[key]
        pg_id = pg if (pg.startswith("PG") and pg != "PG") else None
        if canon in seen_canonicals:
            continue
        seen_canonicals.add(canon)
        claimed_ranges.append((start, end))
        out.append((pg_id, canon))
    # Sort by first-appearance position so user's left-to-right order
    # is preserved in the result.
    def _pos(item):
        pg_id, canon = item
        # explicit PG id — find its position in original text
        if pg_id and pg_id == canon:
            m = _BOOK_PG_ID.search(text)
            return m.start() if m else 1 << 30
        # KNOWN_BOOKS hit — locate matching key in low
        for key in sorted(KNOWN_BOOKS, key=len, reverse=True):
            if KNOWN_BOOKS[key][1] == canon and key in low:
                return low.find(key)
        return 1 << 30
    out.sort(key=_pos)
    return out


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
# Catch the word-noun in any case (слово / слова / слову / словом / слове),
# plus English equivalents. The captured token still has to dodge the
# stopword set so question phrasings like «слова чаще встречаются» don't
# bucket e.word="чаще". Stan's 2026-05-18 demon round caught the original
# version missing «словом fog» (instrumental case) and «слова sword»
# (genitive). Single-form rule was too narrow for free-form Russian.
_WORD_AFTER_KEY = re.compile(
    r"\b(?:слов(?:о|а|у|ом|е)|word|the\s+word)\s+"
    r"[\"'«“]?([a-zA-Zа-яё-]{2,30})[\"'»”]?",
    re.IGNORECASE,
)
# «этимология слова X» / «этимология X» / «происхождение слова X» / «contexts
# of X» — single bare ASCII/Cyrillic token directly after a word-anchor verb.
# Distinct rule (not folded into _WORD_AFTER_KEY) so we can tighten the
# trigger keywords without weakening the «слово X» case.
_WORD_AFTER_VERB = re.compile(
    r"\b(?:этимолог\w*|происхожден\w*|соседств\w*|"
    # Sprint 20 — Stan 2026-05-19 «найди упоминания burger у Дойла»:
    # «упоминания» / «вхождения» / «mentions of» / «occurrences of»
    # weren't trigger words, so the word stayed unextracted even
    # though the intent rule fired correctly.
    r"упоминан\w*|вхождени\w*|встречаемост\w*|"
    r"mentions?\s+of|occurrences?\s+of|"
    r"collocates?\s+of|etymology\s+of|contexts?\s+of)\s+"
    r"(?:слова\s+|word\s+)?"
    r"[\"'«“]?([a-zA-Zа-яё-]{3,30})[\"'»”]?",
    re.IGNORECASE,
)
# Sprint 17 (Round 7 Q8): «примеры ajar у Доyла» / «examples of ajar in
# Dickens» — bare English token after «примеры»/«examples». Distinct
# regex because we ONLY accept Latin-script captures here (Russian
# fillers like «авторов» / «слова» would otherwise sneak through —
# they're more likely false positives than real English-word targets).
_BARE_WORD_AFTER_EXAMPLES = re.compile(
    r"\b(?:[Пп]ример(?:ы|ов|ами)?"
    r"(?:\s+использовани\w*|\s+употреблени\w*)?|"
    r"[Ee]xamples?(?:\s+of)?)"
    r"\s+(?:слова\s+|word\s+)?"
    r"[\"'«“]?([a-z][a-z-]{2,29})[\"'»”]?",
)
# «имени Анна / имя Anna / по имени X» — Stan asks for usage examples of a
# personal name in literature. We capture the bare proper noun after the
# keyword and pass it through to word_contexts / hybrid_search; the search
# layer's `_maybe_translate` handles Russian→English on the way to ChromaDB.
#
# Important: this regex is *not* `re.IGNORECASE`. The proper-noun guard
# (`[A-ZА-ЯЁ]` lead letter on the captured word) is the only thing that
# blocks fillers like «имя автора» / «от моего имени напиши» from
# bleeding through — flipping case-insensitive would defeat it.
_NAME_AFTER_KEY = re.compile(
    r"\b[Ии]м(?:я|ени|енем)\s+[\"'«“]?([A-ZА-ЯЁ][a-zA-Zа-яё-]{1,29})[\"'»”]?"
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
    m = _WORD_AFTER_VERB.search(text)
    if m:
        w = m.group(1).lower()
        if (len(w) >= 3 and not w.istitle()
                and w not in _WORD_STOPWORDS):
            return w
    # Sprint 17 — bare English word after «примеры/examples». Latin-only
    # capture so Russian fillers («примеры авторов» / «примеры слов» —
    # genitive plurals that don't start with «слов»+suffix word-trigger)
    # don't sneak through.
    m = _BARE_WORD_AFTER_EXAMPLES.search(text)
    if m:
        w = m.group(1).lower()
        if (len(w) >= 3 and w not in _WORD_STOPWORDS
                and "слов" not in w):
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
# «викторианский / викторианцев / викторианки / викторианская эпоха» —
# match the stem before «-ск/-нц/-нка» so all case forms land. Stan round
# 2 Q5: «у викторианцев» (instrumental of «викторианец») used to miss
# because the old pattern required «-нск-» which the plural-genitive
# form doesn't have.
_VICTORIAN = re.compile(r"виктори[аяь]н(?:ск\w*|ц\w*|к\w*)", re.IGNORECASE)
_EDWARDIAN = re.compile(r"эдвард(?:иан|овск)\w*", re.IGNORECASE)
_ROMANTICISM = re.compile(r"\bроманти[зч]\w*\s+эпох|эпох\w*\s+романти",
                          re.IGNORECASE)
_NINETEENTH = re.compile(r"\b(?:19|XIX|девятнадцат\w*)\s*(?:век|century)",
                         re.IGNORECASE)


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
    if _EDWARDIAN.search(text):
        return 1901, 1914
    if _NINETEENTH.search(text):
        return 1800, 1899
    return None, None


# ---------- country ----------


def _find_country(text: str) -> str | None:
    """Match country aliases. Latin-script aliases require word boundaries
    to prevent substring false positives — `german` was matching inside
    `germanic` (etymology family) and tagging the query as country=DE
    (Stan 2026-05-19 «germanic vs latinate ratio в Beowulf и Paradise Lost»).

    Cyrillic stems (русск, немецк, ...) still substring-match because they
    cover natural Russian morphology (немецк-ий/-ого/-ому/-ом/-их/-ие).
    """
    s = text.lower()
    for k, v in COUNTRY_ALIASES.items():
        # Cyrillic / non-ASCII stem → keep substring match (covers Russian
        # declensions: русск-ий, русск-ого, etc.)
        if any(ord(ch) > 127 for ch in k):
            if k in s:
                return v
            continue
        # Latin-script alias → require word boundaries to keep `german`
        # from matching `germanic`.
        if re.search(rf"\b{re.escape(k)}\b", s):
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
    # Allow a single intervening adjective: «100 любимых слов»,
    # «50 favourite phrases», «20 archaic words».
    r"(\d{1,4})\s+(?:\w+\s+)?(?:слов\w*|words?|примеров|examples?|"
    r"фраз\w*|выраж\w*|phrases))\b",
    re.IGNORECASE,
)


# Sprint 20 — Stan «сто любимых слов Дойла» (2026-05-19). Russian
# numeral *words* in nominative/genitive/accusative case + common
# English numeral words. Stops at obvious magnitudes — finer-grained
# (двадцать пять / twenty-five) isn't worth the regex tower; users
# wanting precise N usually type digits.
_NUMERAL_WORDS: dict[str, int] = {
    # RU — base + genitive endings the parser sees in queries
    "десять": 10, "десяти": 10,
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "сто": 100, "ста": 100,
    "двести": 200, "двухсот": 200,
    "триста": 300, "трёхсот": 300, "трехсот": 300,
    "пятьсот": 500, "пятисот": 500,
    "тысяча": 1000, "тысячу": 1000, "тысячи": 1000,
    # EN
    "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "hundred": 100, "two hundred": 200, "five hundred": 500,
    "thousand": 1000,
}

# Word-boundary regex for the numeral-word path. Longest-first so
# «two hundred» wins over «two» alone. Allows one optional adjective
# between numeral and noun («сто любимых слов», «fifty favorite words»).
_NUMERAL_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(
        (re.escape(k) for k in _NUMERAL_WORDS),
        key=len, reverse=True,
    )) + r")\s+(?:\w+\s+)?(?:слов\w*|words?|примеров|examples?)\b",
    re.IGNORECASE,
)


def _find_top_n(text: str) -> int | None:
    """Extract a top_n hint from the query.

    Tries digit-based patterns first («топ-N», «N слов»), then falls
    back to numeral-word patterns («сто слов», «two hundred words»).
    Caps at 1000 — anything larger is almost certainly a typo or a
    long tail the user doesn't actually want to render.
    """
    m = _TOPN_RE.search(text)
    if m:
        for g in m.groups():
            if g:
                try:
                    n = int(g)
                    return min(max(n, 1), 1000)
                except ValueError:
                    continue
    m2 = _NUMERAL_WORD_RE.search(text)
    if m2:
        key = m2.group(1).lower()
        n = _NUMERAL_WORDS.get(key)
        if n is not None:
            return min(max(n, 1), 1000)
    return None


# ---------- top_metric (для top_authors_by) ----------
# Q5 (Stan's 2026-05-18 demon round): «топ-5 британских авторов по
# скачиваниям» raw-extract'нулся в `top_n=5, country=GB` без указания
# метрики, plan дёргал `top_authors_by_country` дефолт metric=books,
# и сортировал ответ по количеству книг при наличии downloads колонки.
# Этот extractor вытаскивает явное намерение пользователя по сортировке.
_METRIC_TRIGGERS = {
    "downloads": ("скачивани", "downloads", "скачк", "popular", "популярн"),
    "tokens":    ("токен", "tokens", "размер", "объём", "богатств",
                  "словарн", "размеру словар", "vocabulary size"),
    "books":     ("количеств\\w+\\s+книг", "количество\\s+произвед",
                  "books?\\s+count", "по\\s+числу\\s+книг"),
}


def _find_top_metric(text: str) -> str | None:
    s = text.lower()
    for metric, keys in _METRIC_TRIGGERS.items():
        for k in keys:
            if re.search(rf"\b{k}", s):
                return metric
    return None


# ---------- main API ----------

# Sprint 19+ — role-play preamble stripper. Stan 2026-05-19:
#   «я готический сомелье, готовлю меню на ночь чтения. начнём с
#    аперитива — какие архаизмы у Walpole в Castle of Otranto?»
# The actual question is at the end; the role-play preamble adds 100+
# chars of noise that confuses entity extractors. Detect «я <role>»
# / «представь, что я <X>» / «as a <role>» openers + everything up to
# an em-dash or final-sentence boundary, and feed only the trailing
# substantive question to extractors.
_ROLEPLAY_PREAMBLE = re.compile(
    r"^\s*("
    r"я\s+\w[\w\s,]{1,150}?[—\-:]\s*|"            # «я X — actual question»
    r"я\s+\w[\w\s,]{1,80}?\.\s+\w[\w\s,]{1,80}?[—\-:]\s*|"  # «я X. Y — question»
    r"представь(?:те)?,?\s+что\s+(?:я|ты)\s+[\w\s,]{1,80}?[—\-:.]\s*|"
    r"as\s+a\s+\w[\w\s,]{1,80}?[—\-:,]\s*|"
    r"pretend\s+(?:you\s+are|i\s+am)\s+[\w\s,]{1,80}?[—\-:,]\s*"
    r")",
    re.IGNORECASE,
)


def _extract_attribution_passage(text: str) -> str | None:
    """Return a quoted passage suitable for author attribution / quote
    lookup, or None.

    Stan 2026-05-19: «угадай автора отрывка "the fog came pouring in..."»
    fell to clarify «вставь сам текст (хотя бы 500 слов)» because the
    extractor didn't capture the in-line quoted excerpt. Now we capture
    quoted strings >= 30 chars and >= 5 words as attribution_text. Plan
    builder routes <200-word passages to lexical_search (quote lookup),
    ≥200-word to Burrows Delta (real stylometry).

    Skips short single-word quotes (handled by _find_word) and titles
    matched via KNOWN_BOOKS (those have separate book_id flow)."""
    if not text or len(text) < 40:
        return None
    # Find quoted spans — accept «...», "...", "...", '...'
    quote_patterns = [
        ("«", "»"),
        ('"', '"'),
        ("“", "”"),   # curly double
        ("‘", "’"),   # curly single
    ]
    candidates: list[str] = []
    for open_q, close_q in quote_patterns:
        i = 0
        while True:
            start = text.find(open_q, i)
            if start < 0:
                break
            end = text.find(close_q, start + 1)
            if end < 0:
                break
            inner = text[start + 1:end].strip()
            i = end + 1
            # Filter: long enough to be a passage, not just a word/title
            if len(inner) < 30:
                continue
            word_count = len(inner.split())
            if word_count < 5:
                continue
            # Avoid catching book titles — if the entire inner is in
            # KNOWN_BOOKS, it's a title, not an attribution passage.
            if inner.lower() in KNOWN_BOOKS:
                continue
            candidates.append(inner)
    if not candidates:
        return None
    # Pick the longest one — best signal for both lookup and stylometry.
    return max(candidates, key=len)


def _strip_roleplay_preamble(text: str) -> str:
    """Return text with the role-play preamble removed (if any).

    Keeps the original text untouched at the call-site — caller
    decides whether to use stripped (entity extractors) or original
    (intent classification, raw_misc)."""
    if not text:
        return text
    m = _ROLEPLAY_PREAMBLE.match(text)
    if m:
        stripped = text[m.end():].strip()
        # Sanity: stripped should be a real question, not empty / 1-word
        if len(stripped) >= 8:
            return stripped
    return text


def extract(text: str) -> Entities:
    """Pull every entity from the query. Missing fields stay None."""
    if not text or not text.strip():
        return Entities()
    # Sprint 19+ — strip role-play preamble before entity extraction.
    # Intent classifier still sees the full text (keyword triggers like
    # «архаизмы» often live in the substantive trailing question,
    # which strip preserves; but if a trigger sits ONLY in the
    # preamble, that's the user's fault for ambiguous phrasing).
    text = _strip_roleplay_preamble(text)

    author_hits = _find_authors(text)
    primary_author = author_hits[0] if author_hits else (None, None)
    multi = [r for _, r in author_hits[1:]] if len(author_hits) > 1 else []

    book_id, book_title = _find_book(text)
    # Sprint 17 — collect all books mentioned (for «X или Y» compare).
    # Primary stays in book_id/book_title; secondaries go into the multi_*
    # fields. We dedupe against the primary so the same book doesn't
    # appear in both slots.
    all_books = _find_all_books(text)
    multi_book_ids: list[str | None] = []
    multi_book_titles: list[str] = []
    for pg, canon in all_books:
        # Skip the primary (already captured)
        if pg and pg == book_id:
            continue
        if canon and canon == book_title:
            continue
        multi_book_ids.append(pg)
        multi_book_titles.append(canon)

    year_from, year_to = _find_year_range(text)

    word = _find_word(text)
    # When the same token is also part of a resolved book title, drop it as
    # a "word" — it was just the first noun of the title that the quoted-
    # word regex hijacked. Q19 «"Alice's Adventures in Wonderland"» used to
    # yield word="alice"; we want None so the planner picks a default.
    if word and book_title and word.lower() in book_title.lower():
        word = None

    # Sprint 19+ — capture long quoted passage for attribution. Stan
    # 2026-05-19: «угадай автора отрывка "the fog came pouring in..."»
    # — passage was inside quotes, but extractor didn't surface it. v1
    # author_attribution required ≥500 words and the user expected
    # quote-lookup behaviour. Now: detect quoted passages >30 chars,
    # store as attribution_text so plan can route to lexical search.
    attribution_text = _extract_attribution_passage(text)

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
        top_metric=_find_top_metric(text),
        multi_author_regex=multi,
        multi_book_ids=multi_book_ids,
        multi_book_titles=multi_book_titles,
        # Sprint 20+ B4 + B9: language hint + exclude_archaic flag
        lang_hint=_find_lang_hint(text),
        exclude_archaic=_find_exclude_archaic(text),
        # Sprint 20+ B3: export-format hint for export_word_list followups
        export_format=_find_export_format(text),
        raw_misc={"raw_text": text,
                  **({"attribution_text": attribution_text}
                     if attribution_text else {})},
    )


# ---------- Sprint 20+ — lang_hint + exclude_archaic ----------


_LANG_HINT_PATTERNS = (
    # English literature context — Stan Round 11 B4
    # «английская классика», «english literature», «in English»
    (re.compile(
        r"\b(?:английск\w+|англоязычн\w+|english(?:[\s-]language)?|"
        r"in\s+english|на\s+английском)\b",
        re.IGNORECASE,
    ), "en"),
    # Russian context — for explicitly Russian-corpus queries. The
    # alternation stems may end on a Cyrillic letter, so we can't use
    # trailing `\b` (Python \b between two Cyrillic word chars never
    # matches). Allow optional `\w*` to consume the inflection tail.
    (re.compile(
        r"\b(?:русск\w+\s+(?:литератур|классик|книг|автор|поэз)\w*|"
        r"russian\s+(?:literature|classics?|books?)|"
        r"на\s+русском(?:\s+языке)?)",
        re.IGNORECASE,
    ), "ru"),
    # Add other languages as observed
    (re.compile(
        r"\b(?:французск\w+\s+(?:литератур|классик)\w*|"
        r"french\s+(?:literature|classics?))",
        re.IGNORECASE,
    ), "fr"),
)


def _find_lang_hint(text: str) -> str | None:
    """Detect explicit language filter from the query («английская
    классика» / «english literature»). Used by lexical_search and
    find_book_by_topic to pass `lang='en'` etc, preventing the «имена
    в английской классике» bug where lexical_search returned Finnish /
    Hungarian / Italian results because language wasn't filtered.
    """
    if not text:
        return None
    for pat, lang in _LANG_HINT_PATTERNS:
        if pat.search(text):
            return lang
    return None


_EXCLUDE_ARCHAIC_PATTERNS = re.compile(
    r"\bбез\s+архаизм\w*|"
    r"\bне\s+архаичн\w+|"
    r"\bno\s+archaic|"
    r"\bexclude\s+archaic|"
    r"\bсовременн\w+\s+язык|"
    r"\bmodern\s+(?:english|language)",
    re.IGNORECASE,
)


def _find_exclude_archaic(text: str) -> bool:
    """Detect «без архаизмов» / «no archaic» — used by book_recommendation
    to filter out books with high archaic_density. Stan Round 11 B9:
    «B2 без архаизмов» — filter ignored, returned Pliny / Roman Stoicism."""
    if not text:
        return False
    return bool(_EXCLUDE_ARCHAIC_PATTERNS.search(text))


# ---------- Sprint 20+ B3 — export-format detection ----------
# Round 11 beginner-researcher test: «выгрузи в anki», «csv pls»,
# «дай в markdown» / «json plz» — followups after a word-list turn.
# Was: classified as clarify (no matching rule), now: export_word_list.

_EXPORT_FORMAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anki — TSV for word-card import. Most common request.
    (re.compile(r"\banki\b", re.IGNORECASE), "anki"),
    # CSV — generic spreadsheet
    (re.compile(r"\bcsv\b|\bspreadsheet\b|\bexcel\b|\bтаблиц\w+", re.IGNORECASE),
     "csv"),
    # JSON — for programmatic consumers
    (re.compile(r"\bjson\b", re.IGNORECASE), "json"),
    # Markdown — for note-taking apps (Obsidian, Notion, etc.)
    (re.compile(r"\bmarkdown\b|\bmd\b|\.md\b|"
                r"\bобсидиан\w*|\bobsidian\b|\bnotion\b",
                re.IGNORECASE), "markdown"),
    # Plain TSV — when user says «tab separated» / «tsv» without anki
    (re.compile(r"\btsv\b|\btab[\s\-]separat\w+", re.IGNORECASE), "tsv"),
)


def _find_export_format(text: str) -> str | None:
    """Return canonical format token when text mentions an export target.

    Recognized:
      anki      — TSV optimized for Anki desktop card-import
      csv       — comma-separated for spreadsheets
      json      — array of word objects
      markdown  — pipe-table for note-taking apps
      tsv       — bare tab-separated, no Anki conventions
    """
    if not text:
        return None
    for pat, fmt in _EXPORT_FORMAT_PATTERNS:
        if pat.search(text):
            return fmt
    return None
