"""S-R5 smoke-nit — numeric audit must not flag explanatory scale ranges.

LIVE SMOKE (Stan, deploy 408874e): an answer that explained a difficulty
/ rubric scale like «90-100 — очень трудно, 60-70 — средне» got a 📊
numeric-audit footer claiming 90/100/60/70 are «не в data». They aren't
tool-data values — they describe a SCALE. Pure noise.

FIX: strip sub-year «N-M» ranges from the prose before extraction. The
fix is deliberately scoped to NOT touch year-range pairs (>= 1500) so the
W-7 bio-death-year fabrication path keeps working.

These tests lock both sides:
  - scale ranges no longer flagged (the reported noise);
  - the bio-year fabrication «1564-2008» and a single fabricated count
    «47 книг» are STILL flagged (no audit-coverage regression).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2 import numeric_audit as na


def _records(data: dict) -> list[dict]:
    return [{"data": data, "coverage": {}, "query": {}}]


class ScaleRangesNotFlagged(unittest.TestCase):
    def test_rubric_scale_ranges_are_silent(self):
        answer = ("Шкала сложности слов: 90-100 — очень трудно, "
                  "60-70 — средний уровень, 20-30 — легко.")
        rep = na.audit_numbers(answer, _records({"word": "fog",
                                                  "scope_count": 12}),
                               intent="learning_words")
        flagged = [m.formatted for m in rep.mismatches]
        self.assertEqual(
            flagged, [],
            f"scale-range endpoints must not be flagged, got {flagged!r}",
        )

    def test_percentage_scale_range_silent(self):
        answer = "Покрытие лексики B2: 20-30% книги — это нормально."
        rep = na.audit_numbers(answer, _records({"n": 1}),
                               intent="learning_words")
        self.assertEqual([m.formatted for m in rep.mismatches], [])


class AuditCoverageNotRegressed(unittest.TestCase):
    """The strip must NOT swallow real fabrications."""

    def test_bio_year_range_fabrication_still_flagged(self):
        # «1564-2008» — death year 2008 is fabricated (Marlowe died 1593);
        # only birth 1564 is in data. Year ranges must survive the strip.
        answer = "Кристофер Марло, годы жизни 1564-2008, был драматургом."
        rep = na.audit_numbers(answer,
                               _records({"author": "Marlowe",
                                         "year_of_birth_min": 1564}),
                               intent="author_metadata")
        self.assertTrue(
            any("2008" in m.formatted for m in rep.mismatches),
            "fabricated death year 2008 must still be flagged",
        )

    def test_single_count_fabrication_still_flagged(self):
        answer = "У Дойла 47 книг в корпусе."
        rep = na.audit_numbers(answer, _records({"books": 30}),
                               intent="author_metadata")
        self.assertTrue(
            any("47" in m.formatted for m in rep.mismatches),
            "single fabricated count 47 must still be flagged",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
