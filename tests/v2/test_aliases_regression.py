"""Auto-regression for every curated author alias.

For each entry in `AUTHOR_ALIASES_CURATED`, asserts that
`extract(f"слова у {alias}")` returns the matching `^Surname,` regex.
Adding a new alias automatically gets a regression check — no extra
test code required.

Skipped for «по» (ambiguous preposition, see test_history.py
`PoPrepositionCollision`) and for multi-word aliases that don't fit
the «слова у X» frame ungracefully — those have hand-written tests
elsewhere.

Sprint 16 / v3.0 — Phase H2 from docs/v2/PLUGIN.md."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.planner.entities import AUTHOR_ALIASES_CURATED, extract


# Aliases that need special handling and are tested elsewhere
# (preposition collisions, deliberate stems matching root tokens, etc.).
_SKIP_AUTO_REGRESSION = {
    "по",        # see test_history.PoPrepositionCollision (preposition guard)
    "morris",    # ambiguous English surname (multiple Morrises)
    "льюис",     # M.G. Lewis vs C.S. Lewis vs Lewis Carroll — manual
    "lewis",
    "wilde",     # short — verified by curated probe
    # Phase 0 (2026-05-22) — v6 layered linker is the default and
    # disambiguates these aliases to the prominent canonical (H. G. Wells)
    # rather than the bare `^Wells,` surname. v6 behavior is the intent
    # (E13 closes over-eager surname disambig); covered by
    # test_entity_resolver_v6.py.
    "уэллс",
    "h. g. wells",
}


# Special frames per alias when «слова у X» is awkward. Default frame =
# «слова у <alias>». Multi-word aliases / ones starting with «эдгар» etc.
# use this overrides table.
_FRAMES: dict[str, str] = {
    "edgar allan poe": 'фирменные слова Edgar Allan Poe',
    "конан дойл":      'фирменные слова Конан Дойла',
    "конан дойль":     'фирменные слова Конан Дойль',
    "джонатан свифт":  'фирменные слова Джонатан Свифт',
    "уильям моррис":   'фирменные слова Уильям Моррис',
    "льюис кэрролл":   'фирменные слова Льюис Кэрролл',
    "h. g. wells":     'фирменные слова H. G. Wells',
}


class CuratedAliasExtraction(unittest.TestCase):
    """One sub-test per curated alias. Failure isolates which entry broke."""

    def test_every_curated_alias_extracts(self):
        misses: list[tuple[str, str, str | None]] = []
        for alias, expected_regex in AUTHOR_ALIASES_CURATED.items():
            if alias in _SKIP_AUTO_REGRESSION:
                continue
            frame = _FRAMES.get(alias, f"фирменные слова у {alias}")
            with self.subTest(alias=alias, regex=expected_regex):
                got = extract(frame).author_regex
                if got != expected_regex:
                    misses.append((alias, expected_regex, got))
        # Aggregated report — easier to triage than 80 individual failures
        if misses:
            lines = ["misses (alias → expected vs got):"]
            for alias, exp, got in misses[:30]:
                lines.append(f"  {alias!r}  exp={exp!r}  got={got!r}")
            self.fail("\n".join(lines))

    def test_no_alias_collides_with_obvious_preposition(self):
        """Cyrillic stems shorter than 4 chars are risky — could collide
        with prepositions/particles like «у», «при», «о», «над». Catch
        any new ones before they bite (see «по» preposition collision
        round 5)."""
        from scripts.v2.planner.entities import _AMBIGUOUS_SHORT_ALIASES
        risky = []
        for alias in AUTHOR_ALIASES_CURATED:
            if (len(alias) <= 2 and
                    alias not in _AMBIGUOUS_SHORT_ALIASES and
                    all('а' <= c.lower() <= 'я' or c.lower() == 'ё'
                        for c in alias if c.isalpha())):
                risky.append(alias)
        self.assertFalse(
            risky,
            msg=("short Cyrillic aliases without a preposition-collision "
                 f"guard: {risky}. Add to _AMBIGUOUS_SHORT_ALIASES in "
                 "entities.py with appropriate suppression logic, OR add "
                 "to _SKIP_AUTO_REGRESSION above with comment why this "
                 "is safe."))


class GeneratedAliasesLoad(unittest.TestCase):
    """Verify the generated-aliases layer loads without crashing even
    when the file is missing/malformed (Sprint 16 Phase H, foundation
    for Phase A)."""

    def test_load_missing_file_returns_empty(self):
        from scripts.v2.planner.entities import _load_generated_aliases
        # When data/aliases_generated.json doesn't exist yet (Phase A
        # hasn't run), the loader returns {} and the runtime AUTHOR_ALIASES
        # dict equals AUTHOR_ALIASES_CURATED.
        result = _load_generated_aliases()
        self.assertIsInstance(result, dict)
        # Either empty (Phase A not run) or all values match `^X,` shape
        import re
        for k, v in result.items():
            self.assertIsInstance(k, str)
            self.assertTrue(re.match(r"^\^[A-Za-zЀ-ӿ'-]+,?$", v),
                             msg=f"bad generated alias: {k}={v}")

    def test_curated_always_wins(self):
        """Sanity: curated dict is the source of truth — merge order
        must keep its entries even if generated has overlapping key."""
        from scripts.v2.planner.entities import (
            AUTHOR_ALIASES, AUTHOR_ALIASES_CURATED,
        )
        for k, v in AUTHOR_ALIASES_CURATED.items():
            self.assertEqual(AUTHOR_ALIASES.get(k), v,
                             msg=f"curated {k!r} lost to generated in merge")


if __name__ == "__main__":
    unittest.main(verbosity=2)
