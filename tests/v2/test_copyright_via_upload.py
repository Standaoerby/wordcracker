"""Sprint 19+ — copyright-via-user-upload resolution.

Stan loaded Harry Potter Enhanced Edition locally via /admin/upload.
Before this commit, «архаизмы в Harry Potter» still hit the copyright
OOS refusal («полнотекстовый анализ невозможен») because KNOWN_BOOKS
maps «harry potter» → ("", "Harry Potter") — empty-PG sentinel.

Now: entity extractor checks user_uploads_metadata.csv. If a U-id
covers the title, the empty-PG resolution swaps to the U-id and the
tool chain runs normally. RENDER_PROMPT rule 13 adds a fair-use
disclosure footer."""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class FindUserUploadForTitle(unittest.TestCase):
    """Title → U-id matcher, used by _find_book to bypass copyright OOS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp.name) / "user_uploads_metadata.csv"
        with open(self.csv_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "id", "title", "author", "authoryearofbirth",
                "authoryearofdeath", "language", "downloads",
                "subjects", "type", "source_filename", "uploaded_ts",
            ])
            w.writeheader()
            for row in [
                {"id": "U7", "title": "Harry Potter and the Philosopher's Stone",
                 "author": "Rowling, J. K."},
                {"id": "U8", "title": "Harry Potter and the Chamber of Secrets",
                 "author": "Rowling, J. K."},
                {"id": "U9", "title": "Some Random Book", "author": "Doe, J."},
            ]:
                w.writerow(row)
        from scripts.v2.planner import entities as ent
        self._patches = [
            mock.patch.object(ent, "_USER_UPLOADS_META_PATH", self.csv_path),
            mock.patch.object(ent, "_USER_UPLOADS_CACHE", None),
            mock.patch.object(ent, "_USER_UPLOADS_CACHE_MTIME", 0.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_canonical_substring_match(self):
        """KNOWN_BOOKS canonical «Harry Potter» matches upload's
        full title via substring."""
        from scripts.v2.planner.entities import find_user_upload_for_title
        self.assertEqual(find_user_upload_for_title("Harry Potter"), "U7")

    def test_no_uploads_for_unknown_title(self):
        from scripts.v2.planner.entities import find_user_upload_for_title
        self.assertIsNone(find_user_upload_for_title("The Lord of the Rings"))

    def test_empty_title_returns_none(self):
        from scripts.v2.planner.entities import find_user_upload_for_title
        self.assertIsNone(find_user_upload_for_title(""))


class HPViaUploadEndToEnd(unittest.TestCase):
    """Round-trip: «архаизмы в Harry Potter» now resolves to U-id and
    dispatches book_archaic tool instead of hitting copyright OOS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp.name) / "user_uploads_metadata.csv"
        with open(self.csv_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "id", "title", "author", "authoryearofbirth",
                "authoryearofdeath", "language", "downloads",
                "subjects", "type", "source_filename", "uploaded_ts",
            ])
            w.writeheader()
            w.writerow({"id": "U7",
                        "title": "Harry Potter and the Philosopher's Stone",
                        "author": "Rowling, J. K."})
        from scripts.v2.planner import entities as ent
        self._patches = [
            mock.patch.object(ent, "_USER_UPLOADS_META_PATH", self.csv_path),
            mock.patch.object(ent, "_USER_UPLOADS_CACHE", None),
            mock.patch.object(ent, "_USER_UPLOADS_CACHE_MTIME", 0.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_extract_resolves_to_u_id(self):
        from scripts.v2.planner.entities import extract
        e = extract("архаизмы в Harry Potter")
        self.assertEqual(e.book_id, "U7")
        self.assertEqual(e.book_title, "Harry Potter")

    def test_plan_dispatches_book_archaic(self):
        from scripts.v2.planner.entities import extract
        from scripts.v2.planner.plan import build
        e = extract("архаизмы в Harry Potter")
        p = build("book_archaic", e)
        self.assertFalse(p.needs_clarify)
        # Was: out_of_scope copyright refusal. Now: real tool dispatch.
        self.assertNotEqual(p.intent, "out_of_scope")
        tools = [s.tool for s in p.steps]
        self.assertIn("book_archaic_words", tools[0])

    def test_no_user_uploads_still_falls_to_copyright_oos(self):
        """Sanity: if no user upload exists, KNOWN_BOOKS empty-PG
        sentinel still triggers the legacy copyright OOS path."""
        # Point to non-existent file
        from scripts.v2.planner import entities as ent
        with mock.patch.object(ent, "_USER_UPLOADS_META_PATH",
                                Path("/nonexistent/no_uploads.csv")), \
             mock.patch.object(ent, "_USER_UPLOADS_CACHE", None):
            from scripts.v2.planner.entities import extract
            from scripts.v2.planner.plan import build
            e = extract("архаизмы в Harry Potter")
            p = build("book_archaic", e)
            # No upload → book_id stays None, copyright refusal fires
            self.assertIsNone(e.book_id)
            self.assertEqual(p.intent, "out_of_scope")


class CopyrightDisclosureFlag(unittest.TestCase):
    """rag_v2._detect_copyright_via_uploads — surfaces canonical title
    to the renderer when a copyright-marked book resolves via U-id."""

    def _make_entities(self, book_id, book_title):
        from scripts.v2.planner.entities import Entities
        return Entities(book_id=book_id, book_title=book_title)

    def test_detects_hp_via_upload(self):
        from scripts.v2.rag_v2 import _detect_copyright_via_uploads
        e = self._make_entities("U7", "Harry Potter")
        is_cr, title = _detect_copyright_via_uploads(e)
        self.assertTrue(is_cr)
        self.assertEqual(title, "Harry Potter")

    def test_pg_book_not_copyright(self):
        """Pride and Prejudice has a real PG id → not flagged."""
        from scripts.v2.rag_v2 import _detect_copyright_via_uploads
        e = self._make_entities("PG1342", "Pride and Prejudice")
        is_cr, _ = _detect_copyright_via_uploads(e)
        self.assertFalse(is_cr)

    def test_unknown_user_upload_not_copyright(self):
        """U-id for a non-canonical-copyright book (admin's own
        writing) doesn't trigger the copyright disclaimer."""
        from scripts.v2.rag_v2 import _detect_copyright_via_uploads
        e = self._make_entities("U99", "My Custom Notes")
        is_cr, _ = _detect_copyright_via_uploads(e)
        self.assertFalse(is_cr)

    def test_no_book_at_all(self):
        from scripts.v2.rag_v2 import _detect_copyright_via_uploads
        e = self._make_entities(None, None)
        is_cr, _ = _detect_copyright_via_uploads(e)
        self.assertFalse(is_cr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
