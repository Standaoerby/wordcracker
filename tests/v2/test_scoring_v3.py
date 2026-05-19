"""Sprint 16 Phase B tests — author_influences confidence floor + filter."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.tools.authors.author_profile import (
    _annotate_confidence, _is_collection_bucket,
)


class CollectionBucketFilter(unittest.TestCase):
    def test_various_filtered(self):
        self.assertTrue(_is_collection_bucket({"author": "Various"}))
        self.assertTrue(_is_collection_bucket({"author": "Anonymous"}))
        self.assertTrue(_is_collection_bucket({"author": "Encyclopaedia Britannica"}))

    def test_real_author_not_filtered(self):
        self.assertFalse(_is_collection_bucket({"author": "Doyle, Arthur Conan"}))
        self.assertFalse(_is_collection_bucket({"author": "Poe, Edgar Allan"}))

    def test_non_dict_safe(self):
        self.assertFalse(_is_collection_bucket("Various"))
        self.assertFalse(_is_collection_bucket(None))


class ConfidenceFloor(unittest.TestCase):
    """Stan round 6 R19: Doyle/Poe returned identical baseline list.
    Mark as low-confidence so renderer tells user honestly."""

    def test_tight_cluster_marked_low(self):
        # 5 candidates with deltas within 0.502-0.510 — all near baseline
        raw = {
            "closest": [
                {"author": "Beckford", "delta": 0.504},
                {"author": "Oliphant", "delta": 0.509},
                {"author": "Gosse",    "delta": 0.510},
                {"author": "Melville", "delta": 0.512},
                {"author": "Lathrop",  "delta": 0.502},
            ],
        }
        _annotate_confidence(raw, "^Doyle,")
        self.assertEqual(raw.get("similarity_confidence"), "low")
        self.assertIn("_render_note", raw)
        self.assertIn("baseline", raw["_render_note"])

    def test_clear_winner_marked_high(self):
        # First candidate clearly closer than the rest
        raw = {
            "closest": [
                {"author": "Lovecraft", "delta": 0.30},
                {"author": "Bierce",    "delta": 0.48},
                {"author": "Machen",    "delta": 0.52},
                {"author": "Various",   "delta": 0.55},
            ],
        }
        _annotate_confidence(raw, "^Poe,")
        self.assertEqual(raw.get("similarity_confidence"), "high")
        self.assertNotIn("_render_note", raw)

    def test_too_few_candidates_no_annotation(self):
        # 2 candidates — can't compute meaningful spread
        raw = {"closest": [{"author": "X", "delta": 0.5},
                            {"author": "Y", "delta": 0.6}]}
        _annotate_confidence(raw, "^Z,")
        self.assertNotIn("similarity_confidence", raw)

    def test_missing_delta_field_safe(self):
        raw = {"closest": [{"author": "X"}, {"author": "Y"}, {"author": "Z"}]}
        # No delta field → can't compute → no annotation, no crash
        _annotate_confidence(raw, "^Z,")
        self.assertNotIn("similarity_confidence", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
