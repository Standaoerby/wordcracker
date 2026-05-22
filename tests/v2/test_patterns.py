"""Phase 3 regex harness — iterate the patterns registry.

Gate per REFACTOR_BRIEF Phase 3:
  * Every CompiledPattern in PATTERNS has ≥2 positives and ≥2 negatives.
  * Every positive matches (finditer ≥ 1 hit).
  * Every negative does NOT match (finditer == 0 hits).
  * Every TokenSet in TOKEN_SETS has ≥2 positives and ≥2 negatives.
  * Every positive token IS in the set.
  * Every negative token is NOT in the set.

Add a pattern without negatives → this test fails. That is the structural
guarantee — Rule R5 enforced as a test, not a code review.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.patterns import PATTERNS, TOKEN_SETS


class PatternRegistryGate(unittest.TestCase):

    def test_every_pattern_has_two_positives_and_two_negatives(self):
        for name, p in PATTERNS.items():
            with self.subTest(pattern=name):
                self.assertGreaterEqual(
                    len(p.positives), 2,
                    f"{name}: needs ≥2 positive cases",
                )
                self.assertGreaterEqual(
                    len(p.negatives), 2,
                    f"{name}: needs ≥2 negative cases (R5 — без негативного "
                    f"кейса правка паттерна не принимается)",
                )

    def test_every_positive_matches(self):
        for name, p in PATTERNS.items():
            for case in p.positives:
                with self.subTest(pattern=name, case=case):
                    hits = list(p.pattern.finditer(case))
                    self.assertGreaterEqual(
                        len(hits), 1,
                        f"{name}: positive {case!r} did not match",
                    )

    def test_every_negative_does_not_match(self):
        for name, p in PATTERNS.items():
            for case in p.negatives:
                with self.subTest(pattern=name, case=case):
                    hits = list(p.pattern.finditer(case))
                    self.assertEqual(
                        len(hits), 0,
                        f"{name}: negative {case!r} matched {hits!r} — "
                        f"the case that motivated the fix must keep failing",
                    )


class TokenSetRegistryGate(unittest.TestCase):

    def test_every_token_set_has_two_positives_and_two_negatives(self):
        for name, ts in TOKEN_SETS.items():
            with self.subTest(token_set=name):
                self.assertGreaterEqual(len(ts.positives), 2,
                                        f"{name}: needs ≥2 positives")
                self.assertGreaterEqual(len(ts.negatives), 2,
                                        f"{name}: needs ≥2 negatives")

    def test_every_positive_is_in_set(self):
        for name, ts in TOKEN_SETS.items():
            for tok in ts.positives:
                with self.subTest(token_set=name, token=tok):
                    self.assertIn(
                        tok, ts.tokens,
                        f"{name}: expected {tok!r} ∈ set",
                    )

    def test_every_negative_is_not_in_set(self):
        for name, ts in TOKEN_SETS.items():
            for tok in ts.negatives:
                with self.subTest(token_set=name, token=tok):
                    self.assertNotIn(
                        tok, ts.tokens,
                        f"{name}: did not expect {tok!r} ∈ set — "
                        f"first-name disambiguator must NOT be a stop-word",
                    )


class NoDuplicateDefinitionsGate(unittest.TestCase):
    """The audit-flagged duplicates must not return.

    For each registry entry, walk scripts/v2 and confirm there is no
    second `re.compile(...)` / `frozenset(...)` literal that re-defines
    the same thing. Catches a future drive-by copy-paste before review.
    """

    def test_canonical_format_re_defined_once(self):
        from scripts.v2.patterns.registry import CANONICAL_FORMAT_RE
        # The exact pattern string used in the registry.
        sig = CANONICAL_FORMAT_RE.pattern
        hits = self._scan_scripts_v2_for_literal(sig)
        # Allowed: the registry itself.
        allowed = {Path("scripts/v2/patterns/registry.py").as_posix()}
        rogue = [h for h in hits if h.as_posix() not in allowed]
        self.assertEqual(
            rogue, [],
            f"CANONICAL_FORMAT_RE pattern string appears in multiple "
            f"files — must live in patterns/registry.py only. Rogue: "
            f"{[r.as_posix() for r in rogue]}",
        )

    def test_try_rapidfuzz_defined_once(self):
        # Allowed callers: the registry helper + the legacy alias in
        # entity_resolver.py (which delegates to it).
        from_rapidfuzz_lines = self._scan_scripts_v2_for_literal(
            "from rapidfuzz import fuzz, process",
        )
        allowed = {Path("scripts/v2/patterns/helpers.py").as_posix()}
        rogue = [h for h in from_rapidfuzz_lines if h.as_posix() not in allowed]
        self.assertEqual(
            rogue, [],
            f"`from rapidfuzz import fuzz, process` appears outside "
            f"patterns/helpers.py — try_rapidfuzz() is the single source. "
            f"Rogue: {[r.as_posix() for r in rogue]}",
        )

    @staticmethod
    def _scan_scripts_v2_for_literal(needle: str) -> list[Path]:
        root = Path(__file__).resolve().parents[2] / "scripts" / "v2"
        hits: list[Path] = []
        for py in root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if needle in text:
                rel = py.relative_to(root.parent.parent)
                hits.append(rel)
        return hits


if __name__ == "__main__":
    unittest.main()
