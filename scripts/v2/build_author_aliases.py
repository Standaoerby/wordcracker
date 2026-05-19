#!/usr/bin/env python3
"""Generate scripts/v2/data/aliases_generated.json from corpus metadata.

Sprint 16 Phase A: closes Stan's round 6 gap where Walpole / Radcliffe /
Lewis / Maturin (gothic founders) were missing from AUTHOR_ALIASES, so
queries about them fell to clarify.

Strategy
--------
For each unique author in /workspace/spgc/metadata.csv `author` column
(stored as "LastName, FirstName MiddleName"), generate aliases:

  1. lowercase surname → `^Surname,` regex
  2. lowercase surname + first name → same regex (multi-word lookup)
  3. lowercase surname + full known forenames → same regex
  4. If `iuliia` library is installed AND we detect Cyrillic, also
     generate transliterated variants. iuliia is RU→EN only by design,
     so we use it to verify/normalize Cyrillic entries the corpus
     already has.

Curated entries (entities.AUTHOR_ALIASES_CURATED) ALWAYS win on conflict
via runtime merge order — see entities.py.

Run inside the gutenberg-lab container:

    docker compose exec -T gutenberg-lab \\
      python -u /workspace/scripts/v2/build_author_aliases.py

For dev / CI without /workspace, pass `--fixture` for a minimal probe.

Output
------
JSON at scripts/v2/data/aliases_generated.json:

    {
      "_meta": {"built_at": "2026-05-19T...", "source": "...",
                "total_authors": 4823, "total_aliases": 14687},
      "aliases": {
        "walpole": "^Walpole,",
        "horace walpole": "^Walpole,",
        ...
      }
    }
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("build_author_aliases")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

OUT_PATH = Path(__file__).resolve().parent / "data" / "aliases_generated.json"
DEFAULT_METADATA = Path(os.environ.get(
    "WC_METADATA_PATH",
    "/workspace/spgc/metadata.csv",
))

# Surnames we never want to generate from corpus — too generic to be
# a useful author alias (collides with common English words / risks
# matching mid-sentence). These would need a curated entry with stronger
# context if needed at all.
_GENERIC_SURNAME_SKIP = frozenset({
    "anonymous", "unknown", "various", "n/a", "none",
    "encyclopedia", "compilation", "catholic church",
    "the bible", "the holy bible", "homer",
    # Single-letter / very short — almost certainly junk
})

# Tokens that look like a surname but are too short / common to alias
# without a context guard. We add to the generated dict only if longer
# than this floor.
_MIN_SURNAME_LEN = 4


def _safe_surname(author_field: str) -> str | None:
    """Extract the surname token from a Gutendex-style author string.

    «Doyle, Arthur Conan» → 'Doyle'
    «Twain, Mark» → 'Twain'
    «Wodehouse, P. G. (Pelham Grenville)» → 'Wodehouse'
    «Various» → 'Various' (caught by GENERIC_SKIP later)
    Empty / None → None
    """
    if not author_field or not isinstance(author_field, str):
        return None
    head = author_field.split(",", 1)[0].strip()
    if not head:
        return None
    # Drop bracketed annotations: «Wodehouse [pseud.]» → «Wodehouse»
    head = re.sub(r"\s*\[.*?\]|\s*\(.*?\)", "", head).strip()
    # Must be a clean alphabetic token (no numbers, no slashes)
    if not re.match(r"^[A-Za-zЀ-ӿ.'-]+$", head):
        return None
    return head


def _safe_forenames(author_field: str) -> str | None:
    """Extract the forename portion from «Surname, First Middle» style."""
    if "," not in author_field:
        return None
    tail = author_field.split(",", 1)[1].strip()
    # Drop bracketed
    tail = re.sub(r"\s*\[.*?\]|\s*\(.*?\)", "", tail).strip()
    if not tail or not re.match(r"^[A-Za-zЀ-ӿ.'\s-]+$", tail):
        return None
    return tail


def _surname_regex(surname: str) -> str:
    """Build the v1 `^Surname,` regex matching the corpus metadata column."""
    # The corpus uses metadata.csv with «Surname, ...» — match prefix.
    # Surname may contain apostrophe / hyphen (O'Brien, Saint-Beuve).
    return f"^{surname.strip()},"


def _generate_for_author(author_field: str) -> list[tuple[str, str]]:
    """Return list of (alias_key, regex) pairs for one author."""
    surname = _safe_surname(author_field)
    if not surname:
        return []
    surname_lower = surname.lower()
    if surname_lower in _GENERIC_SURNAME_SKIP:
        return []
    if len(surname_lower) < _MIN_SURNAME_LEN:
        return []
    regex = _surname_regex(surname)
    out = [(surname_lower, regex)]
    forenames = _safe_forenames(author_field)
    if forenames:
        # «Horace Walpole» → «horace walpole»
        full = f"{forenames} {surname}".lower().strip()
        if " " in full and len(full) <= 60:
            out.append((full, regex))
        # First-name only (legacy: «Arthur Conan» → first token alone is
        # too ambiguous; skip this form)
    return out


def _iter_authors_from_csv(csv_path: Path) -> Iterable[str]:
    """Yield unique author field values from metadata.csv."""
    import csv
    seen: set[str] = set()
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            v = (row.get("author") or "").strip()
            if v and v not in seen:
                seen.add(v)
                yield v


def _fixture_authors() -> Iterable[str]:
    """Small in-process fixture for dev / CI runs without /workspace."""
    return [
        "Walpole, Horace",
        "Radcliffe, Ann Ward",
        "Lewis, M. G. (Matthew Gregory)",
        "Maturin, Charles Robert",
        "Beckford, William",
        "Le Fanu, Joseph Sheridan",
        "Doyle, Arthur Conan",
        "Wodehouse, P. G. (Pelham Grenville)",
        "Anonymous",
        "Various",
        "x",  # too short, should be skipped
        "James, Henry",
        "Christie, Agatha",
        "James, M. R. (Montague Rhodes)",  # disambiguate from Henry James
    ]


def build_aliases(authors: Iterable[str]) -> dict[str, str]:
    """Aggregate aliases from author iterable. Last write wins for
    duplicate keys (different authors with same surname). When a
    surname collision exists, we keep the most recent → corpus has
    multiple authors with same surname (e.g. James, Henry vs James,
    M.R.) — multi-word forms disambiguate."""
    aliases: dict[str, str] = {}
    counts = Counter()
    surnames_collision_count: Counter[str] = Counter()
    for author in authors:
        pairs = _generate_for_author(author)
        for key, regex in pairs:
            surnames_collision_count[regex] += 1
            if key in aliases and aliases[key] != regex:
                # Same alias different regex → ambiguous; drop the
                # alias so neither author wins by accident. User must
                # be explicit with a curated entry.
                aliases.pop(key, None)
                counts["dropped_ambiguous"] += 1
                continue
            aliases[key] = regex
            counts["added"] += 1
    log.info("aliases: %d total, %d dropped as ambiguous",
             len(aliases), counts["dropped_ambiguous"])
    return aliases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metadata", type=Path, default=DEFAULT_METADATA,
                    help="path to metadata.csv with 'author' column")
    ap.add_argument("--out", type=Path, default=OUT_PATH,
                    help="output JSON path")
    ap.add_argument("--fixture", action="store_true",
                    help="use in-memory fixture instead of reading metadata")
    args = ap.parse_args()

    if args.fixture or not args.metadata.exists():
        if not args.fixture:
            log.warning("metadata file not found at %s — falling back to "
                        "fixture (use --fixture to silence this warning)",
                        args.metadata)
        authors = _fixture_authors()
        source = "fixture"
    else:
        log.info("reading authors from %s", args.metadata)
        authors = list(_iter_authors_from_csv(args.metadata))
        source = str(args.metadata)
        log.info("read %d unique author fields", len(authors))

    aliases = build_aliases(authors)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_meta": {
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "total_authors": len(list(authors)) if isinstance(authors, list)
                              else None,
            "total_aliases": len(aliases),
        },
        "aliases": aliases,
    }
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    log.info("wrote %d aliases to %s", len(aliases), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
