"""Phase 3 gate — named negative cases the REFACTOR_BRIEF calls out.

The brief specifies:
  > Gate: «все паттерны в реестре; негативный кейс
  > test_christopher_marlowe_does_NOT_clarify и test_wells_DOES_clarify
  > оба проходят на живом пути; линт [:N]-усечения зелёный.»

Those two named tests are equivalent to existing cases in
`test_entity_resolver_v6.py` (test_christopher_marlowe_resolves_to_christopher,
test_wells_bare_triggers_clarify). This file binds them under the
brief's exact names so the gate is verifiable by `pytest -k`.

«Live path» = v6 resolver entry `resolve_v6(...)` — the single live
resolver after Phase 1 collapse. The metadata fixture is shared with
`test_entity_resolver_v6._ResolverTestBase` (same prod-shape Wells /
Doyle / Marlowe / Tolstoy mix used by every existing v6 test).
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from scripts.v2 import entity_resolver as er
from scripts.v2.entity_resolver_v6.main import resolve_v6
from scripts.v2.entity_resolver_v6.types import Decision

# Reuse the existing v6 fixture so prod-shape Wells/Marlowe data stays
# in one place. `tests/v2` isn't a Python package — we go through
# sibling-module discovery the same way the rest of the suite does.
from test_entity_resolver_v6 import _fixture_df  # type: ignore  # noqa: E402


class _Phase3GateBase(unittest.TestCase):
    """Local copy of the v6 test setup — fresh metadata mock per test."""

    def setUp(self):
        self._patcher = mock.patch(
            "scripts.rag_tools._metadata_df",
            return_value=_fixture_df(),
        )
        self._patcher.start()
        with er._prom_lock:
            er._prom_state["data"] = None
        er.get_prominence_index(force_reload=True)

    def tearDown(self):
        self._patcher.stop()
        with er._prom_lock:
            er._prom_state["data"] = None


class Phase3GateNegativeCases(_Phase3GateBase):

    def test_christopher_marlowe_does_NOT_clarify(self):
        """First-name disambiguator present → must RESOLVE, never CLARIFY.

        Anchor for the E13/B-R17-1 class: when the user supplies the
        first name, the resolver must read NON_FIRST_NAME_TOKENS as the
        stop-list (not as «christopher» being a stop-word) and let the
        token through to disambiguate Marlowe → Christopher.
        """
        d = resolve_v6("Christopher Marlowe")
        self.assertEqual(d.decision, Decision.RESOLVED)
        self.assertIsNotNone(d.resolved)
        self.assertIn("Christopher", d.resolved.display)
        # And in-sentence: «сколько книг написал Christopher Marlowe»
        # must also resolve — verb prefix should not strip the disambiguator.
        d2 = resolve_v6("сколько книг написал Christopher Marlowe")
        self.assertEqual(d2.decision, Decision.RESOLVED)
        self.assertIn("Christopher", d2.resolved.display)

    def test_wells_DOES_clarify(self):
        """Bare surname with multiple canonicals → must CLARIFY.

        Anchor for R-22: ambiguous surname must surface candidates,
        not auto-pick the most-prominent silently.
        """
        d = resolve_v6("Wells")
        self.assertEqual(d.decision, Decision.CLARIFY_NEEDED)
        self.assertGreaterEqual(len(d.clarify_candidates), 2)
        names = [c.display for c in d.clarify_candidates]
        self.assertTrue(
            any("H. G." in n for n in names),
            "H.G. Wells must appear in clarify list (highest prominence)",
        )


if __name__ == "__main__":
    unittest.main()
