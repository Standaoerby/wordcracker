"""Surname blocklist for affinity outputs.

Stan 2026-05-19: top of Conan Doyle's «фирменные слова» list was dominated
by character surnames — challenger, knolles, barrymore, holmes, flannigan,
stapleton, mcfarlane, baumgarten. These bypass:

  1. Corpus-diff heuristic — names appearing in multiple authors' books
     (e.g. holmes/barrymore in Doyle + Oliver Wendell Holmes essays +
     Lionel Barrymore biographies) have non-trivial corpus_count and pass
     the «corpus_count - author_count >= max(10, author_count*0.5)» test.
  2. spaCy POS PROPN drop — when fed isolated lowercased tokens, spaCy
     unreliably tags ambiguous strings: «holmes»/«challenger»/«burger»
     come back as NOUN, not PROPN.
  3. word_dictionary proper_noun flag — only filled for words seen in
     previous learning_words runs.

The clean fix is a *positive* surname signal: words that match an
established surname (from PG author metadata or curated literary
characters) shouldn't appear in stylistic-marker lists.

Layered architecture:
  - `_curated_character_surnames()` — hand-maintained ~150 fictional
    character surnames from the Western canon (Sherlock universe, Dickens,
    Austen, Hardy, Tolstoy, etc.). Ships in code, immediate fix.
  - `_pg_author_surnames()` — mtime-cached load of SPGC metadata.csv
    `author` column ("Surname, Forename" → "surname"). On the server this
    yields ~10k surnames; locally returns empty set without crashing.
  - `surname_blocklist()` — union, lowercase, frozen.

If a real stylistic word happens to also be a common surname (e.g.
"smith"/"cooper"/"baker"), it gets dropped — we err on the side of
silencing noise. Stan's signature-word use case treats this as acceptable.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

# Curated literary character surnames — covers the most common bleeds
# we've observed. Add liberally; the cost of over-blocking a real lexeme
# is one less signature word, the cost of under-blocking is the kind of
# garbage Stan saw on 2026-05-19.
_CURATED_CHARACTER_SURNAMES: frozenset[str] = frozenset({
    # Doyle universe (Sherlock + Challenger + Brigadier Gerard + Sir Nigel)
    "holmes", "watson", "mycroft", "lestrade", "moriarty", "moran",
    "adler", "hudson", "gregson", "athelney", "stamford", "barrymore",
    "stapleton", "selden", "frankland", "mortimer", "rucastle", "openshaw",
    "milverton", "musgrave", "trevor", "hatherley", "neville", "stoner",
    "roylott", "challenger", "summerlee", "malone", "roxton", "knolles",
    "alleyne", "samkin", "loring", "navarre", "tristram", "calvert",
    "mcfarlane", "baumgarten", "hastie", "flannigan", "gerard", "etienne",
    "huxtable", "wilder", "saltire",
    # Dickens
    "pickwick", "tupman", "snodgrass", "winkle", "weller", "pecksniff",
    "chuzzlewit", "nickleby", "squeers", "copperfield", "micawber",
    "uriah", "heep", "peggotty", "steerforth", "dorrit", "gradgrind",
    "bounderby", "twist", "fagin", "sikes", "bumble", "havisham", "pip",
    "estella", "magwitch", "darnay", "manette", "defarge", "scrooge",
    "cratchit", "tiny tim", "marley", "skimpole", "jarndyce", "skewton",
    # Phase 3 W-4 (2026-05-22) — characters that dominated «слова Диккенса»
    # top-list, leaking through prior filters because the surname is
    # uncommon enough that PG metadata doesn't carry it as a real author.
    "wegg",      # Silas Wegg — Our Mutual Friend
    "smike",     # Nicholas Nickleby
    "toots",     # Mr Toots — Dombey and Son
    "jip",       # Dora's dog — David Copperfield (yes, even pets bleed)
    "barkis",    # Mr Barkis — David Copperfield
    "trotwood",  # Betsey Trotwood — David Copperfield
    "murdstone", # Edward Murdstone — David Copperfield
    "creakle",   # Mr Creakle — David Copperfield
    "dombey",    # Paul Dombey — Dombey and Son
    "carker",    # Mr Carker — Dombey and Son
    "boffin",    # Nicodemus Boffin — Our Mutual Friend
    "wrayburn",  # Eugene Wrayburn — Our Mutual Friend
    "hexam",     # Lizzie/Charley Hexam — Our Mutual Friend
    "headstone", # Bradley Headstone — Our Mutual Friend
    "venus",     # Mr Venus — Our Mutual Friend (taxidermist)
    "podsnap",   # Mr Podsnap — Our Mutual Friend
    "lammle",    # Lammle — Our Mutual Friend
    "veneering", # Veneering — Our Mutual Friend
    "twemlow",   # Twemlow — Our Mutual Friend
    "rouncewell",# Rouncewell — Bleak House
    "tulkinghorn",# Mr Tulkinghorn — Bleak House
    "dedlock",   # Sir Leicester Dedlock — Bleak House
    "snagsby",   # Mr Snagsby — Bleak House
    "guppy",     # Mr Guppy — Bleak House
    "krook",     # Mr Krook — Bleak House
    "smallweed", # Smallweed — Bleak House
    "bagnet",    # Bagnet — Bleak House
    "skewton",
    "edwin",     # Edwin Drood — The Mystery of Edwin Drood
    "drood",
    "jasper",    # John Jasper — Edwin Drood
    "datchery",  # Dick Datchery — Edwin Drood
    # Phase 3 W-4 reconciliation with tz_claude_code_fixes_2026-05-22 —
    # additional Dickens characters Stan flagged in «характерные слова
    # Диккенса»:
    "cuttle",    # Captain Cuttle — Dombey and Son
    "claypole",  # Noah Claypole — Oliver Twist
    "bagstock",  # Major Bagstock — Dombey and Son
    "blimber",   # Dr. Blimber — Dombey and Son
    "skettles",  # Lady Skettles — Dombey and Son
    "fledgeby",  # «Fascination» Fledgeby — Our Mutual Friend
    "riderhood", # Roger Riderhood — Our Mutual Friend
    "wilfer",    # Bella Wilfer — Our Mutual Friend
    "noddy",     # Noddy Boffin — Our Mutual Friend
    # Austen
    "darcy", "bingley", "wickham", "collins", "bennet", "elliot",
    "wentworth", "knightley", "woodhouse", "fairfax", "churchill",
    "ferrars", "dashwood", "willoughby", "elinor", "marianne", "tilney",
    "thorpe", "morland", "crawford", "norris", "bertram", "rushworth",
    # Bronte
    "rochester", "fairfax", "eyre", "reed", "brocklehurst", "ingram",
    "heathcliff", "earnshaw", "linton", "nelly", "hindley",
    # Hardy
    "tess", "durbeyfield", "clare", "alec", "henchard", "farfrae",
    "newson", "lucetta", "elizabeth-jane", "knight", "bathsheba",
    "everdene", "oak", "boldwood", "troy",
    # Russian classics
    "raskolnikov", "marmeladov", "porfiry", "svidrigailov", "razumikhin",
    "myshkin", "rogozhin", "nastasya", "filippovna", "karamazov", "smerdyakov",
    "zosima", "alyosha", "ivan", "dmitri", "fyodor", "stavrogin",
    "verkhovensky", "shatov", "kirilov", "bolkonsky", "rostov", "bezukhov",
    "kuragin", "drubetskoy", "denisov", "natasha", "andrei", "pierre",
    "karenina", "vronsky", "oblonsky", "levin", "kitty", "stiva",
    "onegin", "lensky", "tatyana", "olga", "pechorin", "grushnitsky",
    "bazarov", "kirsanov", "fenechka",
    # Wodehouse — already in _LITERARY_PROPN_BLACKLIST but include here too
    "wooster", "jeeves", "psmith", "ukridge", "mulliner", "blandings",
    "threepwood", "emsworth", "wrykyn", "rupert", "spode", "glossop",
    "stanning", "marvis",
    # American (Twain, Melville, Hawthorne, Fitzgerald, Poe characters)
    "huckleberry", "sawyer", "ahab", "ishmael", "queequeg", "starbuck",
    "pequod", "hester", "dimmesdale", "chillingworth", "pearl",
    "gatsby", "buchanan", "carraway", "wilson", "myrtle",
    "ligeia", "morella", "berenice", "usher", "pym", "dupin",
    "fortunato", "montresor", "prospero", "metzengerstein",
    # Lovecraft / Wells / Conrad
    "cthulhu", "nyarlathotep", "azathoth", "yog-sothoth", "shub-niggurath",
    "carter", "danforth", "armitage", "whateley", "marsh", "innsmouth",
    "kurtz", "marlow", "jim", "nostromo", "razumov",
    # Shakespeare protagonists often referenced by surname-style
    "hamlet", "ophelia", "polonius", "laertes", "fortinbras", "horatio",
    "macbeth", "duncan", "banquo", "fleance", "malcolm", "macduff",
    "othello", "iago", "desdemona", "cassio", "lear", "cordelia",
    "regan", "goneril", "gloucester", "edmund", "edgar",
    "prospero", "miranda", "caliban", "ariel", "ferdinand",
    "rosalind", "orlando", "viola", "orsino", "olivia",
    # Misc heavyweight 19th-c characters
    "quasimodo", "esmeralda", "frollo", "valjean", "javert", "cosette",
    "marius", "thenardier", "fantine", "gavroche",
    "monte cristo", "dantes", "danglars", "mercedes", "morrel",
    "athos", "porthos", "aramis", "richelieu", "milady",
})


_PG_CACHE: dict = {"surnames": None, "mtime": 0.0}


def _pg_author_surnames(metadata_path: Path) -> frozenset[str]:
    """Lazy-load + mtime-cache PG author surnames.

    Parses the `author` column of SPGC metadata. The convention there is
    "Surname, Forename Middle" (sometimes with suffixes/honorifics). We
    take the chunk before the first comma, lowercase it, drop anything
    that contains spaces (handles edge-cases like "de la Mare").

    Failure modes:
      - File missing → empty set (server may not have synced yet).
      - Parse error / empty file → empty set.
      - Multi-token surnames ("de la Mare", "Vanha-Niemi") — we keep the
        last word only so partial substring matches against affinity
        results still fire.
    """
    if not metadata_path.exists():
        return frozenset()
    try:
        m = metadata_path.stat().st_mtime
    except OSError:
        return frozenset()
    if _PG_CACHE["surnames"] is not None and _PG_CACHE["mtime"] == m:
        return _PG_CACHE["surnames"]
    surnames: set[str] = set()
    try:
        with open(metadata_path, encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            for row in rd:
                a = (row.get("author") or "").strip()
                if not a or "," not in a:
                    continue
                surname_chunk = a.split(",", 1)[0].strip()
                if not surname_chunk:
                    continue
                # Multi-token: "de la Mare" → also add "mare"; "Conan Doyle"
                # → also add "doyle". Keep individual words ≥3 chars.
                for tok in surname_chunk.lower().split():
                    if len(tok) >= 3 and tok.isalpha():
                        surnames.add(tok)
    except (OSError, csv.Error, UnicodeDecodeError):
        return frozenset()
    out = frozenset(surnames)
    _PG_CACHE["surnames"] = out
    _PG_CACHE["mtime"] = m
    return out


def _default_metadata_path() -> Path:
    """Production SPGC metadata path. Overrideable for tests via the
    `metadata_path` arg on `surname_blocklist()`."""
    return Path("/workspace/spgc/SPGC-metadata-2018-07-18.csv")


def surname_blocklist(metadata_path: Path | None = None) -> frozenset[str]:
    """Union of curated literary character surnames + PG author surnames
    (mtime-cached). Lowercase, frozen.

    Pass `metadata_path` in tests to point at a fixture file; in
    production this defaults to the SPGC metadata location.
    """
    path = metadata_path or _default_metadata_path()
    pg = _pg_author_surnames(path)
    return frozenset(_CURATED_CHARACTER_SURNAMES | pg)


def filter_surnames(rows: list[dict], *,
                    word_key: str = "word",
                    metadata_path: Path | None = None) -> tuple[list[dict], int]:
    """Drop rows whose `word_key` value is a known surname.

    Returns (filtered_rows, dropped_count). The blocklist is union of
    curated character surnames + PG author surnames.
    """
    blocklist = surname_blocklist(metadata_path)
    if not rows or not blocklist:
        return rows, 0
    kept = [r for r in rows
            if (r.get(word_key) or "").lower() not in blocklist]
    return kept, len(rows) - len(kept)


__all__ = [
    "filter_surnames",
    "surname_blocklist",
    "_CURATED_CHARACTER_SURNAMES",
]
